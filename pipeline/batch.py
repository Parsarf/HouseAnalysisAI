"""Batch lifecycle state machine (WP-10).

States: ``uploading → ingesting → estimating → awaiting_confirmation →
extracting → computing → complete | paused_budget | failed``.

``paused_budget`` is a first-class state, not an error: extraction jobs
requeue while a batch is paused and resume when the budget is raised.
``awaiting_confirmation`` (a column the API reads for the estimate-confirmation
screen) is driven by :func:`estimation_ready` / :func:`confirm_estimate`.

All functions take a store (``pipeline.store.SqlStore`` in production, an
in-memory fake in tests); they never open transactions themselves.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from common.errors import AcqError, ErrorCode

BATCH_STATES = (
    "uploading", "ingesting", "estimating", "awaiting_confirmation",
    "extracting", "computing", "complete", "paused_budget", "failed",
)

_ACTIVE_AFTER_PAUSE = "extracting"


def _batch(store, batch_id: UUID) -> dict:
    batch = store.get_batch(batch_id)
    if batch is None:
        raise AcqError(ErrorCode.NOT_FOUND, f"batch {batch_id} not found")
    return batch


def start_ingestion(store, batch_id: UUID, *, file_count: int) -> str:
    store.update_batch(batch_id, status="ingesting", file_count=file_count,
                       total_count=file_count)
    return "ingesting"


def estimation_ready(store, batch_id: UUID, estimated_cost_usd: Decimal) -> str:
    """Record the pre-extraction estimate. Over budget → await confirmation."""
    batch = _batch(store, batch_id)
    limit = batch.get("budget_limit_usd")
    if limit is not None and estimated_cost_usd > limit:
        store.update_batch(batch_id, status="awaiting_confirmation",
                           estimated_cost_usd=estimated_cost_usd,
                           awaiting_confirmation=True)
        return "awaiting_confirmation"
    store.update_batch(batch_id, status="extracting",
                       estimated_cost_usd=estimated_cost_usd,
                       awaiting_confirmation=False)
    return "extracting"


def confirm_estimate(store, batch_id: UUID) -> str:
    """User accepted the estimate; extraction may begin."""
    batch = _batch(store, batch_id)
    if not batch.get("awaiting_confirmation"):
        raise AcqError(ErrorCode.CONFLICT, f"batch {batch_id} is not awaiting confirmation",
                       {"status": batch.get("status")})
    store.update_batch(batch_id, status="extracting", awaiting_confirmation=False)
    return "extracting"


def pause_budget(store, batch_id: UUID) -> str:
    """Budget exhausted mid-extraction: pause, do not fail (spec WP-10)."""
    _batch(store, batch_id)
    store.update_batch(batch_id, status="paused_budget")
    return "paused_budget"


def resume_batch(store, batch_id: UUID, *, new_budget_limit_usd: Decimal | None = None) -> str:
    """Resume a budget-paused batch after the budget is raised or confirmed."""
    batch = _batch(store, batch_id)
    if batch.get("status") != "paused_budget" and not batch.get("awaiting_confirmation"):
        raise AcqError(ErrorCode.CONFLICT, f"batch {batch_id} is not paused",
                       {"status": batch.get("status")})
    fields: dict = {"status": _ACTIVE_AFTER_PAUSE, "awaiting_confirmation": False}
    if new_budget_limit_usd is not None:
        fields["budget_limit_usd"] = new_budget_limit_usd
    store.update_batch(batch_id, **fields)
    return _ACTIVE_AFTER_PAUSE


def unit_finished(store, batch_id: UUID, *, failed: bool = False) -> str:
    """Account for one finished unit; when all units land, extraction is done
    and the batch moves to ``computing`` while per-property recomputes run."""
    batch = store.increment_batch_finished(batch_id, failed=failed)
    if batch is None:
        raise AcqError(ErrorCode.NOT_FOUND, f"batch {batch_id} not found")
    total = batch.get("total_count") or 0
    done = (batch.get("completed_count") or 0) + (batch.get("failed_count") or 0)
    if total > 0 and done >= total:
        status = "failed" if batch.get("completed_count") == 0 else "computing"
        store.update_batch(batch_id, status=status)
        return status
    return batch.get("status") or "extracting"


def mark_complete(store, batch_id: UUID) -> str:
    """All units extracted and their recomputes have fanned in."""
    batch = _batch(store, batch_id)
    if batch.get("status") not in ("computing", "extracting"):
        raise AcqError(ErrorCode.CONFLICT,
                       f"batch {batch_id} cannot complete from {batch.get('status')}",
                       {"status": batch.get("status")})
    store.update_batch(batch_id, status="complete")
    return "complete"


def fail(store, batch_id: UUID, reason: str) -> str:
    """Permanent failure: surfaced on the Problems page, never silent."""
    _batch(store, batch_id)
    store.update_batch(batch_id, status="failed")
    return "failed"


__all__ = ["BATCH_STATES", "start_ingestion", "estimation_ready", "confirm_estimate",
           "pause_budget", "resume_batch", "unit_finished", "mark_complete", "fail"]
