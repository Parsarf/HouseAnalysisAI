"""WP-10 job handlers and the worker loop.

Registers every job the pipeline owns (spec §17): ``ingest_document``,
``extract_unit``, ``recompute_property``, ``rank_scope``, ``detect_changes``,
``nightly``. Handlers accept payloads as dicts or JSON strings (the Postgres
queue stores jsonb; some producers enqueue serialized JSON).
"""
import json
import logging
from collections.abc import Callable
from decimal import Decimal
from urllib.parse import urlparse
from uuid import UUID

from classification import classify, section_match_rate, section_pages
from common.db import db_session
from common.errors import ErrorCode
from common.storage import get_document_storage
from contracts import ReportStatus
from db.models import Batch, ExtractionUnit, Report
from extraction import ExtractionService, ProviderClient, UnitInput
from identity.service import attach_report
from ingestion.worker import ingest_document
from jobs.postgres import PostgresJobQueue

from .orchestrator import Pipeline

log = logging.getLogger(__name__)


class PermanentJobFailure(RuntimeError):
    """A job failure that retries cannot repair (for example, a corrupt PDF)."""


def _payload(payload) -> dict:
    return json.loads(payload) if isinstance(payload, str) else dict(payload)


def _pipeline(**kwargs) -> Pipeline:
    return Pipeline(**kwargs)


def _extractor(unit: dict):
    unit_id = UUID(str(unit["id"]))
    context = {
        "unit_id": unit_id,
        "report_id": unit.get("report_id"),
        "batch_id": unit.get("batch_id"),
        "document_path": unit.get("text_path"),
        "unit_type": unit.get("unit_type"),
        "token_estimate": unit.get("token_estimate") or 0,
    }
    provider = ProviderClient()
    context.update({
        "model": provider.model_for(unit["unit_type"]),
        "provider_host": urlparse(provider.base_url).hostname,
    })
    log.info("extraction started", extra={
        **context, "event": "extract_started", "stage": "extraction", "success": True,
    })
    try:
        with get_document_storage().materialize(unit["text_path"]) as text_path:
            text = text_path.read_text()
        request = UnitInput(
            id=unit_id,
            report_id=UUID(str(unit["report_id"])),
            unit_type=unit["unit_type"], text=text,
            page_start=unit.get("page_start") or 1,
            page_end=unit.get("page_end") or 1,
            property_id=unit.get("property_id"), batch_id=unit.get("batch_id"),
            token_estimate=unit.get("token_estimate") or 0,
        )
        # Pipeline.SqlStore owns fact persistence, budget reservation, and the
        # extraction-unit transition. Keep this adapter side-effect free so a
        # retry cannot insert the same fact ledger twice.
        result = ExtractionService(provider).extract_unit(request)
    except Exception as exc:
        log.exception("extraction failed", extra={
            **context,
            "event": "extract_failed",
            "stage": "extraction",
            "success": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        })
        raise
    log.info("extraction completed", extra={
        **context,
        "event": "extract_completed",
        "stage": "extraction",
        "success": True,
        "model_used": result.model,
    })
    return result


def _handle_ingest_document(payload) -> None:
    data = _payload(payload)
    report_id = UUID(str(data["report_id"]))
    with db_session() as session:
        report = session.get(Report, report_id)
        if report is None:
            raise PermanentJobFailure(f"report {report_id} not found")
        context = {"report_id": report.id, "batch_id": report.batch_id,
                   "document_path": report.file_path}
    log.info("document ingestion started", extra={
        **context, "event": "ingestion_started", "stage": "ingestion", "success": True,
    })
    report = ingest_document(data, identity_resolver=attach_report,
                             classifier=classify, sectioner=section_pages,
                             section_matcher=section_match_rate)
    if report.status == ReportStatus.FAILED.value:
        raise PermanentJobFailure(
            f"document ingestion failed: {report.failure_reason or ErrorCode.EXTRACTION_FAILED.value}")
    batch_status, unit_count = _refresh_batch_after_ingest(report)
    if report.status != ReportStatus.CLASSIFIED.value or unit_count == 0:
        raise PermanentJobFailure(
            f"document ingestion incomplete: report status={report.status}, "
            f"extraction units={unit_count}"
        )
    log.info("document ingestion completed", extra={
        "event": "ingestion_completed",
        "stage": "ingestion",
        "success": True,
        **context,
        "report_status_after": report.status,
        "unit_statuses": {"queued": unit_count},
        "batch_status_after": batch_status,
    })


