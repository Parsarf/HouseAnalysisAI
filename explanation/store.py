"""Session-based loaders shared by the explanation builders.

These mirror the read model in ``api.analysis`` (normalized record from the
canonical extraction or the fact ledger; default assumption set) so traces
describe exactly the inputs behind the numbers shown on the deal page. The
API layer delegates to these functions so there is one implementation.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from contracts import (
    AssumptionSet,
    EntityType,
    ExtractedFactDraft,
    NormalizedProperty,
    NullReason,
    SourceKind,
)
from db import models as dbm
from normalization import resolve_facts

log = logging.getLogger(__name__)


def load_normalized(session: Session, property_id: UUID) -> NormalizedProperty | None:
    canonical = (session.query(dbm.ReportExtraction)
                 .filter(dbm.ReportExtraction.property_id == property_id,
                         dbm.ReportExtraction.status == "complete")
                 .order_by(dbm.ReportExtraction.updated_at.desc())
                 .first())
    if canonical is not None:
        payload = (canonical.normalized_json or {}).get("property")
        if isinstance(payload, dict):
            try:
                return NormalizedProperty.model_validate(payload)
            except Exception:
                log.warning("canonical normalized record could not be loaded", exc_info=True,
                            extra={"event": "canonical_analysis_load_failed",
                                   "property_id": property_id})
    rows = (session.query(dbm.ExtractedFact)
            .filter(dbm.ExtractedFact.property_id == property_id,
                    dbm.ExtractedFact.is_active.is_(True))
            .all())
    if not rows:
        return None
    drafts = [ExtractedFactDraft(
        report_id=row.report_id or UUID(int=0), extraction_unit_id=row.extraction_unit_id or UUID(int=0),
        entity_type=EntityType(row.entity_type), entity_local_id=row.entity_local_id,
        field_path=row.field_path, value_raw=row.value_raw, value_parsed=row.value_parsed,
        value_text=row.value_text, value_date=row.value_date, value_bool=row.value_bool,
        unit=row.unit, as_of_date=row.as_of_date, page_number=row.page_number,
        snippet=row.snippet, extraction_confidence=float(row.extraction_confidence),
        null_reason=NullReason(row.null_reason) if row.null_reason else None, source_kind=SourceKind(row.source_kind),
    ) for row in rows]
    return resolve_facts(property_id, drafts)


def load_assumption_set(session: Session, assumption_set_id: UUID | None = None) -> AssumptionSet | None:
    query = session.query(dbm.AssumptionSet)
    if assumption_set_id is not None:
        row = session.get(dbm.AssumptionSet, assumption_set_id)
    else:
        row = query.filter(dbm.AssumptionSet.is_default.is_(True)).first()
        if row is None:
            row = query.first()
    if row is None:
        return None
    try:
        return AssumptionSet(id=row.id, version=row.version, name=row.name, **(row.params or {}))
    except Exception:  # noqa: BLE001 - malformed stored assumption sets fall back to None
        return None


def load_purchase_price(session: Session, property_id: UUID,
                        record: NormalizedProperty) -> Decimal:
    """The purchase price the persisted strategy rows used: latest DealScenario
    price first, then the reported ownership purchase price, else zero."""
    row = (session.query(dbm.DealScenario)
           .filter(dbm.DealScenario.property_id == property_id,
                   dbm.DealScenario.purchase_price.is_not(None))
           .order_by(dbm.DealScenario.computed_at.desc())
           .first())
    if row is not None and row.purchase_price is not None:
        return Decimal(str(row.purchase_price))
    tracked = record.ownership.purchase_price
    if tracked is not None and tracked.value is not None:
        return tracked.value
    return Decimal(0)


def load_scoring_config(session: Session) -> tuple[UUID, dict | None]:
    """Active scoring config as (id, config dict or None for in-code defaults)."""
    from scoring.ranking import ACTIVE_CONFIG_SQL

    row = session.execute(ACTIVE_CONFIG_SQL).mappings().first()
    if row is None:
        from pipeline.store import DEFAULT_SCORING_CONFIG_ID

        return DEFAULT_SCORING_CONFIG_ID, None
    distress_points = dict(row["distress_points"] or {})
    risk_points = distress_points.pop("risk", None)
    config = {
        "weights": row["weights"] or {},
        "bounds": row["bounds"] or {},
        "distress_points": distress_points,
        "gates": row["gates"] or {},
    }
    if risk_points:
        config["risk_points"] = risk_points
    return row["id"], config


def load_persisted_score(session: Session, property_id: UUID) -> dbm.Score | None:
    return (session.query(dbm.Score)
            .filter(dbm.Score.property_id == property_id)
            .order_by(dbm.Score.computed_at.desc())
            .first())
