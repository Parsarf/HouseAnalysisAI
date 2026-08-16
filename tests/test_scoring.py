"""Offline tests for WP-8 scoring and ranking (spec section 10, 11.1).

All tests use in-code DEFAULT_CONFIG and hand-built contracts — no DB, no
network. ``as_of`` is always pinned so recency math is deterministic.
"""
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from contracts import (
    AddressBlock,
    AttachmentBasis,
    BankruptcyRecord,
    DataQualityBlock,
    EquityBlock,
    FlagSummary,
    FlagType,
    ForeclosureState,
    HoaBlock,
    LienRecord,
    ListingRecord,
    NormalizedProperty,
    OwnershipBlock,
    Scenario,
    SourceKind,
    StrategyResult,
    StrategyType,
    TaxBlock,
    TrackedValue,
    UnderwritingResult,
    ValueBlock,
)
from scoring import compute_ranks, rank_scope, score
from scoring.engine import DEFAULT_CONFIG

AS_OF = date(2026, 1, 15)


def tv(value) -> TrackedValue:
    return TrackedValue(value=Decimal(str(value)), confidence=0.9, source_kind=SourceKind.REPORT, is_estimated=False)


def strong_quality() -> DataQualityBlock:
    """DCS = 100: full coverage, corroboration, fresh report, no conflicts."""
    return DataQualityBlock(
        critical_field_coverage=Decimal("1"),
        source_counts_by_field={f"field_{i}": 2 for i in range(22)},
        material_conflict_count=0,
        verified_field_count=22,
        newest_report_date=AS_OF,
        mean_extraction_confidence=Decimal("1"),
    )


def make_property(**overrides) -> NormalizedProperty:
    kwargs = dict(
        property_id=uuid4(),
        address=AddressBlock(line1="1 Main St"),
        data_quality=strong_quality(),
        resolution_version="test",
    )
    kwargs.update(overrides)
    return NormalizedProperty(**kwargs)


def make_underwriting(property_id, status="ok", v_expected="300000", equity_pct=".40", **overrides) -> UnderwritingResult:
    equity = {
        # Conservative inserted first on purpose: equity_pct must come from EXPECTED.
        Scenario.CONSERVATIVE: EquityBlock(gross=Decimal("50000"), equity_pct=Decimal(".10")),
        Scenario.EXPECTED: EquityBlock(gross=Decimal("120000"), equity_pct=Decimal(equity_pct)),
        Scenario.OPTIMISTIC: EquityBlock(gross=Decimal("180000"), equity_pct=Decimal(".55")),
    }
    return UnderwritingResult(
        property_id=property_id,
        assumption_set_id=uuid4(),
        engine_version="test",
        status=status,
        value=ValueBlock(v_expected=Decimal(v_expected)),
        equity=equity,
        **overrides,
    )


def make_strategy(strategy, profit, roi=".25", mao="200000", margin=".20", status="viable") -> StrategyResult:
    return StrategyResult(
        strategy=strategy,
        scenario=Scenario.EXPECTED,
        status=status,
        profit=Decimal(str(profit)) if profit is not None else None,
        roi=Decimal(roi) if roi is not None else None,
        mao=Decimal(mao) if mao is not None else None,
        margin_of_safety=Decimal(margin) if margin is not None else None,
    )


def lien(lien_type, basis, amount=None, status="active", priority=None, recording_date=None) -> LienRecord:
    return LienRecord(
        lien_type=lien_type,
        amount=tv(amount) if amount is not None else None,
        status=status,
        attachment_basis=basis,
        attachment_confidence=0.9,
        recording_date=recording_date,
        priority=priority,
    )


def test_equity_pct_uses_expected_scenario():
    record = make_property()
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    assert result.components["equity_pct"] == Decimal(".40")


