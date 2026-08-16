"""OCR backends for scanned documents.

OCR is optional infrastructure: when neither OCRmyPDF nor Tesseract is on
PATH, ``get_backend()`` returns a null backend and scanned reports are left in
``ocr_pending`` instead of failing the ingest job.
"""

import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

OCR_TIMEOUT_SECONDS = 10 * 60  # spec §4.3: hard 10-minute timeout per document
OCR_PARTIAL_PAGE_LIMIT = 15  # on timeout, keep the first 15 pages
OCR_DPI = 300


@dataclass(frozen=True)
class OcrResult:
    page_texts: list[str]
    ocr_path: Path | None
    partial: bool = False


class OcrBackend(Protocol):
    name: str

    def available(self) -> bool: ...

    def ocr_pdf(self, pdf_path: Path, work_dir: Path, timeout: int = OCR_TIMEOUT_SECONDS) -> OcrResult: ...


class NullBackend:
    name = "none"

    def available(self) -> bool:
        return False

    def ocr_pdf(self, pdf_path: Path, work_dir: Path, timeout: int = OCR_TIMEOUT_SECONDS) -> OcrResult:
        raise RuntimeError("no OCR backend available")


class OcrMyPdfBackend:
    """Full OCR via the ocrmypdf CLI; writes a searchable ocr.pdf alongside the original."""

    name = "ocrmypdf"

    def available(self) -> bool:
        return shutil.which("ocrmypdf") is not None

    def ocr_pdf(self, pdf_path: Path, work_dir: Path, timeout: int = OCR_TIMEOUT_SECONDS) -> OcrResult:
        output = work_dir / "ocr.pdf"
        sidecar = work_dir / "ocr.txt"
        try:
            self._run(pdf_path, output, sidecar, timeout, pages=None)
            partial = False
        except subprocess.TimeoutExpired:
            self._run(pdf_path, output, sidecar, timeout, pages=f"1-{OCR_PARTIAL_PAGE_LIMIT}")
            partial = True
        texts = sidecar.read_text().split("\f") if sidecar.exists() else []
        return OcrResult(texts, output if output.exists() else None, partial)

    def _run(self, source: Path, output: Path, sidecar: Path, timeout: int, pages: str | None) -> None:
        command = ["ocrmypdf", "--skip-text", "--output-type", "pdf", "--sidecar", str(sidecar)]
        if pages:
            command += ["--pages", pages]
        command += [str(source), str(output)]
        subprocess.run(command, check=True, capture_output=True, timeout=timeout)


class TesseractBackend:
    """Text-only OCR via the tesseract CLI; pages are rendered at 300 DPI with PyMuPDF."""

    name = "tesseract"

    def available(self) -> bool:
        return shutil.which("tesseract") is not None

    def ocr_pdf(self, pdf_path: Path, work_dir: Path, timeout: int = OCR_TIMEOUT_SECONDS) -> OcrResult:
        import fitz

        deadline = time.monotonic() + timeout
        texts: list[str] = []
        document = fitz.open(pdf_path)
        try:
            page_count = len(document)
            with tempfile.TemporaryDirectory() as tmp:
                image = Path(tmp) / "page.png"
                for page in document:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    page.get_pixmap(dpi=OCR_DPI).save(image)
                    try:
                        output = subprocess.run(
                            ["tesseract", str(image), "stdout"],
                            check=True,
                            capture_output=True,
                            timeout=remaining,
                        )
                    except subprocess.TimeoutExpired:
                        break
                    texts.append(output.stdout.decode("utf-8", "replace"))
        finally:
            document.close()
        return OcrResult(texts, None, partial=len(texts) < page_count)


def get_backend() -> OcrBackend:
    for backend in (OcrMyPdfBackend(), TesseractBackend()):
        if backend.available():
            return backend
    return NullBackend()
