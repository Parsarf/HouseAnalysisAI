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

from common.db import db_session
from jobs.postgres import PostgresJobQueue
from ingestion.worker import ingest_document

from .orchestrator import Pipeline

log = logging.getLogger(__name__)


def _payload(payload) -> dict:
    return json.loads(payload) if isinstance(payload, str) else dict(payload)


def _pipeline() -> Pipeline:
    return Pipeline()


def _handle_extract_unit(payload) -> None:
    _pipeline().extract_unit(_payload(payload)["unit_id"])


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


def _handle_nightly(payload) -> None:  # noqa: ARG001 - no payload
    _pipeline().nightly()


def default_handlers() -> dict[str, Callable[[dict], None]]:
    return {
        "ingest_document": ingest_document,
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
