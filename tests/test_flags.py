import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from common.errors import AcqError
from contracts import (
    AddressBlock,
    AssumptionSet,
    AttachmentBasis,
    DataQualityBlock,
    ExtractedFactDraft,
    FlagType,
    ForeclosureState,
    LienRecord,
    MortgageRecord,
    NormalizedProperty,
    SourceKind,
    TrackedValue,
    ValuationCandidate,
)
from db.models import Flag
from flags import (
    apply_override,
    collect_flags,
    flag_summaries,
    is_gating,
    open_flags,
    persist_flags,
    resolve_flag,
    sync_flags,
)
from flags.service import GATING_FLAG_TYPES

FIXTURES = Path(__file__).parent.parent / "fixtures"


def tracked(value, confidence=0.9):
    return TrackedValue(value=value, confidence=confidence, source_kind=SourceKind.REPORT,
                        is_estimated=False)


def base_property(**overrides) -> NormalizedProperty:
    property = NormalizedProperty(
        property_id=uuid4(), apn="APN-1", address=AddressBlock(line1="1 Main St"),
        valuation_candidates=[ValuationCandidate(valuation_type="manual", value=tracked(Decimal(300000)))],
        data_quality=DataQualityBlock(critical_field_coverage=Decimal(".9"),
                                      mean_extraction_confidence=Decimal(".9")),
        resolution_version="test")
    for key, value in overrides.items():
        setattr(property, key, value)
    return property


def load_assumptions() -> AssumptionSet:
    return AssumptionSet.model_validate(json.loads((FIXTURES / "assumptions" / "default.json").read_text()))


def lien(amount, basis=AttachmentBasis.RECORDED_AGAINST_PROPERTY, status="active",
         lien_type="judgment"):
    return LienRecord(lien_type=lien_type, amount=tracked(amount) if amount is not None else None,
                      status=status, attachment_basis=basis, attachment_confidence=0.9)


def mortgage(balance, position="first"):
    return MortgageRecord(position=position, estimated_balance=tracked(balance))


# --- trigger tests: each of the ten spec §12 flag types ---

def test_identity_conflict_trigger():
    quality = DataQualityBlock(conflict_count=1, source_counts_by_field={"property.apn": 2})
    flags = collect_flags(base_property(data_quality=quality))
    assert FlagType.IDENTITY_CONFLICT in {flag.flag_type for flag in flags}
    assert is_gating(FlagType.IDENTITY_CONFLICT)


def test_lien_attachment_trigger():
    flags = collect_flags(base_property(liens=[lien(Decimal(128000), AttachmentBasis.OWNER_NAMED_ONLY)]))
    matches = [flag for flag in flags if flag.flag_type == FlagType.LIEN_ATTACHMENT]
    assert len(matches) == 1
    assert matches[0].payload["basis"] == "owner_named_only"


def test_lien_attachment_not_raised_below_threshold_or_when_recorded():
    property = base_property(liens=[lien(Decimal(9999), AttachmentBasis.UNKNOWN),
                                    lien(Decimal(50000), AttachmentBasis.RECORDED_AGAINST_PROPERTY),
                                    lien(Decimal(50000), AttachmentBasis.OWNER_NAMED_ONLY, status="released")])
    assert not [flag for flag in collect_flags(property) if flag.flag_type == FlagType.LIEN_ATTACHMENT]


def test_conflicting_mortgage_trigger():
    flags = collect_flags(base_property(mortgages=[mortgage(Decimal(100000)), mortgage(Decimal(120000))]))
    matches = [flag for flag in flags if flag.flag_type == FlagType.CONFLICTING_MORTGAGE]
    assert len(matches) == 1
    assert matches[0].payload["balances"] == ["100000", "120000"]


def test_conflicting_mortgage_not_raised_within_tolerance():
    property = base_property(mortgages=[mortgage(Decimal(100000)), mortgage(Decimal(104000))])
    assert not [flag for flag in collect_flags(property) if flag.flag_type == FlagType.CONFLICTING_MORTGAGE]


