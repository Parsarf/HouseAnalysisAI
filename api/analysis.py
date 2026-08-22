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

from sqlalchemy import or_
from sqlalchemy.orm import Session

from contracts import (
    AssumptionSet,
    NormalizedProperty,
    OfferGrid,
    Scenario,
    TimelineEvent,
    UnderwritingResult,
)
from db import models as dbm

from . import serializers

log = logging.getLogger(__name__)


def load_normalized(session: Session, property_id: UUID) -> NormalizedProperty | None:
    """Delegates to the explanation package's shared read model."""
    from explanation.store import load_normalized as _load
    return _load(session, property_id)


def load_assumption_set(session: Session, assumption_set_id: UUID | None = None) -> AssumptionSet | None:
    """Default assumption set as a contract object; None when none is configured."""
    from explanation.store import load_assumption_set as _load
    return _load(session, assumption_set_id)


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
            .filter(or_(dbm.Flag.resolution.is_(None), dbm.Flag.resolution != "superseded_duplicate"))
            .order_by(dbm.Flag.financial_impact_usd.desc().nullslast(), dbm.Flag.id)
            .all())
    return [serializers.flag_record(row) for row in rows]


def load_timeline(session: Session, property_id: UUID):
    """Merge foreclosure, bankruptcy, lien, listing, and change events (spec §11.7)."""
    events: list[TimelineEvent] = []
    for foreclosure in session.query(dbm.ForeclosureEvent).filter(dbm.ForeclosureEvent.property_id == property_id).all():
        events.append(serializers.timeline_event(
            "foreclosure", foreclosure.event_date, foreclosure.event_type or "foreclosure_event",
            {"stage": foreclosure.stage_after_event, "trustee": foreclosure.trustee_name,
             "published_bid": str(foreclosure.published_bid) if foreclosure.published_bid is not None else None}))
    for bankruptcy in session.query(dbm.BankruptcyEvent).filter(dbm.BankruptcyEvent.property_id == property_id).all():
        events.append(serializers.timeline_event(
            "bankruptcy", bankruptcy.filing_date, f"chapter_{bankruptcy.chapter or 'unknown'}_filed",
            {"case_number": bankruptcy.case_number, "status": bankruptcy.status}))
        if bankruptcy.discharge_date:
            events.append(serializers.timeline_event("bankruptcy", bankruptcy.discharge_date, "discharged",
                                                     {"case_number": bankruptcy.case_number}))
    for lien in session.query(dbm.Lien).filter(dbm.Lien.property_id == property_id).all():
        events.append(serializers.timeline_event(
            "lien", lien.recording_date, f"{lien.lien_type or 'lien'}_recorded",
            {"creditor": lien.creditor_normalized or lien.creditor_raw,
             "amount": str(lien.amount) if lien.amount is not None else None, "status": lien.status}))
    for listing in session.query(dbm.Listing).filter(dbm.Listing.property_id == property_id).all():
        events.append(serializers.timeline_event(
            "listing", listing.list_date, "listed",
            {"status": listing.status,
             "price": str(listing.list_price) if listing.list_price is not None else None}))
        if listing.delist_date:
            events.append(serializers.timeline_event("listing", listing.delist_date, "delisted",
                                                     {"status": listing.status}))
    for change in session.query(dbm.ChangeEvent).filter(dbm.ChangeEvent.property_id == property_id).all():
        events.append(serializers.timeline_event(
            "change", change.detected_at, change.change_type or "change",
            {"field_path": change.field_path}))
    events.sort(key=lambda event: (event.event_date is None, event.event_date or date.min))
    return events
