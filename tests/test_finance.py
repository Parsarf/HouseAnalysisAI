"""Offline tests for WP-6 finance/ (spec §6.5, §7). No DB, no network."""
from datetime import date
from decimal import Decimal
from uuid import uuid4

from contracts import (
    AcquisitionCosts,
    AddressBlock,
    AssumptionSet,
    AttachmentBasis,
    ConditionSignal,
    DataQualityBlock,
    FlagType,
    ForeclosureState,
    HoaBlock,
    HoldingAssumptions,
    LienRecord,
    MortgageRecord,
    NormalizedProperty,
    PropertyAttributes,
    RepairAssumptions,
    ResaleAssumptions,
    Scenario,
    SourceKind,
    StrategyAssumptions,
    TaxBlock,
    TrackedValue,
    ValuationCandidate,
)
from contracts.models import ComparableSale  # not yet re-exported by contracts/__init__
from finance import (
    ENGINE_VERSION,
    estimate_balance,
    finance_flags,
    historical_rate,
    transfer_tax_rate,
    underwrite,
)

AS_OF = date(2020, 6, 15)
ZERO = Decimal(0)


def assumptions(**overrides) -> AssumptionSet:
    acq = overrides.get("acquisition") or AcquisitionCosts(
        closing_pct=Decimal(".01"), title_pct=Decimal(".005"), escrow_flat=Decimal(1500),
        transfer_tax_lookup_key=overrides.get("transfer_tax_lookup_key"),
        financing_points=Decimal(".02"), financing_flat=Decimal(1200), inspection_flat=Decimal(600),
        legal_flat=Decimal(1500), acq_fee_pct=Decimal(".01"))
    return AssumptionSet(
        id=uuid4(), version=1, name="test", acquisition=acq,
        repairs=RepairAssumptions(psf_by_condition={"cosmetic": Decimal(18), "moderate": Decimal(42),
                                                    "heavy": Decimal(78), "gut": Decimal(135)},
                                  low_multiplier=Decimal(".75"), high_multiplier=Decimal("1.4"), regional_index=Decimal(1)),
        holding=HoldingAssumptions(insurance_pct_yr=Decimal(".0035"), utilities_monthly=Decimal(180),
                                   maintenance_pct_yr=Decimal(".005"), acquisition_months=Decimal(2),
                                   repair_months_by_condition={"moderate": Decimal(4)}, market_days_default=60),
        resale=ResaleAssumptions(commission_pct=Decimal(".05"), seller_closing_pct=Decimal(".01"),
                                 concessions_pct=Decimal(".01"), staging_flat=Decimal(3500), misc_pct=Decimal(".0025")),
        strategy=StrategyAssumptions(cash_target_margin=Decimal(".2"),
                                     flip_target_margin_by_arv_band={"default": Decimal(".2")},
                                     wholesale_investor_pct=Decimal(".7"), min_assignment_spread=Decimal(15000),
                                     hard_money={"rate": Decimal(".1"), "points": Decimal(".02"), "ltv": Decimal(".85")},
                                     rental={"vacancy": Decimal(".06")}),
        attachment_probability={AttachmentBasis.OWNER_NAMED_ONLY: Decimal(".35"), AttachmentBasis.UNKNOWN: Decimal(".5")},
        unknown_lien_medians={"hoa": Decimal(4500), "mechanics": Decimal(12000), "judgment": Decimal(18000)},
        valuation_weights=overrides.get("valuation_weights", {"manual": Decimal(1)}))


def tracked(value, **kw) -> TrackedValue:
    return TrackedValue(value=Decimal(str(value)) if value is not None else None,
                        confidence=kw.pop("confidence", 0.9), source_kind=kw.pop("source_kind", SourceKind.REPORT),
                        is_estimated=kw.pop("is_estimated", False), **kw)


def make_property(sqft="1800", condition="moderate", value="300000", valuation_type="manual", **kw) -> NormalizedProperty:
    candidates = kw.pop("valuation_candidates", [])
    if value is not None:
        candidates.append(ValuationCandidate(valuation_type=valuation_type, value=tracked(value),
                                             as_of=kw.pop("candidate_as_of", AS_OF)))
    return NormalizedProperty(
        property_id=uuid4(), address=AddressBlock(line1="1 Main St"),
        attributes=PropertyAttributes(sqft=tracked(sqft) if sqft else None),
        valuation_candidates=candidates,
        condition=ConditionSignal(condition=condition) if condition else None,
        data_quality=DataQualityBlock(critical_field_coverage=Decimal(".9"),
                                      mean_extraction_confidence=Decimal(".9"), newest_report_date=AS_OF),
        resolution_version="test", **kw)


# --- item 1: per-lien attachment_probability scenario weighting (spec §7.4) ---

