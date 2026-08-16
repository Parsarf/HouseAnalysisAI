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
from strategies.engine import data_confidence

D = Decimal
CENT = D(".01")
Q6 = D(".000001")
CONS, EXP, OPT = Scenario.CONSERVATIVE, Scenario.EXPECTED, Scenario.OPTIMISTIC
# finance-consistent cost blocks: repairs base 20000 x {1.4, 1, 0.75}; holding =
# (v * .0085/12 + 180) x months, months = 8 x {1.5, 1, .75} (moderate condition)
REPAIRS = {CONS: D("28000"), EXP: D("20000"), OPT: D("15000")}
HOLDING = {CONS: D("4284.96"), EXP: D("3140.00"), OPT: D("2567.52")}
ACQUISITION = D("2100")
RESALE_PCT = D(".0725")  # commission .05 + seller closing .01 + concessions .01 + misc .0025


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
                      confirmed=D("100000"), potential=D("20000"), breakdown=None, confidence=D(".7"),
                      with_repairs=True):
    values = {CONS: v_low, EXP: v_exp, OPT: v_high}
    costs = {scenario: CostBlock(acquisition=ACQUISITION, repairs=REPAIRS[scenario] if with_repairs else D("0"),
                                 holding=HOLDING[scenario], resale=(value * RESALE_PCT).quantize(CENT))
             for scenario, value in values.items()}
    arv = {scenario: (value + REPAIRS[scenario]).quantize(CENT) if with_repairs else None
           for scenario, value in values.items()}
    return UnderwritingResult(property_id=property_id, assumption_set_id=uuid4(), engine_version="test", status="ok",
                              value=ValueBlock(v_low=v_low, v_expected=v_exp, v_high=v_high, arv_by_scenario=arv),
                              liabilities=LiabilityBlock(confirmed=confirmed, potential=potential,
                                                         maximum=confirmed + potential, breakdown=breakdown or []),
                              costs=costs, debt_data_present=True, confidence=confidence)


def test_cash_profit_uses_as_is_value_and_full_resale_pct():
    property = make_property()
    underwriting = make_underwriting(property.property_id)
    result = cash(property, underwriting, assumptions(), D("150000"), EXP)
    all_in = D("150000") + ACQUISITION + D("20000") + HOLDING[EXP]
    assert result.all_in_basis == all_in
    # profit = V*(1-resale_pct) - all_in with the as-is value, not ARV
    assert result.profit == (D("300000") * (D("1") - RESALE_PCT) - all_in).quantize(CENT)
    assert result.mao == D("300000") * D(".8") - D("20000") - HOLDING[EXP] - ACQUISITION - D("300000") * RESALE_PCT
    assert result.roi == (result.profit / all_in).quantize(Q6)
    assert result.inputs_echo == {"purchase_price": D("150000")}


def test_cash_unavailable_without_value():
    property = make_property()
    underwriting = make_underwriting(property.property_id)
    underwriting.value = ValueBlock()
    underwriting.costs = {}
    result = cash(property, underwriting, assumptions(), D("0"), EXP)
    assert result.status == "unavailable"
    assert result.unavailable_reason == "no_value_data"
    assert result.inputs_echo == {}  # price 0 carries no information


