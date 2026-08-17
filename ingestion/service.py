import hashlib
import logging
import shutil
import zipfile
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common.errors import AcqError, ErrorCode
from common.storage import DocumentStorage, LocalFilesystemStorage, S3Storage, document_key
from contracts import ReportStatus
from db.models import Report

log = logging.getLogger(__name__)


def _storage_backend(storage: DocumentStorage | None) -> str:
    return "s3" if isinstance(storage, S3Storage) else "filesystem"


def _log_registration(report: Report, *, created: bool, previous_batch_id: UUID | None) -> None:
    log.info("report created or deduplicated", extra={
        "event": "report_registered",
        "batch_id": report.batch_id,
        "report_id": report.id,
        "report_created": created,
        "previous_batch_id": previous_batch_id,
        "final_batch_id": report.batch_id,
        "report_status_after": report.status,
        "document_path": report.file_path,
        "sha256": report.sha256,
    })


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def store_pdf(source: Path, document_root: Path, report_id: UUID | None = None,
              storage: DocumentStorage | None = None) -> tuple[UUID, str]:
    with source.open("rb") as stream:
        magic = stream.read(5)
    if magic != b"%PDF-":
        raise AcqError(ErrorCode.NOT_PDF, f"not a PDF: {source.name}")
    report_id = report_id or uuid4()
    storage = storage or LocalFilesystemStorage(document_root)
    return report_id, storage.save_file(source, document_key(report_id, "original.pdf"))


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
    session: Session, source: Path, document_root: Path, batch_id: UUID | None = None,
    storage: DocumentStorage | None = None,
) -> tuple[Report, bool]:
    """Store a PDF and insert its report row. Returns ``(report, created)``.

    A byte-identical re-upload returns the existing report instead of raising
    on the sha256 unique constraint (spec §4.2).
    """
    digest = sha256_file(source)
    existing = find_by_sha256(session, digest)
    if existing is not None:
        storage = storage or LocalFilesystemStorage(document_root)
        previous_batch_id = existing.batch_id
        backend_changed = isinstance(storage, S3Storage) != existing.file_path.startswith("s3://")
        if existing.status == ReportStatus.FAILED.value or backend_changed:
            _report_id, file_ref = store_pdf(source, document_root, report_id=existing.id,
                                             storage=storage)
            existing.batch_id = batch_id
            existing.file_path = file_ref
            existing.status = ReportStatus.UPLOADED.value
            existing.failure_reason = None
            existing.page_count = None
            existing.is_scanned = False
            existing.ocr_applied = False
            log.info("file saved to document storage", extra={
                "event": "file_saved",
                "batch_id": existing.batch_id,
                "report_id": existing.id,
                "storage_backend": _storage_backend(storage),
                "document_path": existing.file_path,
                "storage_operation": "replaced",
            })
            _log_registration(existing, created=True, previous_batch_id=previous_batch_id)
            return existing, True
        # A byte-identical re-upload into a new batch must re-point the report
        # at that batch. Leaving batch_id on the previous batch orphans the new
        # one: it owns zero reports, so no ingest job is ever created for it and
        # nothing can move it off `ingesting`.
        if batch_id is not None and existing.batch_id != batch_id:
            existing.batch_id = batch_id
        log.info("existing document storage reused", extra={
            "event": "file_saved",
            "batch_id": existing.batch_id,
            "report_id": existing.id,
            "storage_backend": _storage_backend(storage),
            "document_path": existing.file_path,
            "storage_operation": "reused",
        })
        _log_registration(existing, created=False, previous_batch_id=previous_batch_id)
        return existing, False
    report_id, file_ref = store_pdf(source, document_root, storage=storage)
    report = Report(
        id=report_id,
        batch_id=batch_id,
        file_path=file_ref,
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
        _log_registration(existing, created=False, previous_batch_id=existing.batch_id)
        return existing, False
    log.info("file saved to document storage", extra={
        "event": "file_saved",
        "batch_id": report.batch_id,
        "report_id": report.id,
        "storage_backend": _storage_backend(storage),
        "document_path": report.file_path,
        "storage_operation": "created",
    })
    _log_registration(report, created=True, previous_batch_id=None)
    return report, True


def ingest_paste(
    session: Session, text: str, document_root: Path, batch_id: UUID | None = None,
    storage: DocumentStorage | None = None,
) -> tuple[Report, bool]:
    """Pasted text becomes a single-page pseudo-report with vendor='pasted' (spec §4.7)."""
    digest = hashlib.sha256(text.encode()).hexdigest()
    existing = find_by_sha256(session, digest)
    if existing is not None:
        return existing, False
    report_id = uuid4()
    storage = storage or LocalFilesystemStorage(document_root)
    original = storage.save_text(text, document_key(report_id, "original.txt"))
    for page, page_text in enumerate(text.split("\f"), 1):
        storage.save_text(page_text, document_key(report_id, f"pages/{page}.txt"))
    report = Report(
        id=report_id,
        batch_id=batch_id,
        file_path=original,
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
    session: Session, inbox: Path, document_root: Path, batch_id: UUID | None = None,
    storage: DocumentStorage | None = None,
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
            report, _created = register_pdf(session, source, document_root, batch_id=batch_id,
                                            storage=storage)
        except AcqError:
            failed.mkdir(exist_ok=True)
            shutil.move(str(source), failed / source.name)
            continue
        processed.mkdir(exist_ok=True)
        shutil.move(str(source), processed / source.name)
        reports.append(report)
    return reports