def test_dcs_formula_matches_spec_without_double_counting():
    quality = DataQualityBlock(
        critical_field_coverage=Decimal(".8"),
        source_counts_by_field={f"field_{i}": 2 for i in range(11)},
        material_conflict_count=0,
        verified_field_count=11,
        newest_report_date=AS_OF,
        mean_extraction_confidence=Decimal(".9"),
    )
    record = make_property(data_quality=quality)
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    # 100 * (.30*.8 + .20*.5 + .20*1 + .15*1 + .10*.5 + .05*.9)
    assert result.data_confidence == Decimal("78.5")
    # verified_field_count feeds only the 0.10 verification term: +5, not +10.
    quality.verified_field_count = 22
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    assert result.data_confidence == Decimal("83.5")


def test_dcs_recency_decays_with_report_age():
    record = make_property()
    quality = record.data_quality
    quality.newest_report_date = AS_OF - timedelta(days=180)
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    assert result.components["dcs_recency"] == Decimal("10")  # 100 * 0.20 * 0.5
    quality.newest_report_date = None
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    assert result.components["dcs_recency"] == Decimal("0")


def test_distress_nts_within_30_days_vs_later():
    near = ForeclosureState(stage="nts", nts_date=AS_OF, current_sale_date=AS_OF + timedelta(days=20), is_active=True)
    record = make_property(foreclosure=near)
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    assert result.components["distress_foreclosure"] == Decimal("30")

    far = ForeclosureState(stage="nts", nts_date=AS_OF, current_sale_date=AS_OF + timedelta(days=60), is_active=True)
    record = make_property(foreclosure=far)
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    assert result.components["distress_foreclosure"] == Decimal("24")


def test_distress_recency_decay_halves_at_18_months():
    foreclosure = ForeclosureState(
        stage="nts",
        nts_date=AS_OF - timedelta(days=547),  # ~18 months
        current_sale_date=AS_OF + timedelta(days=60),
        is_active=True,
    )
    record = make_property(foreclosure=foreclosure)
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    points = result.components["distress_foreclosure"]
    assert abs(points - Decimal("12")) < Decimal("0.1")  # 24 * 0.5^(~1)


def test_distress_nod_and_prior_activity_cap():
    foreclosure = ForeclosureState(stage="nod", nod_date=AS_OF, postponement_count=3, rescission_count=2, is_active=True)
    record = make_property(foreclosure=foreclosure)
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    assert result.components["distress_foreclosure"] == Decimal("18")
    assert result.components["distress_prior_foreclosure_activity"] == Decimal("16")  # 5 * 8 capped


def test_distress_bankruptcy_terms_and_caps():
    bankruptcies = [BankruptcyRecord(chapter="13", status="active", filing_date=AS_OF)] + [
        BankruptcyRecord(chapter="7", status="dismissed", filing_date=AS_OF) for _ in range(4)
    ]
    record = make_property(bankruptcies=bankruptcies)
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    assert result.components["distress_bankruptcy_active"] == Decimal("12")
    assert result.components["distress_bankruptcy_prior"] == Decimal("18")  # 4 * 6 capped at 18
    assert result.components["distress_repeat_filings"] == Decimal("8")


def test_distress_repeat_filings_by_sequence():
    record = make_property(bankruptcies=[BankruptcyRecord(chapter="13", status="active", filing_date=AS_OF, sequence=2)])
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    assert result.components["distress_repeat_filings"] == Decimal("8")


def test_distress_liens_and_other_cap():
    liens = [
        lien("property_tax", AttachmentBasis.RECORDED_AGAINST_PROPERTY, recording_date=AS_OF),
        lien("federal_tax", AttachmentBasis.OWNER_NAMED_ONLY, recording_date=AS_OF),
    ] + [lien("judgment", AttachmentBasis.OWNER_NAMED_ONLY, recording_date=AS_OF) for _ in range(6)]
    liens.append(lien("property_tax", AttachmentBasis.RECORDED_AGAINST_PROPERTY, status="released", recording_date=AS_OF))
    record = make_property(liens=liens)
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    assert result.components["distress_tax_lien_property"] == Decimal("10")
    assert result.components["distress_tax_lien_owner"] == Decimal("4")
    assert result.components["distress_other_liens"] == Decimal("12")  # 6 * 3 capped at 12


