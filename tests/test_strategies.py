from decimal import Decimal
from uuid import uuid4

from contracts import (AcquisitionCosts, AddressBlock, AssumptionSet, AttachmentBasis, CostBlock,
                       DataQualityBlock, ForeclosureState, HoaBlock, HoldingAssumptions,
                       LiabilityBlock, LienRecord, MortgageRecord, NormalizedProperty,
                       OwnershipBlock, PropertyAttributes, RentalBlock, RepairAssumptions,
                       ResaleAssumptions, Scenario, SourceKind, StrategyAssumptions, TaxBlock,
                       TrackedValue, UnderwritingResult, ValueBlock)
from strategies import (all_strategies, cash, flip, foreclosure, offer_grid, offer_point,
                        rental, short_sale_flag_requests, subject_to, wholesale)

D = Decimal
CENT = D(".01")


def assumptions(rental_extra: dict | None = None):
    rental = {"vacancy": D(".06"), "maintenance_pct": D(".08"), "management_pct": D(".08")}
    rental.update(rental_extra or {})
    return AssumptionSet(id=uuid4(), version=1, name="test",
                         acquisition=AcquisitionCosts(closing_pct=D(".01"), title_pct=D(".005"), escrow_flat=D("1500"),
                                                      financing_points=D(".02"), financing_flat=D("1200"), inspection_flat=D("600"),
                                                      legal_flat=D("1500"), acq_fee_pct=D(".01")),
                         repairs=RepairAssumptions(psf_by_condition={"moderate": D("42")}, low_multiplier=D(".75"),
                                                   high_multiplier=D("1.4"), regional_index=D("1")),
                         holding=HoldingAssumptions(insurance_pct_yr=D(".0035"), utilities_monthly=D("180"),
                                                    maintenance_pct_yr=D(".005"), acquisition_months=D("2"),
                                                    repair_months_by_condition={"moderate": D("4")}, market_days_default=60),
                         resale=ResaleAssumptions(commission_pct=D(".05"), seller_closing_pct=D(".01"),
                                                  concessions_pct=D(".01"), staging_flat=D("3500"), misc_pct=D(".0025")),
                         strategy=StrategyAssumptions(cash_target_margin=D(".2"),
                                                      flip_target_margin_by_arv_band={"default": D(".2")},
                                                      wholesale_investor_pct=D(".7"), min_assignment_spread=D("15000"),
                                                      hard_money={"rate": D(".1"), "points": D(".02"), "ltv": D(".85")},
                                                      rental=rental),
                         attachment_probability={AttachmentBasis.OWNER_NAMED_ONLY: D(".35"), AttachmentBasis.UNKNOWN: D(".5")},
                         unknown_lien_medians={"judgment": D("18000")}, valuation_weights={"manual": D("1")})


def tv(value):
    return TrackedValue(value=D(value), confidence=.9, source_kind=SourceKind.REPORT, is_estimated=False)


def make_property(**kwargs) -> NormalizedProperty:
    defaults = dict(property_id=uuid4(), address=AddressBlock(line1="1 Main St"),
                    attributes=PropertyAttributes(sqft=tv("1800")),
                    data_quality=DataQualityBlock(critical_field_coverage=D(".9"), mean_extraction_confidence=D(".9")),
                    resolution_version="test")
    defaults.update(kwargs)
    return NormalizedProperty(**defaults)


def make_underwriting(property_id, v_low=D("250000"), v_exp=D("300000"), v_high=D("350000"),
                      confirmed=D("100000"), potential=D("20000"), breakdown=None, confidence=D(".7")):
    values = {Scenario.CONSERVATIVE: v_low, Scenario.EXPECTED: v_exp, Scenario.OPTIMISTIC: v_high}
    costs = {scenario: CostBlock(acquisition=D("2100"), repairs=D("20000"), holding=D("360"),
                                 resale=value * D(".07"))
             for scenario, value in values.items()}
    return UnderwritingResult(property_id=property_id, assumption_set_id=uuid4(), engine_version="test", status="ok",
                              value=ValueBlock(v_low=v_low, v_expected=v_exp, v_high=v_high, arv_by_scenario=values),
                              liabilities=LiabilityBlock(confirmed=confirmed, potential=potential,
                                                         maximum=confirmed + potential, breakdown=breakdown or []),
                              costs=costs, debt_data_present=True, confidence=confidence)