def test_flip_financing_staging_and_true_coc():
    property = make_property()
    underwriting = make_underwriting(property.property_id)
    result = flip(property, underwriting, assumptions(), D("150000"), EXP)
    arv = D("320000")  # 300000 + 20000 expected repairs
    months = D("8")  # (2 acquisition + 4 moderate repair + 60/30 market) x 1.0 expected
    loan = D(".85") * (D("150000") + D("20000"))
    financing = (D(".02") * loan + D("1200") + loan * D(".1") / D("12") * months).quantize(CENT)
    assert result.metrics["loan"] == loan
    assert result.metrics["financing"] == financing
    assert result.metrics["financing"] != D("150000") * D(".02")  # regression: interest accrues over holding
    resale_flip = (arv * RESALE_PCT + D("3500")).quantize(CENT)  # staging is flip-only
    all_in = D("150000") + D("20000") + HOLDING[EXP] + financing + ACQUISITION + resale_flip
    assert result.all_in_basis == all_in
    assert result.profit == arv - all_in
    down = D("150000") + D("20000") - loan
    coc_denominator = down + HOLDING[EXP] + ACQUISITION
    assert result.metrics["coc"] == (result.profit / coc_denominator).quantize(Q6)
    assert result.metrics["margin"] == (result.profit / arv).quantize(Q6)
    # MAO solves financing self-consistently: paying the MAO leaves the target margin
    coeff = D(".85") * (D(".02") + D(".1") / D("12") * months)
    k_const = (arv * D(".8") - D("20000") - HOLDING[EXP] - resale_flip - ACQUISITION).quantize(CENT)
    assert result.mao == ((k_const - coeff * D("20000") - D("1200")) / (D("1") + coeff)).quantize(CENT)


def test_flip_unavailable_without_sqft():
    property = make_property(attributes=PropertyAttributes())
    underwriting = make_underwriting(property.property_id, with_repairs=False)
    result = flip(property, underwriting, assumptions(), D("150000"), EXP)
    assert result.status == "unavailable"
    assert result.unavailable_reason == "no_sqft_data"


def test_wholesale_gates_on_data_confidence_0_to_100():
    property = make_property()
    underwriting = make_underwriting(property.property_id, confidence=D(".7"))
    # threshold = ARV * .7 - repairs = 320000 * .7 - 20000 = 204000; contract 170000 -> spread 34000
    result = wholesale(underwriting, assumptions(), D("170000"), EXP)
    assert result.status == "viable"
    assert result.metrics["investor_threshold"] == D("204000")
    assert result.mao == D("204000") - D("15000")  # max_contract: threshold minus target fee
    assert result.metrics["dcs"] == D("70")  # fallback: underwriting.confidence x 100
    # same spread, but Data Confidence 50 (< 60) blocks the deal
    low = make_underwriting(property.property_id, confidence=D(".5"))
    assert wholesale(low, assumptions(), D("170000"), EXP).status == "not_viable"
    # caller-passed Data Confidence wins over the underwriting-derived fallback
    overridden = wholesale(low, assumptions(), D("170000"), EXP, data_confidence=D("80"))
    assert overridden.status == "viable"
    assert overridden.metrics["dcs"] == D("80")
    # spread below the minimum assignment fee
    assert wholesale(underwriting, assumptions(), D("190000"), EXP).status == "not_viable"


def test_data_confidence_formula():
    property = make_property()  # coverage .9, no corroboration, no date, no conflicts, mean .9
    assert data_confidence(property) == D("100") * (D(".30") * D(".9") + D(".15") + D(".05") * D(".9"))


def test_rental_noi_opex_and_unlevered_returns():
    property = make_property(taxes=TaxBlock(annual_taxes=tv("3600")), hoa=HoaBlock(monthly_dues=tv("100")),
                             rental=RentalBlock(rent_estimate=tv("2000")))
    underwriting = make_underwriting(property.property_id)
    price = D("250000")
    result = rental(property, underwriting, assumptions(), price, EXP)
    egi = D("24000") * D(".94")
    # insurance is charged on the as-is value; reserves 5% of EGI; vacancy counted once
    opex = (D("3600") + D("300000") * D(".0035") + D("1200") + egi * (D(".08") + D(".08") + D(".05"))).quantize(CENT)
    noi = (egi - opex).quantize(CENT)
    assert result.metrics["egi"] == egi
    assert result.metrics["opex"] == opex
    assert result.metrics["noi"] == noi
    assert result.metrics["cap_rate"] == (noi / price).quantize(Q6)
    assert result.metrics["cash_flow"] == noi  # no rental debt parameterized
    assert result.metrics["dscr"] is None
    invested = price + ACQUISITION + D("20000")
    assert result.metrics["coc"] == (noi / invested).quantize(Q6)
    assert result.profit == noi
    assert result.status == "viable"


