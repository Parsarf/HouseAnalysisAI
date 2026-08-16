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

from classification import classify, section_match_rate, section_pages
from common.db import db_session
from common.storage import get_document_storage
from extraction import ExtractionService, ProviderClient, UnitInput
from identity.service import attach_report
from ingestion.worker import ingest_document
from jobs.postgres import PostgresJobQueue

from .orchestrator import Pipeline

log = logging.getLogger(__name__)


def _payload(payload) -> dict:
    return json.loads(payload) if isinstance(payload, str) else dict(payload)


def _pipeline(**kwargs) -> Pipeline:
    return Pipeline(**kwargs)


def _extractor(unit: dict):
    from uuid import UUID

    unit_id = UUID(str(unit["id"]))
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
    # extraction-unit transition.  Keep this adapter side-effect free so a
    # retry cannot insert the same fact ledger twice.
    return ExtractionService(ProviderClient()).extract_unit(request)


def _handle_ingest_document(payload) -> None:
    queue = PostgresJobQueue()

    def enqueue(session, name, job_payload, dedupe_key):
        return queue.enqueue(session, name, json.dumps(job_payload), dedupe_key)

    ingest_document(_payload(payload), identity_resolver=attach_report,
                    classifier=classify, sectioner=section_pages,
                    section_matcher=section_match_rate, enqueue=enqueue)


def _handle_extract_unit(payload) -> None:
    _pipeline(extractor=_extractor).extract_unit(_payload(payload)["unit_id"])


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
            try:
                self.handlers[job["name"]](job["payload"])
            except Exception as exc:
                self.queue.fail(session, job["id"], job["attempts"], job["max_attempts"], str(exc))
                log.exception("job failed", extra={"job_id": job["id"]})
            else:
                self.queue.complete(session, job["id"])
            return True


def default_worker() -> Worker:
    return Worker(default_handlers())