def test_cash_resale_uses_cost_block_with_commission():
    property = make_property()
    underwriting = make_underwriting(property.property_id)
    result = cash(property, underwriting, assumptions(), D("150000"), Scenario.EXPECTED)
    # costs.resale already carries commission + seller closing + concessions (7% of 300000)
    assert result.metrics["resale_cost"] == D("21000")
    assert result.metrics["resale_cost"] != D("300000") * D(".01")  # regression: not seller_closing_pct alone
    assert result.all_in_basis == D("150000") + D("20000") + D("360") + D("2100")
    assert result.profit == D("300000") - D("21000") - D("172460")
    assert result.mao == D("300000") * D(".8") - D("20000") - D("360") - D("2100") - D("21000")
    assert result.status == "viable"


def test_cash_unavailable_without_costs():
    property = make_property()
    underwriting = make_underwriting(property.property_id)
    underwriting.costs = {}
    result = cash(property, underwriting, assumptions(), D("150000"), Scenario.EXPECTED)
    assert result.status == "unavailable"


def test_flip_accrues_interest_and_true_coc():
    property = make_property()
    underwriting = make_underwriting(property.property_id)
    result = flip(property, underwriting, assumptions(), D("150000"), Scenario.EXPECTED)
    months = D("2") + D("4") + D("60") / D("30")  # acquisition + repair + market time
    loan = D(".85") * (D("150000") + D("20000"))
    financing = loan * (D(".02") + D(".1") * months / D("12"))
    assert result.metrics["holding_months"] == D("8")
    assert result.metrics["loan"] == loan
    assert result.metrics["financing"] == financing
    assert result.metrics["financing"] != D("150000") * D(".02")  # regression: interest now accrues
    basis = D("150000") + D("20000") + D("360") + D("2100") + D("21000") + financing
    assert result.all_in_basis == basis
    cash_invested = basis - loan
    assert result.metrics["coc"] == result.profit / cash_invested
    assert result.metrics["coc"] != result.profit / D("150000")  # regression: not profit/purchase
    # MAO solves financing self-consistently: paying the MAO leaves the target margin
    carry = D(".02") + D(".1") * months / D("12")
    target = D("300000") * D(".8") - D("20000") - D("360") - D("21000") - D("2100")
    assert result.mao == (target - D(".85") * carry * D("20000")) / (D("1") + D(".85") * carry)


def test_flip_unavailable_without_sqft():
    property = make_property(attributes=PropertyAttributes())
    underwriting = make_underwriting(property.property_id)
    result = flip(property, underwriting, assumptions(), D("150000"))
    assert result.status == "unavailable"
    assert result.unavailable_reason == "no_sqft_data"


def test_wholesale_gates_on_data_confidence_0_to_100():
    property = make_property()
    underwriting = make_underwriting(property.property_id, confidence=D(".7"))
    # threshold = 300000 * .7 - 20000 = 190000; contract 170000 -> spread 20000 >= 15000
    assert wholesale(underwriting, assumptions(), D("170000"), Scenario.EXPECTED).status == "viable"
    # same spread, but Data Confidence 50 (< 60) blocks the deal
    low = make_underwriting(property.property_id, confidence=D(".5"))
    result = wholesale(low, assumptions(), D("170000"), Scenario.EXPECTED)
    assert result.status == "not_viable"
    assert result.metrics["data_confidence"] == D("50")
    # caller-passed Data Confidence wins over the underwriting-derived value
    overridden = wholesale(low, assumptions(), D("170000"), Scenario.EXPECTED, data_confidence=D("80"))
    assert overridden.status == "viable"
    # spread below the minimum assignment fee
    assert wholesale(underwriting, assumptions(), D("180000"), Scenario.EXPECTED).status == "not_viable"


