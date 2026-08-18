"""Assemble the persisted deal-page payload (WP-11, spec §16).

Everything is read from the tables WP-10 persists to — ``deal_scenarios``,
``offer_scenarios``, ``scores``, ``flags`` — plus the ``extracted_facts``
ledger, which ``normalization.resolve_facts`` collapses back into the
``NormalizedProperty`` record. Underwriting has no table of its own, so it is
recomputed on read from the normalized record with the default assumption set
(the same inputs WP-10 persists scenarios from).
"""
import logging
from datetime import date
from uuid import UUID

from sqlalchemy.orm import Session

from contracts import (
    AssumptionSet,
    ExtractedFactDraft,
    NormalizedProperty,
    OfferGrid,
    Scenario,
    UnderwritingResult,
)
from db import models as dbm
from normalization import resolve_facts

from . import serializers

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
                # Preserve backward compatibility with the fact ledger if a
                # future schema version cannot be read by this application.
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
        report_id=row.report_id, extraction_unit_id=row.extraction_unit_id,
        entity_type=row.entity_type, entity_local_id=row.entity_local_id,
        field_path=row.field_path, value_raw=row.value_raw, value_parsed=row.value_parsed,
        value_text=row.value_text, value_date=row.value_date, value_bool=row.value_bool,
        unit=row.unit, as_of_date=row.as_of_date, page_number=row.page_number,
        snippet=row.snippet, extraction_confidence=float(row.extraction_confidence),
        null_reason=row.null_reason, source_kind=row.source_kind,
    ) for row in rows]
    return resolve_facts(property_id, drafts)


def load_assumption_set(session: Session, assumption_set_id: UUID | None = None) -> AssumptionSet | None:
    """Default assumption set as a contract object; None when none is configured."""
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
    except Exception:
        return None


def load_underwriting(session: Session, property_id: UUID,
                      normalized: NormalizedProperty | None) -> UnderwritingResult | None:
    if normalized is None:
        return None
    assumptions = load_assumption_set(session)
    if assumptions is None:
        return None
    canonical = (session.query(dbm.ReportExtraction.id)
                 .filter(dbm.ReportExtraction.property_id == property_id,
                         dbm.ReportExtraction.status == "complete")
                 .first())
    if canonical is not None:
        from report_analysis.normalizer import underwrite_canonical
        return underwrite_canonical(normalized, assumptions)
    from finance import underwrite  # lazy: finance imports contracts either way
    return underwrite(normalized, assumptions)


def load_strategies(session: Session, property_id: UUID):
    rows = (session.query(dbm.DealScenario)
            .filter(dbm.DealScenario.property_id == property_id)
            .order_by(dbm.DealScenario.strategy, dbm.DealScenario.scenario)
            .all())
    return [serializers.strategy_result(row) for row in rows]


def load_offers(session: Session, property_id: UUID, scenario: Scenario,
                underwriting: UnderwritingResult | None) -> OfferGrid | None:
    rows = (session.query(dbm.OfferScenario)
            .filter(dbm.OfferScenario.property_id == property_id,
                    dbm.OfferScenario.scenario == scenario.value)
            .order_by(dbm.OfferScenario.offer_price)
            .all())
    if rows:
        return OfferGrid(property_id=property_id, points=[serializers.offer_point(row) for row in rows])
    if underwriting is None or underwriting.status != "ok":
        return None
    assumptions = load_assumption_set(session)
    if assumptions is None:
        return None
    from strategies import offer_grid  # lazy: strategies sits downstream of finance

    center = underwriting.value.v_expected
    if center is None:
        return None
    grid = offer_grid(underwriting, property_id, assumptions, center)
    return OfferGrid(property_id=property_id,
                     points=[point for point in grid.points if point.scenario == scenario],
                     interpolatable=grid.interpolatable)


def load_scores(session: Session, property_id: UUID):
    row = (session.query(dbm.Score)
           .filter(dbm.Score.property_id == property_id)
           .order_by(dbm.Score.computed_at.desc())
           .first())
    return serializers.score_set(row) if row is not None else None


def load_flags(session: Session, property_id: UUID):
    rows = (session.query(dbm.Flag)
            .filter(dbm.Flag.property_id == property_id)
            .order_by(dbm.Flag.financial_impact_usd.desc())
            .all())
    return [serializers.flag_record(row) for row in rows]


def load_timeline(session: Session, property_id: UUID):
    """Merge foreclosure, bankruptcy, lien, listing, and change events (spec §11.7)."""
    events = []
    for row in session.query(dbm.ForeclosureEvent).filter(dbm.ForeclosureEvent.property_id == property_id).all():
        events.append(serializers.timeline_event(
            "foreclosure", row.event_date, row.event_type or "foreclosure_event",
            {"stage": row.stage_after_event, "trustee": row.trustee_name,
             "published_bid": str(row.published_bid) if row.published_bid is not None else None}))
    for row in session.query(dbm.BankruptcyEvent).filter(dbm.BankruptcyEvent.property_id == property_id).all():
        events.append(serializers.timeline_event(
            "bankruptcy", row.filing_date, f"chapter_{row.chapter or 'unknown'}_filed",
            {"case_number": row.case_number, "status": row.status}))
        if row.discharge_date:
            events.append(serializers.timeline_event("bankruptcy", row.discharge_date, "discharged",
                                                     {"case_number": row.case_number}))
    for row in session.query(dbm.Lien).filter(dbm.Lien.property_id == property_id).all():
        events.append(serializers.timeline_event(
            "lien", row.recording_date, f"{row.lien_type or 'lien'}_recorded",
            {"creditor": row.creditor_normalized or row.creditor_raw,
             "amount": str(row.amount) if row.amount is not None else None, "status": row.status}))
    for row in session.query(dbm.Listing).filter(dbm.Listing.property_id == property_id).all():
        events.append(serializers.timeline_event(
            "listing", row.list_date, "listed",
            {"status": row.status,
             "price": str(row.list_price) if row.list_price is not None else None}))
        if row.delist_date:
            events.append(serializers.timeline_event("listing", row.delist_date, "delisted",
                                                     {"status": row.status}))
    for row in session.query(dbm.ChangeEvent).filter(dbm.ChangeEvent.property_id == property_id).all():
        events.append(serializers.timeline_event(
            "change", row.detected_at, row.change_type or "change",
            {"field_path": row.field_path}))
    events.sort(key=lambda event: (event.event_date is None, event.event_date or date.min))
    return events