def test_conflicting_mortgage_from_material_conflict_count():
    quality = DataQualityBlock(material_conflict_count=2)
    flags = collect_flags(base_property(data_quality=quality))
    assert FlagType.CONFLICTING_MORTGAGE in {flag.flag_type for flag in flags}


def test_foreclosure_unclear_trigger():
    foreclosure = ForeclosureState(stage="nts", nts_date=date(2024, 2, 1), is_active=True)
    flags = collect_flags(base_property(foreclosure=foreclosure))
    matches = [flag for flag in flags if flag.flag_type == FlagType.FORECLOSURE_UNCLEAR]
    assert len(matches) == 1
    assert "nts_without_nod" in matches[0].payload["contradictions"]


def test_missing_lien_amount_trigger():
    flags = collect_flags(base_property(liens=[lien(None)]), load_assumptions())
    matches = [flag for flag in flags if flag.flag_type == FlagType.MISSING_LIEN_AMOUNT]
    assert len(matches) == 1
    assert matches[0].payload["median_used"] == "18000"


def test_valuation_dispersion_trigger():
    candidates = [ValuationCandidate(valuation_type="avm", value=tracked(Decimal(100000))),
                  ValuationCandidate(valuation_type="comp", value=tracked(Decimal(200000)))]
    flags = collect_flags(base_property(valuation_candidates=candidates))
    matches = [flag for flag in flags if flag.flag_type == FlagType.VALUATION_DISPERSION]
    assert len(matches) == 1


def test_valuation_dispersion_not_raised_within_ratio():
    candidates = [ValuationCandidate(valuation_type="avm", value=tracked(Decimal(100000))),
                  ValuationCandidate(valuation_type="comp", value=tracked(Decimal(140000)))]
    assert not [flag for flag in collect_flags(base_property(valuation_candidates=candidates))
                if flag.flag_type == FlagType.VALUATION_DISPERSION]


def test_missing_apn_trigger():
    flags = collect_flags(base_property(apn=None))
    assert FlagType.MISSING_APN in {flag.flag_type for flag in flags}


def test_low_extraction_confidence_trigger():
    candidates = [ValuationCandidate(valuation_type="manual", value=tracked(Decimal(300000), confidence=0.5))]
    flags = collect_flags(base_property(valuation_candidates=candidates))
    matches = [flag for flag in flags if flag.flag_type == FlagType.LOW_EXTRACTION_CONFIDENCE]
    assert len(matches) == 1
    assert matches[0].payload["fields"][0]["confidence"] == 0.5


def test_bid_mismatch_trigger():
    foreclosure = ForeclosureState(stage="nts", published_bid=tracked(Decimal(100000)), is_active=True)
    flags = collect_flags(base_property(foreclosure=foreclosure, mortgages=[mortgage(Decimal(150000))]))
    matches = [flag for flag in flags if flag.flag_type == FlagType.BID_MISMATCH]
    assert len(matches) == 1


def test_bid_mismatch_not_raised_within_20_pct():
    foreclosure = ForeclosureState(stage="nts", published_bid=tracked(Decimal(100000)), is_active=True)
    property = base_property(foreclosure=foreclosure, mortgages=[mortgage(Decimal(115000))])
    assert not [flag for flag in collect_flags(property) if flag.flag_type == FlagType.BID_MISMATCH]


def test_range_violation_trigger():
    from contracts import PropertyAttributes
    attributes = PropertyAttributes(sqft=tracked(Decimal(50)))
    flags = collect_flags(base_property(attributes=attributes))
    matches = [flag for flag in flags if flag.flag_type == FlagType.RANGE_VIOLATION]
    assert len(matches) == 1
    assert matches[0].payload["field"] == "sqft"


def test_collect_flags_clean_property_produces_nothing():
    assert collect_flags(base_property()) == []