def test_scenario_weighting_uses_attachment_probability():
    amount = tracked("10000")
    prop = make_property(liens=[
        LienRecord(lien_type="judgment", amount=amount, attachment_basis=AttachmentBasis.OWNER_NAMED_ONLY, attachment_confidence=.9),
        LienRecord(lien_type="other", amount=amount, attachment_basis=AttachmentBasis.UNKNOWN, attachment_confidence=.5)])
    result = underwrite(prop, assumptions())
    assert result.status == "ok"
    assert result.liabilities.confirmed == ZERO
    assert result.liabilities.potential == Decimal(20000)
    # expected: 10000×0.35 + 10000×0.50 = 8500; conservative: full; optimistic: excluded
    # scenario values: single candidate → disp 0.15 → 255000 / 300000 / 345000
    assert result.equity[Scenario.EXPECTED].adjusted == Decimal("291500.00")
    assert result.equity[Scenario.CONSERVATIVE].adjusted == Decimal("235000.00")
    assert result.equity[Scenario.OPTIMISTIC].adjusted == Decimal("345000.00")


# --- item 2: ARV is after-repair value (spec §7.2) ---

def test_arv_is_after_repair_value():
    result = underwrite(make_property(), assumptions())
    assert result.value.v_expected == Decimal("300000.00")
    # repairs = 1800 sqft × $42 × 1.0 = 75600; ARV = V + repairs × recapture(1.0)
    assert result.costs[Scenario.EXPECTED].repairs == Decimal("75600.00")
    assert result.value.arv_by_scenario[Scenario.EXPECTED] == Decimal("375600.00")
    assert result.value.arv_by_scenario[Scenario.EXPECTED] != result.value.v_expected


# --- item 3/4/5: full cost models (spec §7.5) ---

def test_acquisition_and_financing_cost_model():
    result = underwrite(make_property(), assumptions())
    costs = result.costs[Scenario.EXPECTED]
    # 300000 × (1.0% closing + 0.5% title + 1.0% acq fee) + 1500 + 600 + 1500 = 7500 + 3600
    assert costs.acquisition == Decimal("11100.00")
    # financing points/flat are charged in the flip strategy, where a purchase price
    # exists — no financing cost at underwrite time (golden formula set v1)
    assert costs.financing == ZERO


def test_acquisition_includes_transfer_tax():
    result = underwrite(make_property(), assumptions(transfer_tax_lookup_key="ca"))
    # 11100 + 300000 × 0.0011
    assert result.costs[Scenario.EXPECTED].acquisition == Decimal("11430.00")
    assert transfer_tax_rate(None) == ZERO
    assert transfer_tax_rate("XX:nowhere") == ZERO


def test_holding_cost_model():
    prop = make_property(taxes=TaxBlock(annual_taxes=tracked("3600")), hoa=HoaBlock(monthly_dues=tracked("250")))
    result = underwrite(prop, assumptions())
    # monthly = 300 taxes + 212.50 insurance+maintenance + 180 utilities + 250 HOA = 942.50
    # (no loan interest at underwrite time — no loan exists yet; golden formula set v1)
    # months = 2 acquisition + 4 repair (moderate) + 60/30 market = 8
    assert result.costs[Scenario.EXPECTED].holding == Decimal("7540.00")
    # conservative: v_low=255000 basis → monthly 910.63, period ×1.5 → 910.63 × 12
    assert result.costs[Scenario.CONSERVATIVE].holding == Decimal("10927.56")


def test_resale_uses_full_pct_on_value_staging_is_flip_only():
    result = underwrite(make_property(), assumptions())
    # resale = V × (5% commission + 1% seller closing + 1% concessions + 0.25% misc);
    # staging_flat is added by the flip strategy, not at underwrite (golden formula set v1)
    assert result.costs[Scenario.EXPECTED].resale == Decimal("21750.00")
    assert result.costs[Scenario.CONSERVATIVE].resale == Decimal("18487.50")  # 255000 × 0.0725


# --- item 6: estimate_balance amortization + historical rate fallback (spec §6.5) ---

def test_estimate_balance_amortization():
    # hand-verified: 100k @ 6%, 360-month term, 120 months elapsed
    balance = estimate_balance(Decimal(100000), Decimal("0.06"), 360, date(2010, 6, 15), AS_OF)
    assert balance == Decimal("83685.72")
    assert estimate_balance(Decimal(100000), Decimal("0.06"), 360, date(2025, 1, 1), AS_OF) == Decimal("100000.00")
    assert estimate_balance(Decimal(100000), Decimal("0.06"), 360, date(1980, 1, 1), AS_OF) == ZERO
    assert estimate_balance(None, Decimal("0.06"), 360, date(2010, 1, 1), AS_OF) is None