def test_rental_noi_opex_and_leverage():
    property = make_property(taxes=TaxBlock(annual_taxes=tv("3600")), hoa=HoaBlock(monthly_dues=tv("100")),
                             rental=RentalBlock(rent_estimate=tv("2000")))
    underwriting = make_underwriting(property.property_id)
    price = D("250000")
    levered = assumptions(rental_extra={"reserves_pct": D(".05"), "ltv": D(".75"), "rate": D(".07"), "amort_years": D("30")})
    result = rental(property, underwriting, levered, price, Scenario.EXPECTED)
    egi = D("24000") * D(".94")
    opex = D("3600") + price * D(".0035") + D("1200") + egi * (D(".08") + D(".08") + D(".05"))
    assert result.metrics["egi"] == egi
    assert result.metrics["opex"] == opex
    noi = egi - opex
    assert result.metrics["noi"] == noi  # vacancy counted exactly once (in EGI)
    assert result.metrics["cap_rate"] == noi / price
    debt_service = result.metrics["debt_service"]
    assert D("14000") < debt_service < D("16000")  # 75% LTV, 7%, 30yr amortization
    assert result.metrics["cash_flow"] == noi - debt_service
    assert result.metrics["dscr"] == noi / debt_service
    invested = price - price * D(".75") + D("2100") + D("20000")
    assert result.metrics["coc"] == result.metrics["cash_flow"] / invested
    assert result.status == ("viable" if noi - debt_service >= 0 else "not_viable")


def test_rental_unlevered_has_no_dscr():
    property = make_property(rental=RentalBlock(rent_estimate=tv("2000")))
    underwriting = make_underwriting(property.property_id)
    result = rental(property, underwriting, assumptions(), D("150000"), Scenario.EXPECTED)
    assert result.metrics["debt_service"] == D("0")
    assert result.metrics["dscr"] is None
    assert result.metrics["cash_flow"] == result.metrics["noi"]


def test_rental_never_infers_rent_from_value():
    property = make_property()  # has value data via underwriting, no rent
    underwriting = make_underwriting(property.property_id)
    result = rental(property, underwriting, assumptions(), D("150000"), Scenario.EXPECTED)
    assert result.status == "unavailable"
    assert result.unavailable_reason == "no_rent_data"


def test_rental_unavailable_without_sqft():
    property = make_property(attributes=PropertyAttributes(), rental=RentalBlock(rent_estimate=tv("2000")))
    underwriting = make_underwriting(property.property_id)
    result = rental(property, underwriting, assumptions(), D("150000"), Scenario.EXPECTED)
    assert result.status == "unavailable"
    assert result.unavailable_reason == "no_sqft_data"


def _subject_to_property(**kwargs):
    mortgage = MortgageRecord(position="1", lender="Bank", rate=D(".04"), estimated_balance=tv("160000"))
    defaults = dict(mortgages=[mortgage], taxes=TaxBlock(delinquent_amount=tv("5000")))
    defaults.update(kwargs)
    return make_property(**defaults)


def test_subject_to_fires_on_all_four_conditions():
    property = _subject_to_property()
    underwriting = make_underwriting(property.property_id, v_exp=D("220000"))
    result = subject_to(property, underwriting, assumptions(), D("160000"), Scenario.EXPECTED)
    assert result.status == "requires_human_review"
    text = " ".join(result.notices)
    assert "Due-on-sale" in text and "Legal review" in text
    assert result.metrics["balance_to_value"] == D("160000") / D("220000")


def test_subject_to_silent_when_conditions_fail():
    property = _subject_to_property()
    underwriting = make_underwriting(property.property_id, v_exp=D("220000"))
    # note rate only 100bps below the 10% market proxy
    property.mortgages[0].rate = D(".09")
    assert subject_to(property, underwriting, assumptions(), D("160000"), Scenario.EXPECTED).status == "not_viable"
    # acceleration active
    accelerated = _subject_to_property(foreclosure=ForeclosureState(stage="nod", is_active=True))
    assert subject_to(accelerated, underwriting, assumptions(), D("160000"), Scenario.EXPECTED).status == "not_viable"
    # no distress
    calm = _subject_to_property(taxes=TaxBlock())
    assert subject_to(calm, underwriting, assumptions(), D("160000"), Scenario.EXPECTED).status == "not_viable"