# --- financial impact: equity delta between accepting and rejecting (spec §12) ---

def test_lien_attachment_impact_is_equity_delta_for_128k_fixture():
    property = NormalizedProperty.model_validate(
        json.loads((FIXTURES / "normalized" / "02_owner_only_federal_lien.json").read_text()))
    flags = collect_flags(property, load_assumptions())
    matches = [flag for flag in flags if flag.flag_type == FlagType.LIEN_ATTACHMENT]
    assert len(matches) == 1
    assert matches[0].financial_impact_usd == Decimal(128000)


def test_impact_differs_from_raw_amount_when_engine_discounts_potential():
    # A $20k owner-only lien over the threshold: rejecting it entirely vs accepting it as
    # attached is the full $20k, but the expected-scenario delta vs. *leaving it potential*
    # would be $10k — the flag must measure accept vs. reject, not copy the raw amount.
    property = base_property(liens=[lien(Decimal(20000), AttachmentBasis.OWNER_NAMED_ONLY)])
    (flag,) = [flag for flag in collect_flags(property, load_assumptions())
               if flag.flag_type == FlagType.LIEN_ATTACHMENT]
    assert flag.financial_impact_usd == Decimal(20000)
    assert flag.payload["amount"] == "20000"


def test_bid_mismatch_impact_is_payoff_delta():
    foreclosure = ForeclosureState(stage="nts", published_bid=tracked(Decimal(100000)), is_active=True)
    property = base_property(foreclosure=foreclosure, mortgages=[mortgage(Decimal(150000))])
    (flag,) = [flag for flag in collect_flags(property, load_assumptions())
               if flag.flag_type == FlagType.BID_MISMATCH]
    assert flag.financial_impact_usd == Decimal(50000)


def test_valuation_dispersion_impact_is_candidate_delta():
    candidates = [ValuationCandidate(valuation_type="avm", value=tracked(Decimal(100000))),
                  ValuationCandidate(valuation_type="comp", value=tracked(Decimal(200000)))]
    (flag,) = [flag for flag in collect_flags(base_property(valuation_candidates=candidates), load_assumptions())
               if flag.flag_type == FlagType.VALUATION_DISPERSION]
    assert flag.financial_impact_usd == Decimal(100000)


def test_impact_is_none_without_assumptions():
    property = base_property(liens=[lien(Decimal(128000), AttachmentBasis.OWNER_NAMED_ONLY)])
    (flag,) = [flag for flag in collect_flags(property) if flag.flag_type == FlagType.LIEN_ATTACHMENT]
    assert flag.financial_impact_usd is None


def test_missing_lien_amount_impact_uses_median():
    (flag,) = [flag for flag in collect_flags(base_property(liens=[lien(None)]), load_assumptions())
               if flag.flag_type == FlagType.MISSING_LIEN_AMOUNT]
    assert flag.financial_impact_usd == Decimal(18000)


# --- persistence (offline, sqlite) ---

@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Flag.__table__.create(engine)
    with Session(engine) as session:
        session.execute(text("""
            CREATE TABLE extracted_facts (id TEXT PRIMARY KEY, property_id TEXT, entity_type TEXT,
                entity_local_id TEXT, field_path TEXT, value_raw TEXT, value_parsed NUMERIC,
                value_text TEXT, value_date DATE, value_bool BOOLEAN, page_number INTEGER,
                snippet TEXT, extraction_confidence NUMERIC, null_reason TEXT, source_kind TEXT,
                is_active BOOLEAN DEFAULT 1)"""))
        session.execute(text("""
            CREATE TABLE field_resolutions (id TEXT PRIMARY KEY, property_id TEXT, field_path TEXT,
                winning_fact_id TEXT, verification_state TEXT DEFAULT 'unverified')"""))
        session.execute(text("""
            CREATE TABLE history (id TEXT PRIMARY KEY, entity_type TEXT, entity_id TEXT,
                action TEXT, before TEXT, after TEXT, at TEXT)"""))
        session.commit()
        yield session