def test_distress_listing_failures_cap_and_decay():
    listings = [ListingRecord(list_date=AS_OF - timedelta(days=90), delist_date=AS_OF, status="cancelled") for _ in range(3)]
    listings.append(ListingRecord(list_date=AS_OF - timedelta(days=10), status="active"))
    record = make_property(listings=listings)
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    assert result.components["distress_listing_failures"] == Decimal("12")  # 3 * 6 capped at 12


def test_distress_high_equity_bonus_requires_existing_distress():
    taxes = TaxBlock(delinquent_years=2)
    record = make_property(taxes=taxes)
    result = score(record, make_underwriting(record.property_id, equity_pct=".60"), uuid4(), as_of=AS_OF)
    assert result.components["distress_taxes_delinquent"] == Decimal("10")
    assert result.components["distress_high_equity_bonus"] == Decimal("5")

    record = make_property()
    result = score(record, make_underwriting(record.property_id, equity_pct=".60"), uuid4(), as_of=AS_OF)
    assert result.components["distress_high_equity_bonus"] == Decimal("0")


def test_distress_capped_at_100():
    foreclosure = ForeclosureState(stage="nts", nts_date=AS_OF, current_sale_date=AS_OF + timedelta(days=5),
                                   postponement_count=5, is_active=True)
    bankruptcies = [BankruptcyRecord(chapter="13", status="active", filing_date=AS_OF)] + [
        BankruptcyRecord(chapter="7", status="dismissed", filing_date=AS_OF) for _ in range(4)
    ]
    liens = [lien("property_tax", AttachmentBasis.RECORDED_AGAINST_PROPERTY, recording_date=AS_OF) for _ in range(4)]
    listings = [ListingRecord(list_date=AS_OF - timedelta(days=90), delist_date=AS_OF, status="expired") for _ in range(3)]
    record = make_property(
        foreclosure=foreclosure,
        bankruptcies=bankruptcies,
        liens=liens,
        listings=listings,
        taxes=TaxBlock(delinquent_years=3),
        ownership=OwnershipBlock(is_absentee=True, years_owned=Decimal("20")),
    )
    result = score(record, make_underwriting(record.property_id, equity_pct=".60"), uuid4(), as_of=AS_OF)
    assert result.distress == Decimal("100")


def test_risk_terms():
    liens = [
        lien("federal_tax", AttachmentBasis.RECORDED_AGAINST_PROPERTY, amount="20000"),
        lien("judgment", AttachmentBasis.OWNER_NAMED_ONLY, amount="15000", priority=2),
    ]
    record = make_property(
        liens=liens,
        hoa=HoaBlock(arrears=tv("500"), has_lien=True),
        ownership=OwnershipBlock(is_owner_occupied=True),
    )
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    # 6*2 liens + 10*1 owner-only>10k + 10*2 title flags (junior lien, HOA super-priority)
    # + 8 owner-occupied + 8 hoa arrears + 6 federal tax lien = 64; DCS is 100 so no low-confidence term.
    assert result.risk == Decimal("64")
    assert result.components["risk_lien_count"] == Decimal("12")
    assert result.components["risk_owner_only_liens"] == Decimal("10")
    assert result.components["risk_title_flags"] == Decimal("20")
    assert result.components["risk_owner_occupied"] == Decimal("8")
    assert result.components["risk_hoa_arrears"] == Decimal("8")
    assert result.components["risk_federal_tax_lien"] == Decimal("6")
    assert result.components["risk_low_confidence"] == Decimal("0")


def test_risk_postponement_title_flag_and_low_dcs():
    foreclosure = ForeclosureState(stage="nts", nts_date=AS_OF, current_sale_date=AS_OF + timedelta(days=10),
                                   postponement_count=3, is_active=True)
    record = make_property(foreclosure=foreclosure, data_quality=DataQualityBlock())
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    assert result.components["risk_title_flags"] == Decimal("10")
    assert result.components["risk_foreclosure_stage"] == Decimal("12")
    assert result.components["risk_low_confidence"] == Decimal("12")