def _foreclosure_property(**kwargs):
    defaults = dict(
        foreclosure=ForeclosureState(stage="nts", is_active=True, published_bid=tv("100000"), postponement_count=3),
        liens=[LienRecord(lien_type="judgment", amount=tv("8000"), attachment_basis=AttachmentBasis.RECORDED_AGAINST_PROPERTY, attachment_confidence=.9),
               LienRecord(lien_type="irs", amount=tv("12000"), attachment_basis=AttachmentBasis.RECORDED_AGAINST_PROPERTY, attachment_confidence=.9)],
        hoa=HoaBlock(arrears=tv("3000"), has_lien=True),
        taxes=TaxBlock(delinquent_amount=tv("5000")),
        ownership=OwnershipBlock(is_owner_occupied=True),
        condition=None,
        attributes=PropertyAttributes(sqft=tv("1000")))
    defaults.update(kwargs)
    return make_property(**defaults)


def test_foreclosure_full_math_and_flags():
    property = _foreclosure_property()
    underwriting = make_underwriting(property.property_id, confidence=D(".5"))
    result = foreclosure(property, underwriting, assumptions(), D("100000"), Scenario.EXPECTED)
    # interior unknown -> high repairs in every scenario: 1000 sqft * $42 * 1.4
    assert result.metrics["repairs"] == D("58800")
    # published bid + irs + hoa arrears + delinquent taxes + transfer (escrow + legal)
    assert result.metrics["total_obligations"] == D("100000") + D("12000") + D("3000") + D("5000") + D("3000")
    expected_spread = D("250000") - D("123000") - D("58800") - D("360")  # v_conservative - obligations - repairs - holding
    assert result.profit == expected_spread
    assert result.status == "viable"
    for flag in ("risk_junior_liens", "risk_irs_lien", "risk_hoa_super_priority",
                 "risk_owner_occupied", "risk_interior_unknown", "risk_postponement"):
        assert result.metrics[flag] == D("1"), flag
    assert result.metrics["score_cap"] == D("70")  # Data Confidence 50 < 75
    assert any("capped" in notice for notice in result.notices)
    assert any("source: liens" in notice for notice in result.notices)


def test_foreclosure_no_cap_at_high_confidence_and_known_interior():
    from contracts import ConditionSignal
    property = _foreclosure_property(condition=ConditionSignal(condition="moderate"),
                                     foreclosure=ForeclosureState(stage="nts", is_active=True, published_bid=tv("100000")))
    underwriting = make_underwriting(property.property_id, confidence=D(".8"))
    result = foreclosure(property, underwriting, assumptions(), D("100000"), Scenario.EXPECTED)
    assert result.metrics["repairs"] == D("42000")  # no high multiplier when interior is known
    assert result.metrics["score_cap"] is None  # Data Confidence 80 >= 75
    assert result.metrics["risk_interior_unknown"] == D("0")
    assert result.metrics["risk_postponement"] == D("0")


def test_foreclosure_unavailable_without_active_sale_or_bid():
    property = make_property()
    underwriting = make_underwriting(property.property_id)
    assert foreclosure(property, underwriting, assumptions(), D("1"), Scenario.EXPECTED).unavailable_reason == "no_active_foreclosure"
    no_bid = _foreclosure_property(foreclosure=ForeclosureState(stage="nod", is_active=True))
    assert foreclosure(no_bid, underwriting, assumptions(), D("1"), Scenario.EXPECTED).unavailable_reason == "no_published_bid"