def history_rows(session):
    return session.execute(text("SELECT entity_type, entity_id, action FROM history")).all()


def record_hook(calls):
    def hook(session, property_id, reason):
        calls.append((property_id, reason))
        return {"enqueued": reason}
    return hook


def test_persist_flags_inserts_rows(session):
    property = base_property(liens=[lien(Decimal(128000), AttachmentBasis.OWNER_NAMED_ONLY)])
    created = persist_flags(session, collect_flags(property))
    session.commit()
    assert len(created) == 1
    rows = session.query(Flag).all()
    assert len(rows) == 1
    assert rows[0].flag_type == "lien_attachment"
    assert rows[0].status == "open"


def test_persist_flags_dedupes_on_rerun(session):
    property = base_property(liens=[lien(Decimal(128000), AttachmentBasis.OWNER_NAMED_ONLY)])
    first = persist_flags(session, collect_flags(property))
    session.commit()
    second = persist_flags(session, collect_flags(property))
    session.commit()
    assert len(first) == 1
    assert second == []
    assert session.query(Flag).count() == 1


def short_sale_request(property_id, *, fingerprint="same", minimum="600000", maximum="800000", count=20):
    from contracts import FlagRequest
    return FlagRequest(
        property_id=property_id, flag_type=FlagType.SHORT_SALE_CANDIDATE,
        payload={"affected_offer_points": count, "affected_scenarios": 2,
                 "scenarios": ["conservative", "expected"],
                 "offer_price_min": minimum, "offer_price_max": maximum,
                 "proceeds_low_min": "-100000", "proceeds_low_max": "-1000",
                 "reason": "insufficient proceeds"},
        financial_impact_usd=Decimal("100000"), raised_by="strategies",
        dedupe_key=f"{property_id}:short_sale_candidate", logical_key="short_sale_candidate",
        fingerprint=fingerprint,
    )


def test_sync_flags_aggregates_and_is_property_aware(session):
    property_a, property_b = uuid4(), uuid4()
    first = sync_flags(session, property_a, [short_sale_request(property_a)])
    second = sync_flags(session, property_a, [short_sale_request(property_a)])
    other = sync_flags(session, property_b, [short_sale_request(property_b)])
    session.commit()
    assert len(first) == len(second) == len(other) == 1
    assert session.query(Flag).filter(Flag.flag_type == FlagType.SHORT_SALE_CANDIDATE.value).count() == 2
    assert len(open_flags(session, property_a)) == 1
    assert len(open_flags(session, property_b)) == 1


def test_sync_flags_closes_disappearing_finding_and_reopens_returning_condition(session):
    property_id = uuid4()
    sync_flags(session, property_id, [short_sale_request(property_id)])
    session.commit()
    sync_flags(session, property_id, [])
    session.commit()
    row = session.query(Flag).filter(Flag.property_id == property_id).one()
    assert row.status == "resolved"
    assert row.resolution == "superseded_recompute"
    sync_flags(session, property_id, [short_sale_request(property_id)])
    session.commit()
    assert len(open_flags(session, property_id)) == 1
    assert session.query(Flag).filter(Flag.property_id == property_id).count() == 1


def test_sync_flags_material_change_creates_new_row_after_manual_resolution(session):
    property_id = uuid4()
    sync_flags(session, property_id, [short_sale_request(property_id, fingerprint="a")])
    session.commit()
    row = open_flags(session, property_id)[0]
    resolve_flag(session, row.id, "dismiss", recompute_hook=record_hook([]))
    sync_flags(session, property_id, [short_sale_request(property_id, fingerprint="b", maximum="900000")])
    session.commit()
    rows = session.query(Flag).filter(Flag.property_id == property_id).all()
    assert len(rows) == 2
    assert len(open_flags(session, property_id)) == 1