def test_estimate_balance_historical_rate_fallback():
    assert historical_rate(2005) == Decimal("0.0587")
    assert historical_rate(2005, "heloc") == Decimal("0.0787")
    balance = estimate_balance(Decimal(100000), None, None, date(2005, 6, 15), AS_OF)
    assert balance is not None
    assert ZERO < balance < Decimal(100000)


def test_derived_balance_feeds_liabilities():
    prop = make_property(mortgages=[
        MortgageRecord(position="first", original_amount=tracked("200000"), rate=Decimal("0.06"),
                       term_months=360, origination_date=date(2010, 6, 15))])
    result = underwrite(prop, assumptions())
    assert result.liabilities.confirmed == Decimal("167371.45")
    entry = result.liabilities.breakdown[0]
    assert entry["basis"] == "amortization_v1" and entry["is_estimated"]


def test_fallback_rate_still_derives_balance():
    prop = make_property(mortgages=[
        MortgageRecord(position="first", original_amount=tracked("200000"),
                       term_months=360, origination_date=date(2005, 6, 15))])
    result = underwrite(prop, assumptions())
    # rate fell back to the historical index (2005 conventional = 5.87%)
    assert result.liabilities.breakdown[0]["basis"] == "amortization_v1"
    assert ZERO < result.liabilities.confirmed < Decimal(200000)
    # confirmed debt is constant across scenarios (spec §7.1 scenario vectors do not
    # vary debt; the §6.5 ±150bps band is a rendering concern, golden formula set v1)
    confirmed_by_scenario = {Scenario.CONSERVATIVE: result.value.v_low - result.equity[Scenario.CONSERVATIVE].gross,
                             Scenario.EXPECTED: result.value.v_expected - result.equity[Scenario.EXPECTED].gross,
                             Scenario.OPTIMISTIC: result.value.v_high - result.equity[Scenario.OPTIMISTIC].gross}
    assert len(set(confirmed_by_scenario.values())) == 1


# --- item 7: published-bid reconciliation, bid_mismatch, undrawn HELOC (spec §7.3) ---

def test_published_bid_reconciles_first_mortgage_and_flags_mismatch():
    prop = make_property(
        mortgages=[MortgageRecord(position="first", estimated_balance=tracked("200000"))],
        foreclosure=ForeclosureState(stage="nts", is_active=True, published_bid=tracked("150000")))
    result = underwrite(prop, assumptions())
    # precedence favors the bid; the $200k reported balance is replaced
    assert result.liabilities.confirmed == Decimal("150000.00")
    flags = finance_flags(prop)
    assert [f.flag_type for f in flags] == [FlagType.BID_MISMATCH]
    assert flags[0].financial_impact_usd == Decimal("50000.00")


def test_published_bid_within_tolerance_not_flagged():
    prop = make_property(
        mortgages=[MortgageRecord(position="first", estimated_balance=tracked("140000"))],
        foreclosure=ForeclosureState(stage="nts", is_active=True, published_bid=tracked("150000")))
    result = underwrite(prop, assumptions())
    assert result.liabilities.confirmed == Decimal("150000.00")
    assert finance_flags(prop) == []


def test_undrawn_heloc_is_potential_not_confirmed():
    prop = make_property(mortgages=[
        MortgageRecord(position="heloc", original_amount=tracked("50000"), estimated_balance=tracked("20000"))])
    result = underwrite(prop, assumptions())
    assert result.liabilities.confirmed == Decimal(20000)
    assert result.liabilities.potential == Decimal(30000)
    assert result.liabilities.maximum == Decimal(50000)


# --- item 8: missing sqft → unavailable/flagged, never silent $0 repairs ---

def test_missing_sqft_makes_arv_unavailable_not_zero_repairs():
    prop = make_property(sqft=None)
    result = underwrite(prop, assumptions())
    assert result.status == "ok"
    assert all(arv is None for arv in result.value.arv_by_scenario.values())
    assert result.costs[Scenario.EXPECTED].repairs == ZERO
    # not silent: ARV is unavailable and confidence carries the single-candidate cap
    assert result.confidence == Decimal("0.5")


def test_unknown_condition_falls_back_to_moderate_rate():
    prop = make_property(condition=None)
    result = underwrite(prop, assumptions())
    assert result.costs[Scenario.EXPECTED].repairs == Decimal("75600.00")


# --- item 9: comp-range clamps, valuation_weights, recency decay, comp quality ---

def test_comp_range_clamps_value_band():
    prop = make_property(comparables=[
        ComparableSale(address="2 Main St", price=tracked("290000")),
        ComparableSale(address="3 Main St", price=tracked("310000"))])
    result = underwrite(prop, assumptions())
    # single candidate → disp 0.15 → raw band 255000–345000, clamped to comp range
    assert result.value.v_low == Decimal("290000.00")
    assert result.value.v_high == Decimal("310000.00")


