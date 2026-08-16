import hashlib
import shutil
import zipfile
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common.errors import AcqError, ErrorCode
from contracts import ReportStatus
from db.models import Report


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def store_pdf(source: Path, document_root: Path, report_id: UUID | None = None) -> tuple[UUID, str]:
    with source.open("rb") as stream:
        magic = stream.read(5)
    if magic != b"%PDF-":
        raise AcqError(ErrorCode.NOT_PDF, f"not a PDF: {source.name}")
    report_id = report_id or uuid4()
    target = document_root / str(report_id)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target / "original.pdf")
    return report_id, sha256_file(source)


def extract_text_pages(text: str, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    pages = text.split("\f")
    paths = []
    for number, page in enumerate(pages, 1):
        path = output_dir / f"{number}.txt"
        path.write_text(page)
        paths.append(path)
    return paths


def safe_zip_members(path: Path, max_files: int = 3000) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        if len(members) > max_files:
            raise AcqError(ErrorCode.INVALID_INPUT, "batch exceeds file limit")
        if any(Path(member).is_absolute() or ".." in Path(member).parts for member in members):
            raise AcqError(ErrorCode.INVALID_INPUT, "unsafe ZIP path")
        return members


def find_by_sha256(session: Session, digest: str) -> Report | None:
    return session.scalar(select(Report).where(Report.sha256 == digest))


def register_pdf(
    session: Session, source: Path, document_root: Path, batch_id: UUID | None = None
) -> tuple[Report, bool]:
    """Store a PDF and insert its report row. Returns ``(report, created)``.

    A byte-identical re-upload returns the existing report instead of raising
    on the sha256 unique constraint (spec §4.2).
    """
    digest = sha256_file(source)
    existing = find_by_sha256(session, digest)
    if existing is not None:
        return existing, False
    report_id, _ = store_pdf(source, document_root)
    report = Report(
        id=report_id,
        batch_id=batch_id,
        file_path=str(document_root / str(report_id) / "original.pdf"),
        sha256=digest,
        status=ReportStatus.UPLOADED.value,
    )
    session.add(report)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        # Lost a race with a concurrent upload of the same file.
        existing = find_by_sha256(session, digest)
        if existing is None:
            raise
        return existing, False
    return report, True


def ingest_paste(
    session: Session, text: str, document_root: Path, batch_id: UUID | None = None
) -> tuple[Report, bool]:
    """Pasted text becomes a single-page pseudo-report with vendor='pasted' (spec §4.7)."""
    digest = hashlib.sha256(text.encode()).hexdigest()
    existing = find_by_sha256(session, digest)
    if existing is not None:
        return existing, False
    report_id = uuid4()
    target = document_root / str(report_id)
    target.mkdir(parents=True, exist_ok=True)
    original = target / "original.txt"
    original.write_text(text)
    extract_text_pages(text, target / "pages")
    report = Report(
        id=report_id,
        batch_id=batch_id,
        file_path=str(original),
        sha256=digest,
        vendor="pasted",
        status=ReportStatus.TEXT_EXTRACTED.value,
        page_count=1,
    )
    session.add(report)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        existing = find_by_sha256(session, digest)
        if existing is None:
            raise
        return existing, False
    return report, True


def scan_inbox(
    session: Session, inbox: Path, document_root: Path, batch_id: UUID | None = None
) -> list[Report]:
    """Watched folder (spec §4.1): ingest every PDF dropped into the inbox directory.

    Successfully registered files are moved to ``inbox/processed``; files that
    fail validation are moved to ``inbox/failed`` so they are not retried forever.
    """
    processed = inbox / "processed"
    failed = inbox / "failed"
    reports = []
    for source in sorted(inbox.glob("*.pdf")):
        try:
            report, _created = register_pdf(session, source, document_root, batch_id=batch_id)
        except AcqError:
            failed.mkdir(exist_ok=True)
            shutil.move(str(source), failed / source.name)
            continue
        processed.mkdir(exist_ok=True)
        shutil.move(str(source), processed / source.name)
        reports.append(report)
    return reports