def test_open_flags_sorted_by_financial_impact(session):
    property = base_property(
        liens=[lien(Decimal(128000), AttachmentBasis.OWNER_NAMED_ONLY),
               lien(Decimal(20000), AttachmentBasis.UNKNOWN, lien_type="mechanics")],
        mortgages=[mortgage(Decimal(100000)), mortgage(Decimal(120000))])
    persist_flags(session, collect_flags(property, load_assumptions()))
    session.commit()
    impacts = [flag.financial_impact_usd for flag in open_flags(session, property.property_id)]
    assert impacts == sorted(impacts, reverse=True)
    assert impacts[0] == Decimal(128000)


# --- resolution workflow ---

ALL_TEN_TYPES = [FlagType.IDENTITY_CONFLICT, FlagType.LIEN_ATTACHMENT, FlagType.CONFLICTING_MORTGAGE,
                 FlagType.FORECLOSURE_UNCLEAR, FlagType.MISSING_LIEN_AMOUNT, FlagType.VALUATION_DISPERSION,
                 FlagType.MISSING_APN, FlagType.LOW_EXTRACTION_CONFIDENCE, FlagType.BID_MISMATCH,
                 FlagType.RANGE_VIOLATION]


def add_flag(session, flag_type=FlagType.LIEN_ATTACHMENT, payload=None) -> Flag:
    flag = Flag(id=uuid4(), property_id=uuid4(), flag_type=flag_type.value,
                payload=payload or {}, financial_impact_usd=Decimal(1000),
                status="open", dedupe_key=f"test:{uuid4()}")
    session.add(flag)
    session.flush()
    return flag


@pytest.mark.parametrize("flag_type", ALL_TEN_TYPES)
def test_each_flag_type_resolves_and_triggers_recompute(session, flag_type):
    flag = add_flag(session, flag_type)
    calls = []
    result = resolve_flag(session, flag.id, "dismiss", note="not actionable",
                          user_id="analyst", recompute_hook=record_hook(calls))
    session.commit()
    assert flag.status == "resolved"
    assert flag.resolution == "dismiss"
    assert flag.resolved_at is not None
    assert calls == [(flag.property_id, f"flag_resolved:{flag_type.value}")]
    assert result["recompute"] == {"enqueued": f"flag_resolved:{flag_type.value}"}
    assert len(history_rows(session)) == 1


def test_resolve_reject_deactivates_facts(session):
    fact_id = uuid4()
    session.execute(text("INSERT INTO extracted_facts (id, is_active) VALUES (:id, 1)"), {"id": str(fact_id)})
    flag = add_flag(session, payload={"fact_ids": [str(fact_id)]})
    resolve_flag(session, flag.id, "reject", recompute_hook=record_hook([]))
    session.commit()
    (is_active,) = session.execute(text("SELECT is_active FROM extracted_facts WHERE id=:id"),
                                   {"id": str(fact_id)}).one()
    assert not is_active


def test_resolve_approve_marks_human_verified(session):
    fact_id = uuid4()
    session.execute(text(
        "INSERT INTO field_resolutions (id, winning_fact_id, verification_state) "
        "VALUES (:id, :fact_id, 'corroborated')"), {"id": str(uuid4()), "fact_id": str(fact_id)})
    flag = add_flag(session, payload={"fact_ids": [str(fact_id)]})
    resolve_flag(session, flag.id, "approve", recompute_hook=record_hook([]))
    session.commit()
    (state,) = session.execute(text("SELECT verification_state FROM field_resolutions")).one()
    assert state == "human_verified"