def test_valuation_weights_and_avm_recency_decay():
    prop = make_property(value=None, valuation_candidates=[
        ValuationCandidate(valuation_type="avm", value=tracked("300000"), reported_confidence=1.0,
                           as_of=date(2020, 3, 17)),  # 90 days before AS_OF → decay 0.5
        ValuationCandidate(valuation_type="manual", value=tracked("330000"), as_of=AS_OF)])
    result = underwrite(prop, assumptions(valuation_weights={"avm": Decimal("0.3"), "manual": Decimal(1)}))
    # weights: avm 0.3 × reported_confidence 1.0 × decay 0.5 = 0.15 ; manual 1.0
    # expected = (300000×0.15 + 330000×1.0) / 1.15
    assert result.value.v_expected == Decimal("326086.96")
    assert result.value.candidates_used[0]["weight"] == Decimal("0.15")


def test_comp_quality_adjusts_comp_candidate_weight():
    comp_candidate = ValuationCandidate(valuation_type="comp_estimate", value=tracked("300000"), as_of=AS_OF)
    weak = make_property(value=None, valuation_candidates=[comp_candidate],
                         comparables=[ComparableSale(address="2 Main St", price=tracked("300000"))])
    strong = make_property(value=None, valuation_candidates=[comp_candidate],
                           comparables=[ComparableSale(address=f"{i} Main St", price=tracked("300000")) for i in range(2, 8)])
    weights = {"comp_estimate": Decimal(1)}
    weak_weight = underwrite(weak, assumptions(valuation_weights=weights)).value.candidates_used[0]["weight"]
    strong_weight = underwrite(strong, assumptions(valuation_weights=weights)).value.candidates_used[0]["weight"]
    assert weak_weight == Decimal("0.7")      # 1 comp → count factor 0.7
    assert strong_weight == Decimal(1)      # ≥5 comps → no discount
    assert weak_weight < strong_weight


# --- spec §7.7 failure cases ---

def test_no_value_candidates_is_insufficient_data_with_no_numbers():
    result = underwrite(make_property(value=None), assumptions())
    assert result.status == "insufficient_data"
    assert result.unavailable_reason == "no_valuation_candidates"
    assert result.equity == {} and result.costs == {}
    assert result.value.v_expected is None and result.value.arv_by_scenario == {}
    assert result.liabilities.confirmed == ZERO  # liabilities are still computed
    assert result.confidence == ZERO


def test_no_debt_records_zero_confirmed_and_not_present():
    result = underwrite(make_property(), assumptions())
    assert result.liabilities.confirmed == ZERO
    assert result.debt_data_present is False
    assert result.confidence == Decimal("0.5")  # 0.9 → single-candidate cap 0.5


def test_released_lien_excluded():
    prop = make_property(liens=[
        LienRecord(lien_type="judgment", amount=tracked("10000"), status="released",
                   attachment_basis=AttachmentBasis.RECORDED_AGAINST_PROPERTY, attachment_confidence=.9)])
    result = underwrite(prop, assumptions())
    assert result.liabilities.confirmed == ZERO
    assert result.liabilities.maximum == ZERO


def test_missing_lien_amount_uses_median_and_flags():
    prop = make_property(liens=[
        LienRecord(lien_type="judgment", amount=None,
                   attachment_basis=AttachmentBasis.OWNER_NAMED_ONLY, attachment_confidence=.9)])
    result = underwrite(prop, assumptions())
    assert result.liabilities.potential == Decimal(18000)  # judgment median
    assert result.liabilities.breakdown[0]["is_estimated"] is True
    flags = finance_flags(prop)
    assert [f.flag_type for f in flags] == [FlagType.MISSING_LIEN_AMOUNT]


# --- acceptance: determinism, no floats in the money path ---

def _assert_no_floats(node):
    if isinstance(node, float):
        raise AssertionError(f"float in result: {node!r}")
    if isinstance(node, dict):
        for key, value in node.items():
            _assert_no_floats(key)
            _assert_no_floats(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _assert_no_floats(item)


def test_determinism_100_runs_and_no_floats():
    prop = make_property(
        mortgages=[MortgageRecord(position="first", original_amount=tracked("200000"),
                                  term_months=360, origination_date=date(2005, 6, 15))],
        liens=[LienRecord(lien_type="judgment", amount=tracked("10000"),
                          attachment_basis=AttachmentBasis.OWNER_NAMED_ONLY, attachment_confidence=.9)],
        taxes=TaxBlock(annual_taxes=tracked("3600"), delinquent_amount=tracked("1200")),
        hoa=HoaBlock(monthly_dues=tracked("250")))
    assumption_set = assumptions(transfer_tax_lookup_key="CA")
    first = underwrite(prop, assumption_set)
    _assert_no_floats(first.model_dump())
    for _ in range(99):
        assert underwrite(prop, assumption_set).model_dump() == first.model_dump()
    assert first.engine_version == ENGINE_VERSION