def test_rental_levered_debt_service_dscr_and_coc():
    property = make_property(rental=RentalBlock(rent_estimate=tv("2000")))
    underwriting = make_underwriting(property.property_id)
    price = D("250000")
    levered = assumptions(rental_extra={"ltv": D(".75"), "rate": D(".07"), "amort_years": D("30")})
    result = rental(property, underwriting, levered, price, EXP)
    noi = result.metrics["noi"]
    cash_flow = result.metrics["cash_flow"]
    debt_service = noi - cash_flow
    assert D("14000") < debt_service < D("16000")  # 75% LTV, 7%, 30yr amortization
    assert result.metrics["dscr"] == (noi / debt_service).quantize(Q6)
    invested = price - D("187500") + ACQUISITION + D("20000")
    assert result.metrics["coc"] == (cash_flow / invested).quantize(Q6)
    assert result.status == ("viable" if cash_flow > 0 else "not_viable")


def test_rental_never_infers_rent_from_value():
    property = make_property()  # has value data via underwriting, no rent
    underwriting = make_underwriting(property.property_id)
    result = rental(property, underwriting, assumptions(), D("150000"), EXP)
    assert result.status == "unavailable"
    assert result.unavailable_reason == "no_rent_data"


def test_rental_unavailable_without_sqft():
    property = make_property(attributes=PropertyAttributes(), rental=RentalBlock(rent_estimate=tv("2000")))
    underwriting = make_underwriting(property.property_id)
    result = rental(property, underwriting, assumptions(), D("150000"), EXP)
    assert result.status == "unavailable"
    assert result.unavailable_reason == "no_sqft_data"


def _subject_to_property(**kwargs):
    mortgage = MortgageRecord(position="1", lender="Bank", rate=D(".04"), estimated_balance=tv("160000"))
    defaults = dict(mortgages=[mortgage], taxes=TaxBlock(delinquent_amount=tv("5000")))
    defaults.update(kwargs)
    return make_property(**defaults)


def test_subject_to_detection_only_reports_four_conditions():
    property = _subject_to_property()
    underwriting = make_underwriting(property.property_id)
    result = subject_to(property, underwriting, assumptions(), D("160000"), EXP)
    assert result.status == "requires_human_review"
    assert result.notices == ["detection only - due-on-sale / legal review required"]
    # no market-rate input exists in the contract -> the rate condition is null
    assert result.metrics["condition_rate_200bps_below_market"] is None
    assert result.metrics["condition_balance_le_80pct_value"] == D("1")  # 160000 <= 300000 * .8
    assert result.metrics["condition_no_acceleration"] == D("1")
    assert result.metrics["condition_distress_present"] == D("1")  # delinquent taxes


def test_subject_to_acceleration_and_balance_conditions():
    accelerated = _subject_to_property(foreclosure=ForeclosureState(stage="nod", is_active=True))
    underwriting = make_underwriting(accelerated.property_id)
    result = subject_to(accelerated, underwriting, assumptions(), D("160000"), EXP)
    assert result.status == "requires_human_review"  # detection only, always human review
    assert result.metrics["condition_no_acceleration"] == D("0")  # active foreclosure
    # balance above 80% of value
    high_balance = _subject_to_property()
    high_balance.mortgages[0].estimated_balance = tv("260000")
    result = subject_to(high_balance, underwriting, assumptions(), D("160000"), EXP)
    assert result.metrics["condition_balance_le_80pct_value"] == D("0")


def _foreclosure_property(**kwargs):
    defaults = dict(
        foreclosure=ForeclosureState(stage="nts", is_active=True, published_bid=tv("100000"), postponement_count=3),
        mortgages=[MortgageRecord(position="1", estimated_balance=tv("180000")),
                   MortgageRecord(position="2", estimated_balance=tv("20000"))],
        liens=[LienRecord(lien_type="judgment", amount=tv("8000"), attachment_basis=AttachmentBasis.RECORDED_AGAINST_PROPERTY, attachment_confidence=.9),
               LienRecord(lien_type="federal_tax", amount=tv("12000"), attachment_basis=AttachmentBasis.RECORDED_AGAINST_PROPERTY, attachment_confidence=.9)],
        hoa=HoaBlock(arrears=tv("3000"), has_lien=True),
        taxes=TaxBlock(annual_taxes=tv("3600"), delinquent_amount=tv("5000")),
        ownership=OwnershipBlock(is_owner_occupied=True),
        condition=None,
        attributes=PropertyAttributes(sqft=tv("1000")))
    defaults.update(kwargs)
    return make_property(**defaults)


