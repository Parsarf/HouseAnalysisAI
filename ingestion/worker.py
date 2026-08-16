import json
import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from common.db import db_session
from common.errors import ErrorCode
from db.models import ExtractionUnit, Report
from identity.service import attach_report
from ingestion.ocr import OcrBackend, get_backend
from classification import classify, section_match_rate, section_pages
from jobs.postgres import PostgresJobQueue
from uuid import uuid4

log = logging.getLogger(__name__)


def _mark_failure_reason(report: Report, code: ErrorCode) -> None:
    # reports.failure_reason is being added to the model concurrently; set it only when present.
    if hasattr(report, "failure_reason"):
        report.failure_reason = code.value


def _fail(report: Report, code: ErrorCode) -> None:
    report.status = "failed"
    _mark_failure_reason(report, code)


def is_scanned(pages: list[str]) -> bool:
    """Spec §4.3: median chars/page < 100, or more than 40% of pages empty."""
    if not pages:
        return False
    lengths = sorted(len(page.strip()) for page in pages)
    median = lengths[(len(lengths) - 1) // 2]
    empty = sum(1 for length in lengths if length == 0)
    return median < 100 or empty > len(pages) * 0.4


def _write_pages(pdf: Path, pages: list[str]) -> None:
    page_dir = pdf.parent / "pages"
    page_dir.mkdir(parents=True, exist_ok=True)
    for number, page_text in enumerate(pages, 1):
        (page_dir / f"{number}.txt").write_text(page_text)


def _resolve_identity(
    session: Session,
    report: Report,
    address: str | None,
    apn: str | None,
    fips: str | None,
    zip5: str | None,
) -> None:
    if not address:
        return
    try:
        attach_report(session, report, address, apn=apn, fips=fips, zip5=zip5)
    except ValueError:
        # identity_conflict: leave the report unattached rather than risking a bad merge.
        log.warning("identity conflict; report %s left unattached", report.id)


def _queue_extraction_units(session: Session, report: Report, pages: list[str]) -> int:
    """Create typed extraction units and enqueue them once per report."""
    from sqlalchemy import select
    connection = session.connection()
    if not session.get_bind().dialect.has_table(connection, "extraction_units"):
        # Lightweight ORM-only test databases and older deployments may not
        # have the extraction tables yet; ingestion remains usable and the
        # next migration/ingest run can materialize units.
        return 0
    if session.scalar(select(ExtractionUnit.id).where(ExtractionUnit.report_id == report.id).limit(1)):
        return 0
    result = classify("\n\f\n".join(pages), filename=Path(report.file_path).name, pages=pages)
    units = section_pages(pages)
    if not units:
        return 0
    unit_dir = Path(report.file_path).parent / "units"
    unit_dir.mkdir(parents=True, exist_ok=True)
    queue = PostgresJobQueue()
    for index, section in enumerate(units):
        unit_id = uuid4()
        unit_path = unit_dir / f"{index + 1:04d}.txt"
        unit_path.write_text(section.text)
        session.add(ExtractionUnit(
            id=unit_id, report_id=report.id, unit_type=section.unit_type,
            page_start=section.page_start, page_end=section.page_end,
            text_path=str(unit_path), token_estimate=section.token_estimate, status="queued"))
        queue.enqueue(session, "extract_unit", json.dumps({"unit_id": str(unit_id)}),
                      f"extract_unit:{unit_id}")
    report.report_type = result.report_type
    report.vendor = result.vendor
    report.classification_confidence = result.confidence
    report.section_match_rate = section_match_rate(units)
    report.status = "classified"
    session.flush()
    return len(units)


def run_ingest(
    session: Session,
    report: Report,
    *,
    ocr_backend: OcrBackend | None = None,
    address: str | None = None,
    apn: str | None = None,
    fips: str | None = None,
    zip5: str | None = None,
) -> Report:
    """Extract per-page text, OCR scanned documents, and resolve property identity.

    Permanent failures (encrypted/corrupt) mark the report failed with a
    FailureCode instead of raising, so the job is not retried pointlessly.
    """
    pdf = Path(report.file_path)
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for document ingestion") from exc
    try:
        document = fitz.open(pdf)
    except Exception:  # noqa: BLE001 — PyMuPDF raises several types for malformed files; all map to CORRUPT
        _fail(report, ErrorCode.CORRUPT)
        return report
    try:
        if document.needs_pass:
            _fail(report, ErrorCode.ENCRYPTED)
            return report
        try:
            pages = [page.get_text() for page in document]
        except Exception:  # noqa: BLE001 — a page that fails mid-extraction means the file is corrupt
            _fail(report, ErrorCode.CORRUPT)
            return report
        report.page_count = len(pages)
        report.is_scanned = is_scanned(pages)
        if report.is_scanned:
            backend = ocr_backend if ocr_backend is not None else get_backend()
            if not backend.available():
                # No OCR tooling installed: keep what little text exists and
                # leave the report queued for OCR rather than failing it.
                report.status = "ocr_pending"
                _write_pages(pdf, pages)
                return report
            result = backend.ocr_pdf(pdf, pdf.parent)
            report.ocr_applied = True
            if result.ocr_path is not None:
                report.ocr_path = str(result.ocr_path)
            if result.partial:
                _mark_failure_reason(report, ErrorCode.PARTIAL_OCR)
            merged = [ocr if ocr.strip() else original for original, ocr in zip(pages, result.page_texts)]
            pages = merged + pages[len(result.page_texts) :]
        _write_pages(pdf, pages)
        report.status = "text_extracted"
    finally:
        document.close()
    _resolve_identity(session, report, address, apn, fips, zip5)
    if report.status == "text_extracted":
        _queue_extraction_units(session, report, pages)
    return report


def ingest_document(payload: dict) -> None:
    if isinstance(payload, str):
        payload = json.loads(payload)
    report_id = payload["report_id"]
    with db_session() as session:
        report = session.get(Report, UUID(str(report_id)))
        if report is None:
            raise ValueError("report not found")
        run_ingest(
            session,
            report,
            address=payload.get("address"),
            apn=payload.get("apn"),
            fips=payload.get("fips"),
            zip5=payload.get("zip5"),
        )
