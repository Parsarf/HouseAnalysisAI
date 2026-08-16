"""WP-10 pipeline orchestration (spec §17).

``recompute_property`` stays the pure compute core (normalized record in,
contract objects out). ``Pipeline`` is the real orchestrator around it:
load facts from the ledger → normalize → underwrite → strategies + offer
grid → score → persist ``deal_scenarios`` / ``offer_scenarios`` / ``scores``
→ emit flags. All DB access goes through an injectable store factory
(``pipeline.store.SqlStore`` in production, an in-memory fake in tests), so
every operation here runs offline.

Invariants (WP-10 acceptance criteria):
- ``Pipeline.recompute`` is the single re-entry point downstream of the
  ledger. It is idempotent (delete + insert inside one transaction) and
  serialized per property via an advisory lock.
- ``Pipeline.extract_unit`` finishes one extraction unit and, when the
  per-property outstanding-unit count hits zero, triggers exactly one
  recompute (fan-in).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

from common.errors import AcqError, ErrorCode
from contracts import (
    AssumptionSet,
    NormalizedProperty,
    OfferGrid,
    ScoreSet,
    StrategyResult,
    UnderwritingResult,
)
from finance import underwrite
from normalization import resolve_facts
from scoring import score
from strategies import all_strategies, offer_grid

from . import batch as batch_machine
from .store import DEFAULT_SCORING_CONFIG_ID, UnitOutcome, sql_store_factory

log = logging.getLogger(__name__)

StoreFactory = Callable[[], AbstractContextManager]

# Extractor contract: called with the unit row dict, returns either a plain
# list[ExtractedFactDraft] or an object with .facts (and optionally .cost_usd,
# .model, .prompt_version) — WP-4's client is still landing, so both shapes
# are accepted.
Extractor = Callable[[dict], object]

EnqueueHook = Callable[[str, dict, str | None], object]

BULK_RECHUNK = 500  # spec §17: bulk recompute enqueues in chunks of 500


@dataclass(frozen=True)
class Computation:
    underwriting: UnderwritingResult
    strategies: list[StrategyResult]
    score: ScoreSet
    grid: OfferGrid


def _score_record(record: NormalizedProperty, underwriting: UnderwritingResult,
                  scoring_config_id: UUID, strategies: list[StrategyResult],
                  config: dict | None) -> ScoreSet:
    if config is None:
        return score(record, underwriting, scoring_config_id, strategies)
    try:
        return score(record, underwriting, scoring_config_id, strategies, config=config)
    except TypeError:  # scoring mid-rewrite: older signature has no config kwarg
        return score(record, underwriting, scoring_config_id, strategies)


def recompute_property(property: NormalizedProperty, assumptions: AssumptionSet,
                       scoring_config_id: UUID, purchase_price: Decimal,
                       *, config: dict | None = None) -> Computation:
    """Pure compute core: underwrite → strategies → score → offer grid."""
    underwriting_result = underwrite(property, assumptions)
    strategy_results = all_strategies(property, underwriting_result, assumptions, purchase_price)
    score_result = _score_record(property, underwriting_result, scoring_config_id,
                                 strategy_results, config)
    grid = offer_grid(underwriting_result, property.property_id, assumptions, purchase_price)
    return Computation(underwriting_result, strategy_results, score_result, grid)


def _purchase_price(record: NormalizedProperty, explicit: Decimal | None) -> Decimal:
    if explicit is not None:
        return explicit
    tracked = record.ownership.purchase_price
    if tracked is not None and tracked.value is not None:
        return tracked.value
    return Decimal(0)


def _flag_requests(record: NormalizedProperty, assumptions: AssumptionSet,
                   computation: Computation) -> list:
    from flags import collect_flags
    requests = list(collect_flags(record, assumptions))
    try:
        from strategies import short_sale_flag_requests
        requests.extend(short_sale_flag_requests(computation.grid))
    except Exception:  # strategies mid-rewrite: short-sale flags are additive only
        log.warning("short-sale flag generation failed", exc_info=True)
    return requests


def _facts_and_meta(result: object) -> tuple[list, Decimal | None, str | None, str | None]:
    facts = getattr(result, "facts", result)
    return (list(facts), getattr(result, "cost_usd", None),
            getattr(result, "model", None), getattr(result, "prompt_version", None))


class Pipeline:
    """The WP-10 orchestrator. One instance wires one store factory."""

    def __init__(self, store_factory: StoreFactory = sql_store_factory, *,
                 extractor: Extractor | None = None,
                 enqueue: EnqueueHook | None = None):
        self._store_factory = store_factory
        self._extractor = extractor
        self._enqueue_hook = enqueue

    # ------------------------------------------------------------- recompute

    def recompute(self, property_id, *, reason: str = "manual",
                  purchase_price: Decimal | None = None,
                  assumption_set_id: UUID | None = None) -> Computation:
        """Load → normalize → compute → persist → emit flags.

        Idempotent: derived rows are replaced, flags dedupe by key, and the
        whole operation is one transaction guarded by the per-property
        advisory lock.
        """
        property_id = UUID(str(property_id))
        with self._store_factory() as store:
            store.acquire_property_lock(property_id)
            if store.get_property(property_id) is None:
                raise AcqError(ErrorCode.NOT_FOUND, f"property {property_id} not found")
            facts = store.load_facts(property_id)
            assumptions = store.load_assumptions(assumption_set_id)
            scoring_config_id, config = store.active_scoring_config()
            record = resolve_facts(property_id, facts,
                                   ocr_applied=store.reports_ocr_applied(property_id))
            price = _purchase_price(record, purchase_price)
            computation = recompute_property(record, assumptions, scoring_config_id, price,
                                             config=config)
            store.replace_results(property_id, computation, purchase_price=price)
            store.persist_flags(property_id, _flag_requests(record, assumptions, computation))
            store.mark_recomputed(property_id, computation.underwriting.status)
            log.info("recomputed property %s (reason=%s, status=%s)",
                     property_id, reason, computation.underwriting.status)
            return computation

    # ------------------------------------------------------------- extract_unit

    def extract_unit(self, unit_id, *, extractor: Extractor | None = None) -> UnitOutcome:
        """Finish one extraction unit; recompute once the property's units all land.

        Budget-gated: a batch paused on budget (or whose budget the unit's
        cost would exceed) raises ``budget_paused`` so the job is retried
        after the budget is raised — the batch itself is marked
        ``paused_budget``, which is a first-class state, not a failure.
        """
        unit_id = UUID(str(unit_id))
        extract = extractor or self._extractor
        blocked: AcqError | None = None
        with self._store_factory() as store:
            unit = store.get_unit(unit_id)
            if unit is None:
                raise AcqError(ErrorCode.NOT_FOUND, f"extraction unit {unit_id} not found")
            if unit["status"] in ("queued", "running"):
                batch_id = unit["batch_id"]
                if batch_id is not None and store.batch_is_paused(batch_id):
                    blocked = AcqError(ErrorCode.BUDGET_PAUSED, "batch is paused on budget",
                                       {"batch_id": str(batch_id)})
                elif extract is None:
                    raise AcqError(ErrorCode.EXTRACTION_FAILED,
                                   "no extraction client configured for extract_unit")
                else:
                    facts, cost, model, prompt_version = _facts_and_meta(extract(unit))
                    if batch_id is not None and cost and not store.reserve_batch_budget(batch_id, cost):
                        store.update_batch(batch_id, status="paused_budget")
                        blocked = AcqError(ErrorCode.BUDGET_PAUSED,
                                           "batch budget exhausted", {"batch_id": str(batch_id)})
                    else:
                        outcome = store.finish_unit(unit_id, facts, cost_usd=cost,
                                                    model=model, prompt_version=prompt_version)
            else:
                # Retry of an already-finished unit: never re-extract or duplicate
                # facts, but still evaluate the fan-in so a crash between
                # finish_unit and recompute resumes correctly.
                outcome = store.finish_unit(unit_id, [])
        if blocked is not None:
            raise blocked
        if outcome.transitioned and outcome.batch_id is not None:
            with self._store_factory() as store:
                batch_machine.unit_finished(store, outcome.batch_id)
        if outcome.property_id is not None and outcome.outstanding == 0:
            self.recompute(outcome.property_id, reason="extraction_complete")
        return outcome

    # ------------------------------------------------------------- rank / changes / nightly

    def rank_scope(self, scope_type: str = "portfolio", scope_id=None) -> int:
        """Materialize one rankings snapshot for a scope (delegates to WP-8)."""
        with self._store_factory() as store:
            return store.rank_scope(scope_type, UUID(str(scope_id)) if scope_id else None)

    def detect_changes(self, property_id, *, before: NormalizedProperty | None = None,
                       source_report_id=None) -> list:
        """Diff the current resolved record against a previous snapshot and
        persist change_events. With no snapshot available there is nothing to
        diff against, so the job is a no-op (snapshot storage is WP-16's)."""
        property_id = UUID(str(property_id))
        with self._store_factory() as store:
            store.acquire_property_lock(property_id)
            facts = store.load_facts(property_id)
            after = resolve_facts(property_id, facts,
                                  ocr_applied=store.reports_ocr_applied(property_id))
            if before is None:
                return []
            try:
                from changes.diff import diff_properties
                events = diff_properties(before, after)
            except ImportError:  # changes mid-rewrite: fall back to the flat-record diff
                from changes import diff_records
                events = diff_records(before.model_dump(mode="json"),
                                      after.model_dump(mode="json"))
            store.persist_change_events(
                property_id, events,
                source_report_id=UUID(str(source_report_id)) if source_report_id else None)
            return events

    def nightly(self) -> dict:
        """Re-rank the portfolio. Backup/prune stay with WP-18's scripts."""
        return {"ranked": self.rank_scope("portfolio")}

    # ------------------------------------------------------------- bulk recompute

    def bulk_recompute(self, *, reason: str = "bulk", chunk_size: int = BULK_RECHUNK) -> dict:
        """Enqueue recompute_property for every active property in chunks.

        Progress lives in the settings table under ``bulk_recompute:<run_id>``
        so the API can poll it.
        """
        run_id = uuid4()
        key = f"bulk_recompute:{run_id}"
        with self._store_factory() as store:
            total = store.count_properties()
            store.put_settings(key, {"status": "running", "total": total,
                                     "enqueued": 0, "reason": reason})
        enqueued = offset = 0
        while True:
            with self._store_factory() as store:
                ids = store.list_property_ids(limit=chunk_size, offset=offset)
            if not ids:
                break
            for property_id in ids:
                self._enqueue("recompute_property",
                              {"property_id": str(property_id), "reason": reason},
                              dedupe_key=f"recompute_property:{property_id}")
            enqueued += len(ids)
            offset += chunk_size
            with self._store_factory() as store:
                store.put_settings(key, {"status": "running", "total": total,
                                         "enqueued": enqueued, "reason": reason})
        with self._store_factory() as store:
            store.put_settings(key, {"status": "complete", "total": total,
                                     "enqueued": enqueued, "reason": reason})
        return {"run_id": run_id, "total": total, "enqueued": enqueued}

    def _enqueue(self, name: str, payload: dict, dedupe_key: str | None = None):
        if self._enqueue_hook is not None:
            return self._enqueue_hook(name, payload, dedupe_key)
        from common.db import db_session
        from jobs.postgres import PostgresJobQueue
        with db_session() as session:
            return PostgresJobQueue().enqueue(session, name, json.dumps(payload), dedupe_key)


__all__ = [
    "DEFAULT_SCORING_CONFIG_ID",
    "Computation",
    "Pipeline",
    "UnitOutcome",
    "recompute_property",
]