def test_offer_grid_points_and_costs():
    property = make_property()
    breakdown = [{"label": "judgment", "amount": D("20000"), "basis": "owner_named_only", "is_estimated": False}]
    underwriting = make_underwriting(property.property_id, breakdown=breakdown)
    grid = offer_grid(underwriting, property.property_id, assumptions(), D("150000"))
    assert grid.interpolatable is True
    by_scenario = {scenario: [p for p in grid.points if p.scenario == scenario] for scenario in Scenario}
    for scenario, points in by_scenario.items():
        plain = [p for p in points if p.label is None]
        assert len(plain) >= 9
        assert all(p.offer_price % D("5000") == 0 for p in plain)  # rounded to $5k
        assert any(p.label == "mao_cash" for p in points)
        assert any(p.label == "mao_flip" for p in points)
    point = next(p for p in by_scenario[Scenario.EXPECTED] if p.label is None)
    offer = point.offer_price
    # expected proceeds weight potential liabilities by attachment_probability (0.35 owner-only)
    assert point.proceeds_expected == offer - D("100000") - offer * D(".01") - D("20000") * D(".35")
    assert point.proceeds_low == offer - D("100000") - offer * D(".01") - D("20000")
    assert point.proceeds_high == offer - D("100000") - offer * D(".01")
    # buyer basis carries acquisition + repairs + holding; profit nets resale
    assert point.buyer_basis == offer + D("2100") + D("20000") + D("360")
    assert point.profit == D("300000") - D("21000") - point.buyer_basis


def test_offer_grid_linearity_licenses_interpolation():
    property = make_property()
    breakdown = [{"label": "judgment", "amount": D("20000"), "basis": "owner_named_only", "is_estimated": False}]
    underwriting = make_underwriting(property.property_id, breakdown=breakdown)
    assumption_set = assumptions()
    grid = offer_grid(underwriting, property.property_id, assumption_set, D("150000"))
    for scenario in Scenario:
        points = sorted((p for p in grid.points if p.scenario == scenario), key=lambda p: p.offer_price)
        for low_point, high_point in zip(points, points[1:]):
            midpoint = (low_point.offer_price + high_point.offer_price) / 2
            computed = offer_point(underwriting, assumption_set, midpoint, scenario)
            for field in ("proceeds_low", "proceeds_expected", "proceeds_high", "buyer_basis", "profit", "closing_costs"):
                interpolated = (getattr(low_point, field) + getattr(high_point, field)) / 2
                assert getattr(computed, field).quantize(CENT) == interpolated.quantize(CENT), (scenario, field, midpoint)


def test_offer_grid_short_sale_flag():
    property = make_property()
    underwriting = make_underwriting(property.property_id, confirmed=D("400000"))  # debt above any offer
    grid = offer_grid(underwriting, property.property_id, assumptions(), D("150000"))
    assert any(point.is_short_sale for point in grid.points)
    requests = short_sale_flag_requests(grid)
    assert requests
    assert all(request.flag_type.value == "short_sale_candidate" for request in requests)
    assert len({request.dedupe_key for request in requests}) == len(requests)


def test_offer_grid_empty_without_value():
    property = make_property()
    underwriting = make_underwriting(property.property_id)
    underwriting.value = ValueBlock()
    grid = offer_grid(underwriting, property.property_id, assumptions(), D("150000"))
    assert grid.points == []


def test_all_strategies_six_by_three_and_deterministic():
    property = _foreclosure_property(rental=RentalBlock(rent_estimate=tv("2000")))
    underwriting = make_underwriting(property.property_id)
    assumption_set = assumptions()
    first = all_strategies(property, underwriting, assumption_set, D("150000"))
    second = all_strategies(property, underwriting, assumption_set, D("150000"))
    assert len(first) == 18
    assert [item.model_dump() for item in first] == [item.model_dump() for item in second]
    strategies_seen = {item.strategy for item in first}
    assert len(strategies_seen) == 6


def test_missing_sqft_leaves_cash_and_foreclosure_computed():
    property = _foreclosure_property(attributes=PropertyAttributes(), rental=RentalBlock(rent_estimate=tv("2000")))
    underwriting = make_underwriting(property.property_id)
    assumption_set = assumptions()
    assert flip(property, underwriting, assumption_set, D("150000"), Scenario.EXPECTED).status == "unavailable"
    assert rental(property, underwriting, assumption_set, D("150000"), Scenario.EXPECTED).status == "unavailable"
    assert cash(property, underwriting, assumption_set, D("150000"), Scenario.EXPECTED).status in ("viable", "not_viable")
    assert foreclosure(property, underwriting, assumption_set, D("150000"), Scenario.EXPECTED).status in ("viable", "not_viable")
