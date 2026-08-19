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
from scoring import data_confidence, score
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
    return score(record, underwriting, scoring_config_id, strategies, config=config)


def recompute_property(property: NormalizedProperty, assumptions: AssumptionSet,
                       scoring_config_id: UUID, purchase_price: Decimal,
                       *, config: dict | None = None) -> Computation:
    """Pure compute core: underwrite → strategies → score → offer grid."""
    underwriting_result = underwrite(property, assumptions)
    dcs = data_confidence(property, config=config)
    wholesale_min = Decimal(str((config or {}).get("gates", {}).get("wholesale_min", 60)))
    strategy_results = all_strategies(property, underwriting_result, assumptions, purchase_price,
                                      data_confidence_value=dcs, wholesale_min=wholesale_min)
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
    from strategies import short_sale_flag_requests
    requests.extend(short_sale_flag_requests(computation.grid))
    return requests


def _facts_and_meta(result: object) -> tuple[list, Decimal | None, str | None, str | None]:
    facts = getattr(result, "facts", result)
    fact_list = list(facts) if isinstance(facts, (list, tuple)) else []
    return (fact_list, getattr(result, "cost_usd", None),
            getattr(result, "model", None), getattr(result, "prompt_version", None))


def _finish_unit_with_trace(store, unit_id: UUID, facts: list, context: dict, **metadata):
    try:
        return store.finish_unit(unit_id, facts, **metadata)
    except Exception as exc:
        log.exception("extracted facts persistence failed", extra={
            **context,
            "event": "analysis_stage_failed",
            "stage": "facts_persisted",
            "success": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        })
        raise


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
                  assumption_set_id: UUID | None = None,
                  trace_context: dict | None = None) -> Computation:
        """Load → normalize → compute → persist → emit flags.

        Idempotent: derived rows are replaced, flags dedupe by key, and the
        whole operation is one transaction guarded by the per-property
        advisory lock.
        """
        property_id = UUID(str(property_id))
        context = {"property_id": property_id, **(trace_context or {})}
        log.info("property recompute started", extra={
            **context,
            "event": "property_recompute_started",
            "stage": "property_recompute",
            "success": True,
            "reason": reason,
        })
        with self._store_factory() as store:
            store.acquire_property_lock(property_id)
            if store.get_property(property_id) is None:
                raise AcqError(ErrorCode.NOT_FOUND, f"property {property_id} not found")
            facts = store.load_facts(property_id)
            assumptions = store.load_assumptions(assumption_set_id)
            scoring_config_id, config = store.active_scoring_config()
            try:
                record = resolve_facts(
                    property_id, facts,
                    ocr_applied=store.reports_ocr_applied(property_id),
                )
            except Exception as exc:
                log.exception("property normalization failed", extra={
                    **context,
                    "event": "analysis_stage_failed",
                    "stage": "normalization_validation",
                    "success": False,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                })
                raise
            log.info("property normalization completed", extra={
                **context,
                "event": "analysis_stage_completed",
                "stage": "normalization_validation",
                "success": True,
            })
            price = _purchase_price(record, purchase_price)
            try:
                computation = recompute_property(record, assumptions, scoring_config_id, price,
                                                 config=config)
            except Exception as exc:
                log.exception("financial, scoring, or strategy analysis failed", extra={
                    **context,
                    "event": "analysis_stage_failed",
                    "stage": "financial_scoring_strategy",
                    "success": False,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                })
                raise
            for stage in ("financial_calculations", "scoring", "strategy_ranking"):
                log.info("analysis stage completed", extra={
                    **context,
                    "event": "analysis_stage_completed",
                    "stage": stage,
                    "success": True,
                })
            try:
                store.replace_results(property_id, computation, purchase_price=price)
                store.persist_flags(property_id, _flag_requests(record, assumptions, computation))
                store.mark_recomputed(property_id, computation.underwriting.status)
            except Exception as exc:
                log.exception("analysis persistence failed", extra={
                    **context,
                    "event": "analysis_stage_failed",
                    "stage": "analysis_persistence",
                    "success": False,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                })
                raise
            log.info("recomputed property %s (reason=%s, status=%s)",
                     property_id, reason, computation.underwriting.status, extra={
                         **context,
                         "event": "analysis_completed",
                         "stage": "analysis_persistence",
                         "success": True,
                     })
            log.info("property recompute completed", extra={
                **context,
                "event": "property_recompute_completed",
                "stage": "property_recompute",
                "success": True,
                "underwriting_status": computation.underwriting.status,
            })
            return computation

    def compute_normalized(
        self, record: NormalizedProperty, *, reason: str = "whole_pdf_analysis",
        purchase_price: Decimal | None = None,
        assumption_set_id: UUID | None = None,
        trace_context: dict | None = None,
    ) -> Computation:
        """Compute directly from a validated canonical record.

        The whole-PDF path uses this boundary so calculations do not depend on
        exploding canonical JSON into the legacy extracted-fact ledger first.
        """
        property_id = UUID(str(record.property_id))
        context = {"property_id": property_id, **(trace_context or {})}
        log.info("property recompute started", extra={
            **context,
            "event": "property_recompute_started",
            "stage": "property_recompute",
            "success": True,
            "reason": reason,
        })
        with self._store_factory() as store:
            store.acquire_property_lock(property_id)
            if store.get_property(property_id) is None:
                raise AcqError(ErrorCode.NOT_FOUND, f"property {property_id} not found")
            assumptions = store.load_assumptions(assumption_set_id)
            scoring_config_id, config = store.active_scoring_config()
            price = _purchase_price(record, purchase_price)
            from report_analysis.normalizer import underwrite_canonical
            underwriting_result = underwrite_canonical(record, assumptions)
            if underwriting_result.status == "ok":
                dcs = data_confidence(record, config=config)
                wholesale_min = Decimal(str((config or {}).get("gates", {}).get("wholesale_min", 60)))
                strategy_results = all_strategies(
                    record, underwriting_result, assumptions, price,
                    data_confidence_value=dcs, wholesale_min=wholesale_min,
                )
                grid = offer_grid(
                    underwriting_result, record.property_id, assumptions, price,
                )
            else:
                strategy_results = []
                grid = OfferGrid(property_id=record.property_id, points=[])
            score_result = _score_record(
                record, underwriting_result, scoring_config_id, strategy_results, config,
            )
            computation = Computation(
                underwriting_result, strategy_results, score_result, grid,
            )
            for stage in ("financial_calculations", "scoring", "strategy_ranking"):
                log.info("analysis stage completed", extra={
                    **context,
                    "event": "analysis_stage_completed",
                    "stage": stage,
                    "success": True,
                })
            store.replace_results(property_id, computation, purchase_price=price)
            store.persist_flags(property_id, _flag_requests(record, assumptions, computation))
            store.mark_recomputed(property_id, computation.underwriting.status)
            log.info("property recompute completed", extra={
                **context,
                "event": "property_recompute_completed",
                "stage": "property_recompute",
                "success": True,
                "underwriting_status": computation.underwriting.status,
            })
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
        facts: list = []
        with self._store_factory() as store:
            unit = store.get_unit(unit_id)
            if unit is None:
                raise AcqError(ErrorCode.NOT_FOUND, f"extraction unit {unit_id} not found")
            trace_context = {
                "batch_id": unit.get("batch_id"),
                "report_id": unit.get("report_id"),
                "unit_id": unit_id,
            }
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
                        outcome = _finish_unit_with_trace(
                            store, unit_id, facts, trace_context, cost_usd=cost,
                            model=model, prompt_version=prompt_version,
                        )
            else:
                # Retry of an already-finished unit: never re-extract or duplicate
                # facts, but still evaluate the fan-in so a crash between
                # finish_unit and recompute resumes correctly.
                outcome = _finish_unit_with_trace(store, unit_id, [], trace_context)
        if blocked is not None:
            raise blocked
        log.info("extracted facts persisted", extra={
            **trace_context,
            "event": "analysis_stage_completed",
            "stage": "facts_persisted",
            "success": True,
            "fact_count": len(facts) if outcome.transitioned else 0,
            "outstanding_units": outcome.outstanding,
            "transitioned": outcome.transitioned,
        })
        if outcome.identity_evidence is not None:
            evidence = outcome.identity_evidence
            log.info("property identity evidence found", extra={
                **trace_context,
                "event": "property_identity_evidence_found",
                "address": evidence.get("address"),
                "apn": evidence.get("apn"),
                "confidence": evidence.get("confidence"),
                "source": evidence.get("source_kind"),
            })
        if outcome.report_attached and outcome.property_id is not None:
            linked_context = {**trace_context, "property_id": outcome.property_id}
            log.info("property resolved from extracted evidence", extra={
                **linked_context,
                "event": "property_resolved",
                "property_created": outcome.property_created,
            })
            log.info("report attached to property", extra={
                **linked_context,
                "event": "report_attached_to_property",
            })
            log.info("extracted facts attached to property", extra={
                **linked_context,
                "event": "facts_attached_to_property",
                "fact_count": outcome.facts_attached,
            })
        if outcome.identity_unresolved:
            log.warning("property identity could not be resolved", extra={
                **trace_context,
                "event": "property_identity_unresolved",
                "stage": "property_identity",
                "success": False,
                "reason": "property identity could not be resolved",
                "preserved_fact_count": len(facts) if outcome.transitioned else 0,
            })
        if outcome.transitioned and outcome.batch_id is not None:
            with self._store_factory() as store:
                batch_status = batch_machine.unit_finished(
                    store, outcome.batch_id, failed=outcome.identity_unresolved,
                )
                batch = store.get_batch(outcome.batch_id)
                log.info("batch extraction progress updated", extra={
                    **trace_context,
                    "event": "batch_status_changed",
                    "stage": "batch_refresh",
                    "success": True,
                    "batch_status_after": batch_status,
                    "total_count": batch.get("total_count") if batch else None,
                    "completed_count": batch.get("completed_count") if batch else None,
                    "failed_count": batch.get("failed_count") if batch else None,
                })
        if outcome.property_id is not None and outcome.outstanding == 0:
            self.recompute(
                outcome.property_id, reason="extraction_complete", trace_context=trace_context,
            )
        if outcome.batch_id is not None:
            with self._store_factory() as store:
                batch = store.get_batch(outcome.batch_id)
                if batch is not None and batch.get("status") == "computing":
                    results = store.batch_results(outcome.batch_id)
                    try:
                        if results["unresolved_reports"] or not results["property_ids"]:
                            batch_machine.fail(
                                store, outcome.batch_id,
                                "property identity could not be resolved",
                            )
                        else:
                            batch_machine.mark_complete(store, outcome.batch_id)
                        final_batch = store.get_batch(outcome.batch_id)
                    except Exception as exc:
                        log.exception("final extraction fan-in failed", extra={
                            **trace_context,
                            "event": "analysis_stage_failed",
                            "stage": "final_fan_in",
                            "success": False,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                        })
                        raise
                    if final_batch and final_batch.get("status") == "complete":
                        log.info("final extraction fan-in completed", extra={
                            **trace_context,
                            "event": "analysis_stage_completed",
                            "stage": "final_fan_in",
                            "success": True,
                        })
                        log.info("batch results ready", extra={
                            **trace_context,
                            "event": "batch_results_ready",
                            "property_ids": [str(value) for value in results["property_ids"]],
                            "count": len(results["property_ids"]),
                        })
                        log.info("batch marked complete", extra={
                            **trace_context,
                            "event": "batch_completed",
                            "stage": "final_fan_in",
                            "success": True,
                            "final_status": final_batch.get("status"),
                            "total_count": final_batch.get("total_count"),
                            "completed_count": final_batch.get("completed_count"),
                            "failed_count": final_batch.get("failed_count"),
                        })
                    else:
                        log.warning("batch has no resolvable property identity", extra={
                            **trace_context,
                            "event": "batch_results_unresolved",
                            "stage": "final_fan_in",
                            "success": False,
                            "reason": "property identity could not be resolved",
                            "unresolved_report_ids": [
                                str(value["report_id"])
                                for value in results["unresolved_reports"]
                            ],
                        })
        return outcome

    # ------------------------------------------------------------- rank / changes / nightly

    def rank_scope(self, scope_type: str = "portfolio", scope_id=None) -> int:
        """Materialize one rankings snapshot for a scope (delegates to WP-8)."""
        with self._store_factory() as store:
            ranked = store.rank_scope(scope_type, UUID(str(scope_id)) if scope_id else None)
            log.info("ranking scope completed", extra={
                "event": "analysis_stage_completed",
                "stage": "strategy_ranking",
                "success": True,
                "unit_count": ranked,
            })
            return ranked

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
            from changes.diff import diff_properties
            events = diff_properties(before, after)
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
            return self._enqueue_hook(name, payload, dedupe_key or f"{name}:{payload.get('property_id', '')}")
        from common.db import db_session
        from jobs.postgres import PostgresJobQueue
        with db_session() as session:
            return PostgresJobQueue().enqueue(session, name, json.dumps(payload), dedupe_key or f"{name}:{payload.get('property_id', '')}")


__all__ = [
    "DEFAULT_SCORING_CONFIG_ID",
    "Computation",
    "Pipeline",
    "UnitOutcome",
    "recompute_property",
]