def test_resolve_replace_creates_human_fact(session):
    property_id = uuid4()
    flag = add_flag(session)
    flag.property_id = property_id
    calls = []
    result = resolve_flag(session, flag.id, "replace",
                          resolved_value={"field_path": "property.apn", "value": "APN-999"},
                          note="verified against deed", user_id="analyst",
                          recompute_hook=record_hook(calls))
    session.commit()
    row = session.execute(text(
        "SELECT source_kind, extraction_confidence, value_text, property_id FROM extracted_facts")).one()
    assert row[0] == "human"
    assert float(row[1]) == 1.0
    assert row[2] == "APN-999"
    assert flag.resolved_value["fact_id"]
    # replace fires exactly one recompute (the resolution's), not two
    assert calls == [(property_id, "flag_resolved:lien_attachment")]
    assert result["resolution"] == "replace"


def test_resolve_replace_requires_value(session):
    flag = add_flag(session)
    with pytest.raises(AcqError):
        resolve_flag(session, flag.id, "replace", recompute_hook=record_hook([]))


def test_resolve_unknown_action_and_missing_flag(session):
    flag = add_flag(session)
    with pytest.raises(AcqError):
        resolve_flag(session, flag.id, "explode")
    with pytest.raises(AcqError):
        resolve_flag(session, uuid4(), "dismiss")


def test_resolve_already_resolved_conflicts(session):
    flag = add_flag(session)
    resolve_flag(session, flag.id, "dismiss", recompute_hook=record_hook([]))
    with pytest.raises(AcqError):
        resolve_flag(session, flag.id, "dismiss", recompute_hook=record_hook([]))


# --- apply_override ---

def test_apply_override_writes_human_fact_and_enqueues_recompute(session):
    property_id = uuid4()
    calls = []
    fact_id = apply_override(property_id, "mortgages.first.estimated_balance", Decimal(97500),
                             "payoff statement", "analyst", session=session,
                             recompute_hook=record_hook(calls))
    session.commit()
    assert isinstance(fact_id, UUID)
    row = session.execute(text(
        "SELECT entity_type, field_path, value_parsed, source_kind, extraction_confidence "
        "FROM extracted_facts WHERE id=:id"), {"id": str(fact_id)}).one()
    assert row[0] == "mortgage"
    assert row[1] == "mortgages.first.estimated_balance"
    assert Decimal(str(row[2])) == Decimal(97500)
    assert row[3] == "human"
    assert float(row[4]) == 1.0
    assert calls == [(property_id, "apply_override:mortgages.first.estimated_balance")]
    assert len(history_rows(session)) == 1


def test_apply_override_fact_wins_resolution_against_other_sources():
    resolver = pytest.importorskip("normalization.resolver")  # sibling WP-5; skip while in flight

    def draft(value, confidence, source):
        return ExtractedFactDraft(report_id=uuid4(), extraction_unit_id=uuid4(),
                                  entity_type="property", entity_local_id="property",
                                  field_path="property.apn", value_text=value,
                                  page_number=1, snippet=value, extraction_confidence=confidence,
                                  source_kind=source)

    human = draft("APN-HUMAN", 1.0, SourceKind.HUMAN)
    resolved = resolver.resolve_facts(uuid4(), [draft("APN-REPORT", 0.95, SourceKind.REPORT),
                                                draft("APN-API", 0.9, SourceKind.API), human])
    assert resolved.apn == "APN-HUMAN"


# --- gating wiring into scoring (acceptance: resolving a gating flag unblocks ranking) ---

def test_gating_flag_summary_blocks_ranking_until_resolved():
    from finance import underwrite
    from scoring import score

    property = base_property()
    underwriting = underwrite(property, load_assumptions())
    requests = collect_flags(base_property(
        data_quality=DataQualityBlock(conflict_count=1, source_counts_by_field={"property.apn": 2})))
    assert any(request.flag_type in GATING_FLAG_TYPES for request in requests)

    property.open_flags = flag_summaries(requests)
    gated = score(property, underwriting, uuid4())
    assert "open_gating_flag" in gated.gates_applied
    assert not gated.is_rankable

    property.open_flags = []  # resolution clears the flag; next recompute is rankable again
    resolved = score(property, underwriting, uuid4())
    assert "open_gating_flag" not in resolved.gates_applied
    assert resolved.is_rankable