def test_needs_review_caps_at_45_but_stays_rankable():
    record = make_property(data_quality=DataQualityBlock())  # DCS = 15 (only the no-conflict term)
    underwriting = make_underwriting(record.property_id, equity_pct=".60")
    strategies = [make_strategy(StrategyType.CASH, "150000", roi=".50", mao="195000", margin=".35")]
    result = score(record, underwriting, uuid4(), strategies, as_of=AS_OF)
    assert result.data_confidence == Decimal("15")
    assert result.fos == Decimal("100")
    assert result.overall == Decimal("45")  # 50 raw, capped
    assert "needs_review" in result.gates_applied
    assert result.is_rankable is True


def test_insufficient_data_is_unranked():
    record = make_property()
    underwriting = make_underwriting(record.property_id, status="insufficient_data")
    result = score(record, underwriting, uuid4(), as_of=AS_OF)
    assert "insufficient_data" in result.gates_applied
    assert result.is_rankable is False


def test_open_gating_flag_is_unranked():
    record = make_property(open_flags=[FlagSummary(type=FlagType.IDENTITY_CONFLICT, is_gating=True)])
    result = score(record, make_underwriting(record.property_id), uuid4(), as_of=AS_OF)
    assert "open_gating_flag" in result.gates_applied
    assert result.is_rankable is False


def test_config_override_changes_scores_without_code_change():
    record = make_property()
    underwriting = make_underwriting(record.property_id)
    strategies = [make_strategy(StrategyType.CASH, "75000")]
    default = score(record, underwriting, uuid4(), strategies, as_of=AS_OF)
    config = {"weights": {"overall": {"fos": "1.0", "distress": "0", "dcs": "0", "risk": "0"}}, "version": 7}
    overridden = score(record, underwriting, uuid4(), strategies, config=config, as_of=AS_OF)
    assert overridden.overall == overridden.fos == default.fos
    assert overridden.overall != default.overall
    # Untouched sections still fall back to defaults.
    assert overridden.data_confidence == default.data_confidence


def test_determinism():
    record = make_property(
        foreclosure=ForeclosureState(stage="nod", nod_date=AS_OF - timedelta(days=100), is_active=True),
        bankruptcies=[BankruptcyRecord(chapter="7", status="dismissed", filing_date=AS_OF - timedelta(days=400))],
        liens=[lien("judgment", AttachmentBasis.OWNER_NAMED_ONLY, amount="20000")],
        listings=[ListingRecord(list_date=AS_OF - timedelta(days=200), delist_date=AS_OF - timedelta(days=30), status="expired")],
    )
    underwriting = make_underwriting(record.property_id)
    strategies = [make_strategy(StrategyType.CASH, "80000")]
    config_id = uuid4()
    first = score(record, underwriting, config_id, strategies, as_of=AS_OF)
    second = score(record, underwriting, config_id, strategies, as_of=AS_OF)
    assert first.model_dump() == second.model_dump()


def test_components_expose_every_sub_term():
    record = make_property()
    result = score(record, make_underwriting(record.property_id), uuid4(), [make_strategy(StrategyType.CASH, "50000")], as_of=AS_OF)
    expected = {
        "profit", "roi", "equity_pct", "discount_to_value", "margin_of_safety",
        "fos_profit", "fos_roi", "fos_equity_pct", "fos_discount_to_value", "fos_margin_of_safety",
        "distress_foreclosure", "distress_prior_foreclosure_activity", "distress_bankruptcy_active",
        "distress_bankruptcy_prior", "distress_repeat_filings", "distress_tax_lien_property",
        "distress_tax_lien_owner", "distress_other_liens", "distress_taxes_delinquent", "distress_absentee",
        "distress_long_ownership", "distress_listing_failures", "distress_high_equity_bonus",
        "dcs_coverage", "dcs_corroboration", "dcs_recency", "dcs_conflict", "dcs_verification", "dcs_extraction",
        "risk_lien_count", "risk_active_bankruptcy", "risk_foreclosure_stage", "risk_owner_only_liens",
        "risk_title_flags", "risk_owner_occupied", "risk_hoa_arrears", "risk_material_conflicts",
        "risk_low_confidence", "risk_federal_tax_lien",
    }
    assert expected <= set(result.components)
    assert all(isinstance(value, Decimal) for value in result.components.values())