def _handle_extract_unit(payload) -> None:
    unit_id = UUID(str(_payload(payload)["unit_id"]))
    with db_session() as session:
        unit = session.get(ExtractionUnit, unit_id)
        if unit is not None and unit.status == "queued":
            unit.status = "running"
    _pipeline(extractor=_extractor).extract_unit(unit_id)


def _handle_recompute_property(payload) -> None:
    data = _payload(payload)
    price = data.get("purchase_price")
    _pipeline().recompute(
        data["property_id"], reason=data.get("reason", "manual"),
        purchase_price=Decimal(str(price)) if price is not None else None)


def _handle_rank_scope(payload) -> None:
    data = _payload(payload)
    _pipeline().rank_scope(data.get("scope_type") or data.get("scope") or "portfolio",
                           data.get("scope_id"))


def _handle_detect_changes(payload) -> None:
    data = _payload(payload)
    _pipeline().detect_changes(data["property_id"],
                               source_report_id=data.get("source_report_id"))


def _handle_nightly(payload) -> None:
    _pipeline().nightly()


def default_handlers() -> dict[str, Callable[[dict], None]]:
    return {
        "ingest_document": _handle_ingest_document,
        "extract_unit": _handle_extract_unit,
        "recompute_property": _handle_recompute_property,
        "rank_scope": _handle_rank_scope,
        "detect_changes": _handle_detect_changes,
        "nightly": _handle_nightly,
    }


