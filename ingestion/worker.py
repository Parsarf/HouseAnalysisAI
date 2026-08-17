import json
import logging
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from common.db import db_session
from common.errors import AcqError, ErrorCode
from common.storage import DocumentStorage, get_document_storage
from contracts import ReportStatus
from db.models import ExtractionUnit, Report
from ingestion.ocr import OcrBackend, get_backend

log = logging.getLogger(__name__)

# Test and embedding hook. Production composition injects the identity
# resolver from ``pipeline.worker``; keeping this nullable avoids a reverse
# package dependency while preserving the historical injection seam.
attach_report = None


def _mark_failure_reason(report: Report, code: ErrorCode) -> None:
    # reports.failure_reason is being added to the model concurrently; set it only when present.
    if hasattr(report, "failure_reason"):
        report.failure_reason = code.value


def _fail(report: Report, code: ErrorCode) -> None:
    report.status = ReportStatus.FAILED.value
    _mark_failure_reason(report, code)


def is_scanned(pages: list[str]) -> bool:
    """Spec §4.3: median chars/page < 100, or more than 40% of pages empty."""
    if not pages:
        return False
    lengths = sorted(len(page.strip()) for page in pages)
    median = lengths[(len(lengths) - 1) // 2]
    empty = sum(1 for length in lengths if length == 0)
    return median < 100 or empty > len(pages) * 0.4


def _write_pages(storage: DocumentStorage, report_ref: str, pages: list[str]) -> None:
    for number, page_text in enumerate(pages, 1):
        storage.save_text(page_text, storage.child(report_ref, f"pages/{number}.txt"))


def _resolve_identity(
    session: Session,
    report: Report,
    address: str | None,
    apn: str | None,
    fips: str | None,
    zip5: str | None,
    resolver: Callable | None = None,
) -> None:
    resolver = resolver or attach_report
    if not address or resolver is None:
        return
    try:
        resolver(session, report, address, apn=apn, fips=fips, zip5=zip5)
    except ValueError:
        # identity_conflict: leave the report unattached rather than risking a bad merge.
        log.warning("identity conflict; report %s left unattached", report.id)


def _queue_extraction_units(
    session: Session,
    report: Report,
    pages: list[str],
    *,
    classifier: Callable | None = None,
    sectioner: Callable | None = None,
    match_rate: Callable | None = None,
    enqueue: Callable | None = None,
    storage: DocumentStorage | None = None,
) -> int:
    """Create typed extraction units and enqueue them once per report."""
    from sqlalchemy import func, select
    if classifier is None or sectioner is None or match_rate is None:
        # The standalone text/OCR path intentionally stops before classification;
        # the production pipeline always supplies all four composition hooks.
        return 0
    connection = session.connection()
    if not session.get_bind().dialect.has_table(connection, "extraction_units"):
        raise AcqError(ErrorCode.INTERNAL, "extraction_units table is unavailable",
                       {"report_id": str(report.id), "required_migration": "extraction_units"})
    already_created = int(session.scalar(
        select(func.count(ExtractionUnit.id)).where(ExtractionUnit.report_id == report.id)
    ) or 0)
    log.info("document classification started", extra={
        "batch_id": report.batch_id,
        "report_id": report.id,
        "document_path": report.file_path,
        "unit_count": already_created,
    })
    result = classifier("\n\f\n".join(pages), filename=Path(report.file_path).name, pages=pages)
    units = sectioner(pages)
    if not units:
        return 0
    if already_created:
        # Re-ingestion of a report that already has units (job retry, or the same
        # document re-uploaded into a new batch). Do not duplicate the units, but
        # still apply classification so the report reaches `classified`; returning
        # 0 here previously made the caller treat the run as incomplete.
        report.report_type = result.report_type
        report.vendor = result.vendor
        report.classification_confidence = result.confidence
        report.section_match_rate = match_rate(units)
        report.status = ReportStatus.CLASSIFIED.value
        session.flush()
        log.info("document classification completed", extra={
            "batch_id": report.batch_id,
            "report_id": report.id,
            "document_path": report.file_path,
            "report_status_after": report.status,
            "unit_count": already_created,
        })
        return already_created
    storage = storage or get_document_storage()
    for index, section in enumerate(units):
        unit_id = uuid4()
        unit_path = storage.save_text(section.text,
                                      storage.child(report.file_path, f"units/{index + 1:04d}.txt"))
        unit = ExtractionUnit(
            id=unit_id, report_id=report.id, unit_type=section.unit_type,
            page_start=section.page_start, page_end=section.page_end,
            text_path=unit_path, token_estimate=section.token_estimate, status="queued")
        session.add(unit)
        session.flush()
        log.info("extraction unit created", extra={
            "batch_id": report.batch_id,
            "report_id": report.id,
            "unit_id": unit.id,
            "unit_status": unit.status,
            "document_path": unit.text_path,
        })
        if enqueue is not None:
            enqueue(session, "extract_unit", {"unit_id": str(unit_id)}, f"extract_unit:{unit_id}")
    report.report_type = result.report_type
    report.vendor = result.vendor
    report.classification_confidence = result.confidence
    report.section_match_rate = match_rate(units)
    report.status = ReportStatus.CLASSIFIED.value
    session.flush()
    log.info("document classification completed", extra={
        "batch_id": report.batch_id,
        "report_id": report.id,
        "document_path": report.file_path,
        "report_status_after": report.status,
        "unit_count": len(units),
    })
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
    identity_resolver: Callable | None = None,
    classifier: Callable | None = None,
    sectioner: Callable | None = None,
    section_matcher: Callable | None = None,
    enqueue: Callable | None = None,
    storage: DocumentStorage | None = None,
) -> Report:
    """Extract per-page text, OCR scanned documents, and resolve property identity.

    Permanent failures (encrypted/corrupt) mark the report failed with a
    FailureCode instead of raising, so the job is not retried pointlessly.
    """
    storage = storage or get_document_storage()
    if report.vendor == "pasted" or Path(report.file_path).suffix.lower() == ".txt":
        try:
            text = storage.read_text(report.file_path)
        except FileNotFoundError as exc:
            raise AcqError(
                ErrorCode.EXTRACTION_FAILED,
                "stored document is unavailable to the worker",
                {"report_id": str(report.id), "document_path": report.file_path},
            ) from exc
        pages = text.split("\f")
        report.page_count = len(pages)
        report.status = ReportStatus.TEXT_EXTRACTED.value
        _write_pages(storage, report.file_path, pages)
        _resolve_identity(session, report, address, apn, fips, zip5, identity_resolver)
        _queue_extraction_units(session, report, pages, classifier=classifier,
                                sectioner=sectioner, match_rate=section_matcher,
                                enqueue=enqueue, storage=storage)
        return report
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for document ingestion") from exc
    try:
        with storage.materialize(report.file_path) as pdf:
            log.info("stored document materialized", extra={
                "batch_id": report.batch_id,
                "report_id": report.id,
                "document_path": report.file_path,
                "storage_backend": "s3" if report.file_path.startswith("s3://") else "filesystem",
            })
            try:
                document = fitz.open(pdf)
            except Exception:  # noqa: BLE001 — PyMuPDF raises several types for malformed files
                _fail(report, ErrorCode.CORRUPT)
                return report
            return _run_document_ingest(session, report, document, pdf, storage,
                                        ocr_backend=ocr_backend, address=address, apn=apn,
                                        fips=fips, zip5=zip5, identity_resolver=identity_resolver,
                                        classifier=classifier, sectioner=sectioner,
                                        section_matcher=section_matcher, enqueue=enqueue)
    except FileNotFoundError as exc:
        raise AcqError(
            ErrorCode.EXTRACTION_FAILED,
            "stored document is unavailable to the worker",
            {"report_id": str(report.id), "document_path": report.file_path},
        ) from exc
    except AcqError:
        raise
    except OSError:
        _fail(report, ErrorCode.CORRUPT)
        return report