def test_recommended_strategy_near_ties_and_priority_tiebreak():
    record = make_property()
    underwriting = make_underwriting(record.property_id)
    strategies = [
        make_strategy(StrategyType.CASH, "150000"),
        make_strategy(StrategyType.FLIP, "145000"),   # 96.67 vs 100 -> within 5
        make_strategy(StrategyType.WHOLESALE, "100000"),  # 66.67 -> not a near-tie
        make_strategy(StrategyType.RENTAL, "200000", status="not_viable"),
    ]
    result = score(record, underwriting, uuid4(), strategies, as_of=AS_OF)
    assert result.recommended_strategy == StrategyType.CASH
    assert result.recommended_alternatives == [StrategyType.FLIP]

    tied = [make_strategy(StrategyType.FLIP, "100000"), make_strategy(StrategyType.CASH, "100000")]
    result = score(record, underwriting, uuid4(), tied, as_of=AS_OF)
    assert result.recommended_strategy == StrategyType.CASH  # priority order breaks the tie
    assert result.recommended_alternatives == [StrategyType.FLIP]


def test_no_viable_strategy():
    record = make_property()
    result = score(record, make_underwriting(record.property_id), uuid4(), [], as_of=AS_OF)
    assert result.recommended_strategy is None
    assert result.recommended_alternatives == []
    assert result.components["profit"] == Decimal("0")
    assert result.components["discount_to_value"] == Decimal("0")


def test_compute_ranks_orders_and_carries_prev_rank():
    a, b, c = uuid4(), uuid4(), uuid4()
    first = compute_ranks({a: Decimal("90"), b: Decimal("80"), c: Decimal("70")})
    assert [(row.property_id, row.rank, row.prev_rank) for row in first] == [(a, 1, None), (b, 2, None), (c, 3, None)]
    previous = {row.property_id: row.rank for row in first}
    second = compute_ranks({a: Decimal("90"), b: Decimal("95"), c: Decimal("60")}, previous=previous)
    assert [(row.property_id, row.rank, row.prev_rank) for row in second] == [(b, 1, 2), (a, 2, 1), (c, 3, 3)]


def test_rank_scope_executes_single_statement_with_scope_params():
    class FakeResult:
        rowcount = 3

    class FakeConnection:
        def __init__(self):
            self.calls = []

        def execute(self, clause, params=None):
            self.calls.append((clause, params))
            return FakeResult()

    conn = FakeConnection()
    scope_id = uuid4()
    written = rank_scope(conn, "batch", scope_id)
    assert written == 3
    assert len(conn.calls) == 1
    clause, params = conn.calls[0]
    sql = str(clause)
    assert "RANK() OVER (ORDER BY latest.overall DESC, latest.property_id)" in sql
    assert "previous.rank" in sql
    assert "INSERT INTO rankings" in sql
    assert "insufficient_data" in sql and "open_gating_flag" in sql
    assert params == {"scope_type": "batch", "scope_id": scope_id}


def test_default_config_is_spec_section_10():
    assert DEFAULT_CONFIG["weights"]["overall"] == {
        "fos": Decimal("0.50"), "distress": Decimal("0.20"), "dcs": Decimal("0.20"), "risk": Decimal("0.25")
    }
    assert DEFAULT_CONFIG["distress_points"]["nts_near"] == Decimal("30")
    assert DEFAULT_CONFIG["gates"]["dcs_cap_threshold"] == Decimal("40")
    assert DEFAULT_CONFIG["gates"]["dcs_cap"] == Decimal("45")
