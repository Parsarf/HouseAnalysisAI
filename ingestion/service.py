import hashlib
import shutil
import zipfile
from pathlib import Path
from uuid import UUID, uuid4

from common.errors import AcqError, ErrorCode


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
