"""SQL-backed persistence for the WP-10 orchestrator.

Every DB touch in ``pipeline/`` goes through ``SqlStore`` so the orchestrator
runs offline in tests against an in-memory fake exposing the same method
surface. Tables without ORM models (``extracted_facts``, ``deal_scenarios``,
``offer_scenarios``, ``scores``, ``assumption_sets``, ``settings``,
``change_events``) are accessed with ``text()`` SQL against ``db/schema.sql``,
mirroring the approach in ``scoring/ranking.py``.

Transaction boundaries are owned by the caller: ``sql_store_factory`` wraps
``common.db.db_session`` (commit on clean exit, rollback on exception), so a
worker crash mid-job leaves no partial rows behind.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from common.errors import AcqError, ErrorCode
from common.locks import acquire_advisory_lock
from common.serializers import json_safe
from contracts import AssumptionSet, ExtractedFactDraft
from db.models import Report
from identity import resolve_report_identity

log = logging.getLogger(__name__)

# Substituted for nullable report_id / extraction_unit_id on human-sourced
# facts (flags/workflow.py inserts those without either column populated).
NIL_UUID = UUID(int=0)

# scoring_config_id used when no scoring_configs row is active yet; the engine
# falls back to its in-code DEFAULT_CONFIG for the weights.
DEFAULT_SCORING_CONFIG_ID = UUID(int=0)

_UNIT_OUTSTANDING_SQL = text("""
    SELECT count(*) FROM extraction_units u
    JOIN reports r ON r.id = u.report_id
    WHERE r.property_id = :pid AND u.status IN ('queued', 'running')
""")

_REPORT_UNIT_OUTSTANDING_SQL = text("""
    SELECT count(*) FROM extraction_units
    WHERE report_id = :report_id AND status IN ('queued', 'running')
""")

_UNIT_SQL = text("""
    SELECT u.id, u.report_id, u.status, u.unit_type, u.page_start, u.page_end,
           u.text_path, u.token_estimate,
           r.property_id, r.batch_id
    FROM extraction_units u
    JOIN reports r ON r.id = u.report_id
    WHERE u.id = :id
""")

_FACT_INSERT_SQL = text("""
    INSERT INTO extracted_facts (id, property_id, report_id, extraction_unit_id,
        entity_type, entity_local_id, field_path, value_raw, value_parsed, value_text,
        value_date, value_bool, unit, as_of_date, page_number, snippet,
        extraction_confidence, null_reason, source_kind)
    VALUES (:id, :property_id, :report_id, :extraction_unit_id,
        :entity_type, :entity_local_id, :field_path, :value_raw, :value_parsed, :value_text,
        :value_date, :value_bool, :unit, :as_of_date, :page_number, :snippet,
        :extraction_confidence, :null_reason, :source_kind)
""")

_FACT_SELECT_SQL = text("""
    SELECT report_id, extraction_unit_id, entity_type, entity_local_id, field_path,
           value_raw, value_parsed, value_text, value_date, value_bool, unit, as_of_date,
           page_number, snippet, extraction_confidence, null_reason, source_kind
    FROM extracted_facts
    WHERE property_id = :pid AND is_active
    ORDER BY created_at, id
""")

_REPORT_FACT_SELECT_SQL = text("""
    SELECT report_id, extraction_unit_id, entity_type, entity_local_id, field_path,
           value_raw, value_parsed, value_text, value_date, value_bool, unit, as_of_date,
           page_number, snippet, extraction_confidence, null_reason, source_kind
    FROM extracted_facts
    WHERE report_id = :report_id AND is_active
    ORDER BY created_at, id
""")

_DEAL_DELETE_SQL = text("DELETE FROM deal_scenarios WHERE property_id = :pid")
_DEAL_INSERT_SQL = text("""
    INSERT INTO deal_scenarios (id, property_id, strategy, scenario, assumption_set_id,
        engine_version, purchase_price, arv, repairs, holding, financing, resale,
        all_in_basis, profit, roi, margin_of_safety, cap_rate, cash_flow, coc, mao,
        status, unavailable_reason, computed_at)
    VALUES (:id, :pid, :strategy, :scenario, :asid, :engine, :price, :arv, :repairs,
        :holding, :financing, :resale, :basis, :profit, :roi, :mos, :cap_rate,
        :cash_flow, :coc, :mao, :status, :reason, :computed_at)