def _run_document_ingest(session: Session, report: Report, document, pdf: Path,
                         storage: DocumentStorage, *, ocr_backend: OcrBackend | None,
                         address: str | None, apn: str | None, fips: str | None,
                         zip5: str | None, identity_resolver: Callable | None,
                         classifier: Callable | None, sectioner: Callable | None,
                         section_matcher: Callable | None, enqueue: Callable | None) -> Report:
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
        ocr_backend_name = "not_required"
        ocr_backend_available = False
        log.info("document scan detection completed", extra={
            "batch_id": report.batch_id,
            "report_id": report.id,
            "document_path": report.file_path,
            "is_scanned": report.is_scanned,
            "report_status_after": report.status,
        })
        if report.is_scanned:
            backend = ocr_backend if ocr_backend is not None else get_backend()
            ocr_backend_name = backend.name
            ocr_backend_available = backend.available()
            log.info("OCR backend selected", extra={
                "batch_id": report.batch_id,
                "report_id": report.id,
                "document_path": report.file_path,
                "is_scanned": True,
                "ocr_backend": ocr_backend_name,
                "ocr_backend_available": ocr_backend_available,
            })
            if not ocr_backend_available:
                # No OCR tooling installed: keep what little text exists and
                # leave the report queued for OCR rather than failing it.
                report.status = ReportStatus.OCR_PENDING.value
                _write_pages(storage, report.file_path, pages)
                log.warning("OCR decision completed without an available backend", extra={
                    "batch_id": report.batch_id,
                    "report_id": report.id,
                    "document_path": report.file_path,
                    "is_scanned": True,
                    "ocr_backend": ocr_backend_name,
                    "ocr_backend_available": False,
                    "ocr_applied": False,
                    "report_status_after": report.status,
                })
                return report
            result = backend.ocr_pdf(pdf, pdf.parent)
            report.ocr_applied = True
            if result.ocr_path is not None:
                report.ocr_path = storage.save_file(result.ocr_path,
                                                    storage.child(report.file_path, "ocr.pdf"))
            if result.partial:
                _mark_failure_reason(report, ErrorCode.PARTIAL_OCR)
            merged = [ocr if ocr.strip() else original for original, ocr in zip(pages, result.page_texts)]
            pages = merged + pages[len(result.page_texts) :]
        _write_pages(storage, report.file_path, pages)
        report.status = ReportStatus.TEXT_EXTRACTED.value
        log.info("OCR decision completed", extra={
            "batch_id": report.batch_id,
            "report_id": report.id,
            "document_path": report.file_path,
            "is_scanned": report.is_scanned,
            "ocr_backend": ocr_backend_name,
            "ocr_backend_available": ocr_backend_available,
            "ocr_applied": report.ocr_applied,
            "report_status_after": report.status,
        })
    finally:
        document.close()
    _resolve_identity(session, report, address, apn, fips, zip5, identity_resolver)
    if report.status == ReportStatus.TEXT_EXTRACTED.value:
        _queue_extraction_units(session, report, pages, classifier=classifier,
                                sectioner=sectioner, match_rate=section_matcher,
                                enqueue=enqueue, storage=storage)
    return report