def test_foreclosure_full_math_and_flags():
    property = _foreclosure_property()
    underwriting = make_underwriting(property.property_id)
    result = foreclosure(property, underwriting, assumptions(), D("100000"), EXP)
    # published bid + junior mortgage + recorded liens + delinquent taxes
    assert result.metrics["total_obligations"] == D("100000") + D("20000") + D("8000") + D("12000") + D("5000")
    # auction holding: 2 months of conservative monthly holding
    monthly = (D("3600") / D("12") + D("250000") * D(".0085") / D("12") + D("180")).quantize(CENT)
    assert result.metrics["auction_holding"] == (monthly * D("2")).quantize(CENT)
    # interior unknown -> conservative (high) repairs in every scenario
    expected_spread = (D("250000") - D("145000") - D("28000") - result.metrics["auction_holding"]).quantize(CENT)
    assert result.profit == expected_spread
    assert result.metrics["spread"] == expected_spread
    assert result.status == "viable"
    for flag in ("flag_junior_liens_present", "flag_irs_lien", "flag_hoa_super_priority",
                 "flag_owner_occupied", "flag_interior_unknown", "flag_postponements_ge_3"):
        assert result.metrics[flag] == D("1"), flag
    # spec S8: each flag is a boolean with a source
    assert len(result.notices) == 6
    assert "flag_irs_lien (source: liens)" in result.notices
    assert "flag_owner_occupied (source: ownership)" in result.notices


def test_foreclosure_known_interior_uses_scenario_repairs():
    from contracts import ConditionSignal
    property = _foreclosure_property(condition=ConditionSignal(condition="moderate"),
                                     foreclosure=ForeclosureState(stage="nts", is_active=True, published_bid=tv("100000")))
    underwriting = make_underwriting(property.property_id)
    result = foreclosure(property, underwriting, assumptions(), D("100000"), EXP)
    expected_spread = (D("250000") - D("145000") - D("20000") - result.metrics["auction_holding"]).quantize(CENT)
    assert result.profit == expected_spread
    assert result.metrics["flag_interior_unknown"] == D("0")
    assert result.metrics["flag_postponements_ge_3"] == D("0")


def test_foreclosure_bid_falls_back_to_first_balance():
    property = _foreclosure_property(foreclosure=ForeclosureState(stage="nod", is_active=True),
                                     mortgages=[MortgageRecord(position="1", estimated_balance=tv("180000"))],
                                     liens=[], hoa=HoaBlock(), ownership=OwnershipBlock())
    underwriting = make_underwriting(property.property_id)
    result = foreclosure(property, underwriting, assumptions(), D("100000"), EXP)
    assert result.metrics["total_obligations"] == D("180000") + D("5000")


def test_foreclosure_unavailable_without_active_sale():
    property = make_property()
    underwriting = make_underwriting(property.property_id)
    result = foreclosure(property, underwriting, assumptions(), D("1"), EXP)
    assert result.status == "unavailable"
    assert result.unavailable_reason == "no_active_foreclosure"