""")

_OFFER_DELETE_SQL = text("DELETE FROM offer_scenarios WHERE property_id = :pid")
_OFFER_INSERT_SQL = text("""
    INSERT INTO offer_scenarios (id, property_id, offer_price, scenario, confirmed_payoffs,
        potential_payoffs, closing_costs, proceeds_low, proceeds_expected, proceeds_high,
        buyer_basis, profit, roi, is_short_sale)
    VALUES (:id, :pid, :offer, :scenario, :confirmed, :potential, :closing, :low,
        :expected, :high, :basis, :profit, :roi, :short_sale)
""")

_SCORE_DELETE_SQL = text(
    "DELETE FROM scores WHERE property_id = :pid AND scoring_config_id = :cid")
_DEFAULT_SCORE_CONFIG_SQL = text("""
    INSERT INTO scoring_configs
        (id, weights, bounds, distress_points, gates, version, is_active)
    VALUES (:id, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, 0, false)
    ON CONFLICT (id) DO NOTHING
""")
_SCORE_INSERT_SQL = text("""
    INSERT INTO scores (id, property_id, scoring_config_id, fos, distress,
        data_confidence, risk, overall, components, gates_applied, computed_at)
    VALUES (:id, :pid, :cid, :fos, :distress, :dcs, :risk, :overall,
        CAST(:components AS jsonb), CAST(:gates AS text[]), :computed_at)
""")

_CHANGE_INSERT_SQL = text("""
    INSERT INTO change_events (id, property_id, change_type, field_path, old_value,
        new_value, source_report_id, score_delta, detected_at)
    VALUES (:id, :pid, :change_type, :field_path, CAST(:old_value AS jsonb),
        CAST(:new_value AS jsonb), :source_report_id, :score_delta, :detected_at)
""")

_SETTINGS_UPSERT_SQL = text("""
    INSERT INTO settings (key, value) VALUES (:key, CAST(:value AS jsonb))
    ON CONFLICT (key) DO UPDATE SET value = CAST(:value AS jsonb)