def ingest_document(payload: dict, **hooks) -> Report:
    if isinstance(payload, str):
        payload = json.loads(payload)
    report_id = payload["report_id"]
    committed = {}
    with db_session() as session:
        report = session.get(Report, UUID(str(report_id)))
        if report is None:
            raise ValueError("report not found")
        status_before = report.status
        log.info("report ingestion state before", extra={
            "batch_id": report.batch_id,
            "report_id": report.id,
            "report_status_before": status_before,
            "document_path": report.file_path,
        })
        report = run_ingest(
            session,
            report,
            address=payload.get("address"),
            apn=payload.get("apn"),
            fips=payload.get("fips"),
            zip5=payload.get("zip5"),
            storage=get_document_storage(),
            **hooks,
        )
        session.flush()
        units = session.query(ExtractionUnit).filter(ExtractionUnit.report_id == report.id).all()
        committed = {
            "batch_id": report.batch_id,
            "report_id": report.id,
            "report_status_before": status_before,
            "report_status_after": report.status,
            "unit_statuses": {
                status: sum(unit.status == status for unit in units)
                for status in sorted({unit.status for unit in units})
            },
        }
        log.info("report ingestion state after", extra=committed)
    log.info("report ingestion transaction committed", extra={
        **committed,
        "transaction_status": "committed",
    })
    return report