class Worker:
    def __init__(self, handlers: dict[str, Callable[[dict], None]],
                 queue=None, session_factory=None):
        self.handlers = handlers
        self.queue = queue or PostgresJobQueue()
        self._session_factory = session_factory or db_session

    def run_once(self) -> bool:
        with self._session_factory() as session:
            job = self.queue.claim(session)
            if not job:
                return False
            context = {
                "job_id": job["id"],
                "job_name": job["name"],
                "attempt": job["attempts"],
                "max_attempts": job["max_attempts"],
                "stage": "job_handler",
            }
            try:
                context.update(_job_context(session, job))
            except Exception:
                log.warning("unable to load job context", extra=context, exc_info=True)
            log.info("job found", extra={
                **context, "event": "worker_job_claimed", "success": True,
            })
            try:
                self.handlers[job["name"]](job["payload"])
            except Exception as exc:
                terminal = isinstance(exc, PermanentJobFailure) or job["attempts"] >= job["max_attempts"]
                attempts = job["max_attempts"] if terminal else job["attempts"]
                self.queue.fail(session, job["id"], attempts, job["max_attempts"], str(exc))
                if terminal:
                    _mark_terminal_failure(session, job, exc)
                log.exception(
                    "job failed permanently" if terminal else "job failed; retry scheduled",
                    extra={
                        **context,
                        "event": "job_failed_permanently" if terminal else "job_retry_scheduled",
                        "success": False,
                        "job_status": "dead" if terminal else "queued",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
            else:
                self.queue.complete(session, job["id"])
                log.info("job completion", extra={
                    **context,
                    "event": "worker_job_completed",
                    "success": True,
                    "job_status": "complete",
                })
            return True

    def recover_stale(self) -> int:
        recover = getattr(self.queue, "recover_stale", None)
        if recover is None:
            return 0
        with self._session_factory() as session:
            return int(recover(session))

    def claimable_summary(self) -> dict[str, int]:
        summarize = getattr(self.queue, "claimable_summary", None)
        if summarize is None:
            return {}
        with self._session_factory() as session:
            return dict(summarize(session))


def _job_context(session, job: dict) -> dict:
    data = _payload(job["payload"])
    if job["name"] == "ingest_document" and data.get("report_id"):
        report = session.get(Report, UUID(str(data["report_id"])))
        if report is not None:
            return {"report_id": report.id, "batch_id": report.batch_id,
                    "document_path": report.file_path}
    if job["name"] == "extract_unit" and data.get("unit_id"):
        unit = session.get(ExtractionUnit, UUID(str(data["unit_id"])))
        if unit is not None:
            report = session.get(Report, unit.report_id)
            return {"unit_id": unit.id, "report_id": unit.report_id,
                    "batch_id": report.batch_id if report else None,
                    "document_path": unit.text_path}
    return {}


def _mark_terminal_failure(session, job: dict, exc: Exception) -> None:
    """Move domain records to terminal failure when a queue job is exhausted."""
    data = _payload(job["payload"])
    batch: Batch | None = None
    if job["name"] == "ingest_document" and data.get("report_id"):
        report = session.get(Report, UUID(str(data["report_id"])))
        if report is not None:
            report.status = ReportStatus.FAILED.value
            report.failure_reason = report.failure_reason or ErrorCode.EXTRACTION_FAILED.value
            batch = session.get(Batch, report.batch_id) if report.batch_id else None
    elif job["name"] == "extract_unit" and data.get("unit_id"):
        unit = session.get(ExtractionUnit, UUID(str(data["unit_id"])))
        if unit is not None:
            was_failed = unit.status == "failed"
            unit.status = "failed"
            report = session.get(Report, unit.report_id)
            if report is not None:
                report.status = ReportStatus.FAILED.value
                report.failure_reason = ErrorCode.EXTRACTION_FAILED.value
                batch = session.get(Batch, report.batch_id) if report.batch_id else None
                if batch is not None and not was_failed:
                    batch.failed_count = (batch.failed_count or 0) + 1
    if batch is not None:
        if job["name"] == "ingest_document":
            failed_reports = (session.query(Report)
                              .filter(Report.batch_id == batch.id,
                                      Report.status == ReportStatus.FAILED.value)
                              .count())
            batch.failed_count = max(batch.failed_count or 0, failed_reports)
        batch.status = "failed"
    failure_context = {
        **_job_context(session, job),
        "event": "batch_failed" if batch is not None else "domain_record_failed",
        "stage": "job_handler",
        "success": False,
        "job_id": job["id"],
        "job_name": job["name"],
        "error_type": type(exc).__name__,
        "error_message": str(exc),
    }
    if batch is not None:
        failure_context.update({
            "final_status": batch.status,
            "total_count": batch.total_count,
            "completed_count": batch.completed_count,
            "failed_count": batch.failed_count,
        })
    log.exception("domain records marked failed", extra=failure_context)


def _refresh_batch_after_ingest(report: Report) -> tuple[str, int]:
    if report.batch_id is None:
        return "unbatched", 0
    committed = {}
    current_report_units = 0
    with db_session() as session:
        batch = session.get(Batch, report.batch_id)
        if batch is None:
            return "missing", 0
        reports = session.query(Report).filter(Report.batch_id == batch.id).all()
        report_ids = [row.id for row in reports]
        units = (session.query(ExtractionUnit)
                 .filter(ExtractionUnit.report_id.in_(report_ids)).all()) if report_ids else []
        unit_report_ids = {unit.report_id for unit in units}
        pending = [row for row in reports if row.status in (
            ReportStatus.UPLOADED.value, ReportStatus.OCR_PENDING.value,
        ) or (row.status != ReportStatus.FAILED.value and row.id not in unit_report_ids)]
        failed = [row for row in reports if row.status == ReportStatus.FAILED.value]
        status_before = batch.status
        batch.failed_count = len(failed)
        if not pending:
            batch.status = "failed" if len(failed) == len(reports) else "uploaded"
        current_report_units = sum(unit.report_id == report.id for unit in units)
        session.flush()
        committed = {
            "event": "batch_status_changed" if status_before != batch.status else "batch_status_refreshed",
            "stage": "batch_refresh",
            "success": True,
            "batch_id": batch.id,
            "report_id": report.id,
            "batch_status_before": status_before,
            "batch_status_after": batch.status,
            "total_count": batch.total_count,
            "completed_count": batch.completed_count,
            "failed_count": batch.failed_count,
            "report_count": len(reports),
            "unit_count": len(units),
            "report_statuses": {
                status: sum(row.status == status for row in reports)
                for status in sorted({row.status for row in reports})
            },
            "unit_statuses": {
                status: sum(unit.status == status for unit in units)
                for status in sorted({unit.status for unit in units})
            },
        }
        log.info("batch status recomputed after ingestion", extra=committed)
    log.info("batch ingestion state committed", extra={
        **committed,
        "event": "ingestion_transaction_committed",
        "transaction_status": "committed",
        "report_status_after": report.status,
    })
    return str(committed["batch_status_after"]), current_report_units


def default_worker() -> Worker:
    return Worker(default_handlers())