""")

# Columns the batch state machine is allowed to drive (spec WP-10).
_BATCH_FIELDS = frozenset({
    "status", "file_count", "total_count", "completed_count", "failed_count",
    "estimated_cost_usd", "actual_cost_usd", "budget_limit_usd", "spent_usd",
    "awaiting_confirmation",
})


@dataclass(frozen=True)
class UnitOutcome:
    """Result of finishing one extraction unit (fan-in decision input)."""
    unit_id: UUID
    property_id: UUID | None
    batch_id: UUID | None
    outstanding: int | None
    transitioned: bool  # True when this call moved the unit to 'extracted'
    report_outstanding: int | None = None
    identity_evidence: dict | None = None
    property_created: bool = False
    report_attached: bool = False
    facts_attached: int = 0
    identity_unresolved: bool = False


def _fact_row(property_id: UUID | None, report_id: UUID | None, unit_id: UUID | None,
              fact: ExtractedFactDraft) -> dict[str, Any]:
    return {
        "id": uuid4(), "property_id": property_id,
        "report_id": fact.report_id or report_id or NIL_UUID,
        "extraction_unit_id": fact.extraction_unit_id or unit_id or NIL_UUID,
        "entity_type": fact.entity_type.value, "entity_local_id": fact.entity_local_id,
        "field_path": fact.field_path, "value_raw": fact.value_raw,
        "value_parsed": fact.value_parsed, "value_text": fact.value_text,
        "value_date": fact.value_date, "value_bool": fact.value_bool,
        "unit": fact.unit, "as_of_date": fact.as_of_date,
        "page_number": fact.page_number, "snippet": fact.snippet,
        "extraction_confidence": fact.extraction_confidence,
        "null_reason": fact.null_reason.value if fact.null_reason else None,
        "source_kind": fact.source_kind.value,
    }


def _fact_from_row(row: Any) -> ExtractedFactDraft:
    return ExtractedFactDraft(
        report_id=row["report_id"] or NIL_UUID,
        extraction_unit_id=row["extraction_unit_id"] or NIL_UUID,
        entity_type=row["entity_type"], entity_local_id=row["entity_local_id"],
        field_path=row["field_path"], value_raw=row["value_raw"],
        value_parsed=row["value_parsed"], value_text=row["value_text"],
        value_date=row["value_date"], value_bool=row["value_bool"],
        unit=row["unit"], as_of_date=row["as_of_date"],
        page_number=row["page_number"], snippet=row["snippet"],
        extraction_confidence=float(row["extraction_confidence"]),
        null_reason=row["null_reason"], source_kind=row["source_kind"],
    )


class SqlStore:
    """Production store: every method is one logical DB operation on ``session``."""

    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------ locking

    def acquire_property_lock(self, property_id: UUID) -> None:
        """Serialize all downstream-of-ledger work for one property (WP-10)."""
        acquire_advisory_lock(self.session, f"pipeline:recompute:{property_id}")

    # ------------------------------------------------------------------ reads

    def get_property(self, property_id: UUID) -> dict | None:
        row = self.session.execute(
            text("SELECT id, pipeline_status FROM properties WHERE id = :id"),
            {"id": property_id}).mappings().first()
        return dict(row) if row else None

    def load_facts(self, property_id: UUID) -> list[ExtractedFactDraft]:
        rows = self.session.execute(_FACT_SELECT_SQL, {"pid": property_id}).mappings().all()
        return [_fact_from_row(row) for row in rows]

    def reports_ocr_applied(self, property_id: UUID) -> bool:
        return bool(self.session.execute(
            text("SELECT COALESCE(bool_or(ocr_applied), false) FROM reports WHERE property_id = :pid"),
            {"pid": property_id}).scalar())

    def load_assumptions(self, assumption_set_id: UUID | None = None) -> AssumptionSet:
        if assumption_set_id is not None:
            row = self.session.execute(
                text("SELECT id, name, version, params FROM assumption_sets WHERE id = :id"),
                {"id": assumption_set_id}).mappings().first()
        else:
            row = self.session.execute(
                text("SELECT id, name, version, params FROM assumption_sets "
                     "ORDER BY is_default DESC, version DESC LIMIT 1")).mappings().first()
        if row is None:
            raise AcqError(ErrorCode.NOT_FOUND, "no assumption set configured")
        params = dict(row["params"] or {})
        return AssumptionSet.model_validate(
            {**params, "id": str(row["id"]), "version": row["version"], "name": row["name"]})

    def active_scoring_config(self) -> tuple[UUID, dict | None]:
        """(scoring_config_id, config-dict) for scoring.score; defaults when unconfigured."""
        try:
            from scoring import load_active_scoring_config
            result = load_active_scoring_config(self.session)
        except Exception as exc:
            from sqlalchemy.exc import ProgrammingError
            if not isinstance(exc, ProgrammingError):
                raise
            log.warning("active scoring config unavailable; using engine defaults", exc_info=True)
            return DEFAULT_SCORING_CONFIG_ID, None
        if result is None:
            return DEFAULT_SCORING_CONFIG_ID, None
        return result

    # ------------------------------------------------------------------ recompute persistence

    def replace_results(self, property_id: UUID, computation, *, purchase_price: Decimal) -> None:
        """Delete + insert derived rows so recompute is idempotent (WP-10 AC #3)."""
        now = datetime.now(UTC)
        underwriting = computation.underwriting
        self.session.execute(_DEAL_DELETE_SQL, {"pid": property_id})
        for result in computation.strategies:
            costs = underwriting.costs.get(result.scenario)
            metrics = result.metrics or {}
            self.session.execute(_DEAL_INSERT_SQL, {
                "id": uuid4(), "pid": property_id,
                "strategy": result.strategy.value, "scenario": result.scenario.value,
                "asid": underwriting.assumption_set_id, "engine": underwriting.engine_version,
                "price": purchase_price,
                "arv": underwriting.value.arv_by_scenario.get(result.scenario),
                "repairs": costs.repairs if costs else None,
                "holding": costs.holding if costs else None,
                "financing": metrics.get("financing", costs.financing if costs else None),
                "resale": costs.resale if costs else None,
                "basis": result.all_in_basis, "profit": result.profit, "roi": result.roi,
                "mos": result.margin_of_safety, "cap_rate": metrics.get("cap_rate"),
                "cash_flow": metrics.get("cash_flow"), "coc": metrics.get("coc"),
                "mao": result.mao, "status": result.status,
                "reason": result.unavailable_reason, "computed_at": now})
        self.session.execute(_OFFER_DELETE_SQL, {"pid": property_id})
        for point in computation.grid.points:
            self.session.execute(_OFFER_INSERT_SQL, {
                "id": uuid4(), "pid": property_id, "offer": point.offer_price,
                "scenario": point.scenario.value, "confirmed": point.confirmed_payoffs,
                "potential": point.potential_payoffs, "closing": point.closing_costs,
                "low": point.proceeds_low, "expected": point.proceeds_expected,
                "high": point.proceeds_high, "basis": point.buyer_basis,
                "profit": point.profit, "roi": point.roi, "short_sale": point.is_short_sale})
        score = computation.score
        if score.scoring_config_id == DEFAULT_SCORING_CONFIG_ID:
            # The in-code scoring defaults still need a real FK target. Clean
            # databases have no active scoring_configs row yet, so create one
            # stable inactive sentinel in the same recompute transaction.
            self.session.execute(
                _DEFAULT_SCORE_CONFIG_SQL, {"id": DEFAULT_SCORING_CONFIG_ID},
            )
        self.session.execute(_SCORE_DELETE_SQL, {"pid": property_id, "cid": score.scoring_config_id})
        self.session.execute(_SCORE_INSERT_SQL, {
            "id": uuid4(), "pid": property_id, "cid": score.scoring_config_id,
            "fos": score.fos, "distress": score.distress, "dcs": score.data_confidence,
            "risk": score.risk, "overall": score.overall,
            "components": json.dumps(json_safe(score.components)),
            "gates": list(score.gates_applied), "computed_at": now})

    def persist_flags(self, property_id: UUID, requests, *, reconcile: bool = True) -> int:
        """Persist findings, reconciling only when the request set is complete.

        Identity resolution can emit a partial flag set before the full property
        recompute, so that path remains append-only.
        """
        from flags import persist_flags, sync_flags
        if not reconcile:
            return len(persist_flags(self.session, requests))
        if not requests:
            # An empty current finding set still matters: close stale open
            # findings after a successful recompute.
            return len(sync_flags(self.session, property_id, []))
        return len(sync_flags(self.session, property_id, requests))

    def mark_recomputed(self, property_id: UUID, underwriting_status: str) -> None:
        # SAVEPOINT: on databases created from the ORM metadata these columns do
        # not exist, and a failed statement would abort the whole transaction.
        try:
            with self.session.begin_nested():
                self.session.execute(
                    text("UPDATE properties SET last_recomputed_at = now(), "
                         "underwriting_status = :status, pipeline_status = 'analyzed', "
                         "updated_at = now() WHERE id = :pid"),
                    {"status": underwriting_status, "pid": property_id})
        except Exception:
            log.warning("properties recompute markers unavailable", exc_info=True)

    # ------------------------------------------------------------------ extraction fan-in

    def get_unit(self, unit_id: UUID) -> dict | None:
        row = self.session.execute(_UNIT_SQL, {"id": unit_id}).mappings().first()
        return dict(row) if row else None

    def finish_unit(self, unit_id: UUID, facts: list[ExtractedFactDraft], *,
                    cost_usd: Decimal | None = None, model: str | None = None,
                    prompt_version: str | None = None) -> UnitOutcome:
        """Mark a unit extracted, persist its facts, and count outstanding units.

        The per-property row lock (``SELECT ... FOR UPDATE`` on the property)
        serializes concurrent finishers, so exactly one of them observes an
        outstanding count of zero and triggers the recompute (WP-10 fan-in).
        """
        unit = self.get_unit(unit_id)
        if unit is None:
            raise AcqError(ErrorCode.NOT_FOUND, f"extraction unit {unit_id} not found")
        property_id = unit["property_id"]
        evidence = None
        property_created = False
        report_attached = False
        report = self.session.get(Report, unit["report_id"])
        if report is None:
            raise AcqError(ErrorCode.NOT_FOUND, f"report {unit['report_id']} not found")
        if property_id is None:
            prior_rows = self.session.execute(
                _REPORT_FACT_SELECT_SQL, {"report_id": unit["report_id"]}
            ).mappings().all()
            prior_facts = [_fact_from_row(row) for row in prior_rows]
            property_row, evidence, property_created = resolve_report_identity(
                self.session, report, [*prior_facts, *facts]
            )
            if property_row is not None:
                property_id = property_row.id
                report_attached = True
                self.persist_flags(property_id, getattr(property_row, "identity_flags", []), reconcile=False)
        if property_id is not None:
            self.session.execute(
                text("SELECT id FROM properties WHERE id = :pid FOR UPDATE"),
                {"pid": property_id})
        transitioned = unit["status"] in ("queued", "running")
        if transitioned:
            self.session.execute(
                text("UPDATE extraction_units SET status = 'extracted', cost_usd = :cost, "
                     "model = :model, prompt_version = :pv, updated_at = now() WHERE id = :id"),
                {"cost": cost_usd, "model": model, "pv": prompt_version, "id": unit_id})
            for fact in facts:
                self.session.execute(
                    _FACT_INSERT_SQL, _fact_row(property_id, unit["report_id"], unit_id, fact))
        report_outstanding = int(self.session.execute(
            _REPORT_UNIT_OUTSTANDING_SQL, {"report_id": unit["report_id"]}).scalar())
        identity_unresolved = report_outstanding == 0 and property_id is None
        if property_id is not None:
            self.session.execute(
                text("UPDATE extracted_facts SET property_id = :pid "
                     "WHERE report_id = :report_id AND property_id IS DISTINCT FROM :pid"),
                {"pid": property_id, "report_id": unit["report_id"]},
            )
        if report_outstanding == 0:
            self.session.execute(
                text("UPDATE reports SET status = :status, failure_reason = :reason, "
                     "updated_at = now() WHERE id = :id"),
                {
                    "id": unit["report_id"],
                    "status": "failed" if identity_unresolved else "extracted",
                    "reason": ErrorCode.IDENTITY_UNRESOLVED.value if identity_unresolved else None,
                },
            )
        outstanding = None
        if property_id is not None:
            outstanding = int(self.session.execute(
                _UNIT_OUTSTANDING_SQL, {"pid": property_id}).scalar())
        facts_attached = 0
        if property_id is not None:
            facts_attached = int(self.session.execute(
                text("SELECT count(*) FROM extracted_facts "
                     "WHERE report_id = :report_id AND property_id = :pid AND is_active"),
                {"report_id": unit["report_id"], "pid": property_id},
            ).scalar())
        return UnitOutcome(
            unit_id=unit_id,
            property_id=property_id,
            batch_id=unit["batch_id"],
            outstanding=outstanding,
            transitioned=transitioned,
            report_outstanding=report_outstanding,
            identity_evidence=asdict(evidence) if evidence is not None else None,
            property_created=property_created,
            report_attached=report_attached,
            facts_attached=facts_attached,
            identity_unresolved=identity_unresolved,
        )

    def fail_unit(self, unit_id: UUID, reason: str) -> None:
        self.session.execute(
            text("UPDATE extraction_units SET status = 'failed', updated_at = now() WHERE id = :id"),
            {"id": unit_id})

    # ------------------------------------------------------------------ batches

    def get_batch(self, batch_id: UUID) -> dict | None:
        row = self.session.execute(
            text("SELECT * FROM batches WHERE id = :id"), {"id": batch_id}).mappings().first()
        return dict(row) if row else None

    def update_batch(self, batch_id: UUID, **fields) -> None:
        unknown = set(fields) - _BATCH_FIELDS
        if unknown:
            raise AcqError(ErrorCode.INVALID_INPUT, f"unknown batch fields {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = :{name}" for name in sorted(fields))
        self.session.execute(
            text(f"UPDATE batches SET {assignments}, updated_at = now() WHERE id = :id"),
            {**fields, "id": batch_id})

    def batch_is_paused(self, batch_id: UUID) -> bool:
        batch = self.get_batch(batch_id)
        return bool(batch and (batch["status"] == "paused_budget" or batch["awaiting_confirmation"]))

    def reserve_batch_budget(self, batch_id: UUID, amount: Decimal) -> bool:
        from ops.db_budget import reserve_budget
        return reserve_budget(self.session, batch_id, amount)

    def increment_batch_finished(self, batch_id: UUID, *, failed: bool = False) -> dict | None:
        self.session.execute(
            text("UPDATE batches SET completed_count = completed_count + :completed, "
                 "failed_count = failed_count + :failed, updated_at = now() WHERE id = :id"),
            {"completed": 0 if failed else 1, "failed": 1 if failed else 0, "id": batch_id})
        return self.get_batch(batch_id)

    def batch_results(self, batch_id: UUID) -> dict:
        property_ids = list(self.session.execute(
            text("SELECT DISTINCT property_id FROM reports "
                 "WHERE batch_id = :batch_id AND property_id IS NOT NULL ORDER BY property_id"),
            {"batch_id": batch_id},
        ).scalars().all())
        unresolved = self.session.execute(
            text("SELECT id, failure_reason FROM reports "
                 "WHERE batch_id = :batch_id AND property_id IS NULL "
                 "AND failure_reason = :reason ORDER BY id"),
            {"batch_id": batch_id, "reason": ErrorCode.IDENTITY_UNRESOLVED.value},
        ).mappings().all()
        return {
            "property_ids": property_ids,
            "unresolved_reports": [
                {"report_id": row["id"], "reason": row["failure_reason"]} for row in unresolved
            ],
        }

    # ------------------------------------------------------------------ rankings / bulk

    def rank_scope(self, scope_type: str, scope_id: UUID | None = None) -> int:
        from scoring import rank_scope
        return rank_scope(self.session, scope_type, scope_id)

    def count_properties(self) -> int:
        return int(self.session.execute(
            text("SELECT count(*) FROM properties WHERE merged_into_id IS NULL")).scalar())

    def list_property_ids(self, *, limit: int, offset: int) -> list[UUID]:
        return list(self.session.execute(
            text("SELECT id FROM properties WHERE merged_into_id IS NULL "
                 "ORDER BY id LIMIT :limit OFFSET :offset"),
            {"limit": limit, "offset": offset}).scalars().all())

    def put_settings(self, key: str, value: dict) -> None:
        self.session.execute(_SETTINGS_UPSERT_SQL,
                             {"key": key, "value": json.dumps(json_safe(value))})

    def get_settings(self, key: str) -> dict | None:
        row = self.session.execute(
            text("SELECT value FROM settings WHERE key = :key"), {"key": key}).scalar()
        return dict(row) if row else None

    # ------------------------------------------------------------------ change detection

    def persist_change_events(self, property_id: UUID, events, *,
                              source_report_id: UUID | None = None) -> int:
        now = datetime.now(UTC)
        for event in events:
            self.session.execute(_CHANGE_INSERT_SQL, {
                "id": uuid4(), "pid": property_id,
                "change_type": str(event.change_type), "field_path": event.field_path,
                "old_value": json.dumps(json_safe(event.old_value), default=str),
                "new_value": json.dumps(json_safe(event.new_value), default=str),
                "source_report_id": source_report_id, "score_delta": event.score_delta,
                "detected_at": now})
        return len(events)


@contextmanager
def sql_store_factory() -> Iterator[SqlStore]:
    """Production store factory: one transaction per pipeline operation."""
    from common.db import db_session  # lazy: importing common.db creates the engine
    with db_session() as session:
        yield SqlStore(session)