def test_offer_grid_points_and_costs():
    property = make_property()
    breakdown = [{"label": "mortgage:1", "amount": D("100000"), "basis": "recorded", "is_estimated": False},
                 {"label": "judgment", "amount": D("20000"), "basis": "owner_named_only", "is_estimated": False}]
    underwriting = make_underwriting(property.property_id, breakdown=breakdown)
    grid = offer_grid(underwriting, property.property_id, assumptions(), D("150000"))
    assert grid.interpolatable is True
    by_scenario = {scenario: [p for p in grid.points if p.scenario == scenario] for scenario in Scenario}
    for scenario, points in by_scenario.items():
        plain = [p for p in points if p.label is None]
        assert len(plain) == 9
        assert all(p.offer_price % D("5000") == 0 for p in plain)  # rounded to $5k
        assert any(p.label == "mao_cash" for p in points)
        assert any(p.label == "mao_flip" for p in points)
        assert points == sorted(points, key=lambda p: p.offer_price)  # sorted by offer
    point = next(p for p in by_scenario[EXP] if p.label is None)
    offer = point.offer_price
    # $1,200 payoff fees because an open mortgage exists; closing = title % + escrow flat
    closing = (offer * D(".005") + D("1500")).quantize(CENT)
    assert point.confirmed_payoffs == D("101200")
    assert point.closing_costs == closing
    high = (offer - D("101200") - closing).quantize(CENT)
    assert point.proceeds_high == high
    # expected proceeds weight potential liabilities by attachment_probability (0.35 owner-only)
    assert point.proceeds_expected == high - (D("20000") * D(".35")).quantize(CENT)
    assert point.proceeds_low == high - D("20000")
    # buyer basis carries acquisition + repairs + holding; profit nets resale on the as-is value
    assert point.buyer_basis == offer + ACQUISITION + D("20000") + HOLDING[EXP]
    assert point.profit == (D("300000") * (D("1") - RESALE_PCT) - point.buyer_basis).quantize(CENT)
    assert point.roi == (point.profit / point.buyer_basis).quantize(Q6)
    # MAO markers match the strategy results for the same scenario
    results = all_strategies(property, underwriting, assumptions(), D("225000"))
    maos = {(r.strategy.value, r.scenario): r.mao for r in results}
    for marker, key in (("mao_cash", "cash"), ("mao_flip", "flip")):
        marked = next(p for p in by_scenario[EXP] if p.label == marker)
        assert marked.offer_price == maos[(key, EXP)]


def test_offer_grid_mao_cash_value():
    property = make_property()
    underwriting = make_underwriting(property.property_id)
    grid = offer_grid(underwriting, property.property_id, assumptions(), D("150000"))
    marked = next(p for p in grid.points if p.label == "mao_cash" and p.scenario == EXP)
    assert marked.offer_price == D("300000") * D(".8") - D("20000") - HOLDING[EXP] - ACQUISITION - D("300000") * RESALE_PCT


def test_offer_grid_linearity_licenses_interpolation():
    property = make_property()
    breakdown = [{"label": "judgment", "amount": D("20000"), "basis": "owner_named_only", "is_estimated": False}]
    underwriting = make_underwriting(property.property_id, breakdown=breakdown)
    assumption_set = assumptions()
    grid = offer_grid(underwriting, property.property_id, assumption_set, D("150000"))
    for scenario in Scenario:
        # spec S9.3: the slider snaps to the 9 grid points and interpolates between
        # them exactly; MAO markers are display annotations, not snap points (their
        # unrounded offers can land interpolation on a half-cent rounding boundary)
        points = sorted((p for p in grid.points if p.scenario == scenario and p.label is None),
                        key=lambda p: p.offer_price)
        assert len(points) == 9
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
    assert len({item.strategy for item in first}) == 6
    # all_strategies passes the record-derived DCS into the wholesale gate
    wholesale_result = next(r for r in first if r.strategy.value == "wholesale")
    assert wholesale_result.metrics["dcs"] == data_confidence(property)


def test_missing_sqft_leaves_cash_and_foreclosure_computed():
    property = _foreclosure_property(attributes=PropertyAttributes(), rental=RentalBlock(rent_estimate=tv("2000")))
    underwriting = make_underwriting(property.property_id, with_repairs=False)
    assumption_set = assumptions()
    assert flip(property, underwriting, assumption_set, D("150000"), EXP).status == "unavailable"
    assert rental(property, underwriting, assumption_set, D("150000"), EXP).status == "unavailable"
    assert cash(property, underwriting, assumption_set, D("150000"), EXP).status in ("viable", "not_viable")
    assert foreclosure(property, underwriting, assumption_set, D("150000"), EXP).status in ("viable", "not_viable")
