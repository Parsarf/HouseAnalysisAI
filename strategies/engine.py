"""WP-7 strategy and offer engine (spec §8/§9). Pure library: no DB, no IO, no floats.

Reconciled against GOLDEN FORMULA SET v2 (fixtures/generate_goldens.py docstring):
- Cash, rental and offer-grid profit read the as-is scenario value
  (v_low/v_expected/v_high); flip and wholesale read the after-repair value
  (arv_by_scenario), which is None when sqft is missing.
- Quantization convention: money 2dp / ratios 6dp / holding months 4dp,
  ROUND_HALF_UP per labelled step; downstream steps consume quantized values.
  All arithmetic runs at decimal precision 40, matching the golden generator.
- Financing (points + flat + accrued interest) and staging are charged inside the
  flip strategy: CostBlock.financing is 0 and CostBlock.resale excludes staging.
- Wholesale gates on Data Confidence >= 60 (0-100 scale). The value is passed in
  (`data_confidence`); `all_strategies` computes it from the record via
  `data_confidence()`. Standalone callers that omit it get the
  underwriting.confidence x 100 fallback.

Deliberate deviations from the golden set, each traced to explicit spec text:
- subject_to `condition_no_acceleration` is evaluated from foreclosure.is_active
  (spec §8 lists "no acceleration" as a detection condition); the golden
  hardcodes 1. fixtures/strategies/04_active_nts_postponements.json updated.
- foreclosure risk flags carry their data source in notices (spec §8: "explicit
  flags, each a boolean with a source"); the golden emits the booleans only.
  fixtures/strategies/04_active_nts_postponements.json updated.
- flip honors flip_target_margin_by_arv_band over_500k/under_500k keys when
  present (spec §8 margin bands); fixture sets carry only "default".
- rental supports optional leverage via rental-assumption keys ltv/rate/
  amort_years (spec §8 debt service, DSCR, leveraged CoC) and an
  owner_utilities_monthly key (spec §8 owner-paid utilities); fixture sets
  parameterize neither, so DSCR is null and cash_flow == NOI there.
"""
import hashlib
import json
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext

from common.mortgage import is_first, position_key
from contracts import (
    AssumptionSet,
    AttachmentBasis,
    CostBlock,
    FlagRequest,
    FlagType,
    NormalizedProperty,
    OfferGrid,
    OfferPoint,
    Scenario,
    StrategyResult,
    StrategyType,
    UnderwritingResult,
)
from finance.transfer_tax import transfer_tax_rate

ZERO = Decimal(0)
ONE = Decimal(1)
Q2 = Decimal("0.01")
Q4 = Decimal("0.0001")
Q6 = Decimal("0.000001")
PRECISION = 40  # matches the golden generator

SCENARIO_ORDER = [Scenario.CONSERVATIVE, Scenario.EXPECTED, Scenario.OPTIMISTIC]
HOLDING_MULT = {Scenario.CONSERVATIVE: Decimal("1.5"), Scenario.EXPECTED: ONE, Scenario.OPTIMISTIC: Decimal("0.75")}
DCS_WHOLESALE_MIN = Decimal(60)
PAYOFF_FEES_DEFAULT = Decimal(1200)  # spec §9.2 payoff interest/fees default
GRID_ROUND = Decimal(5000)
FLIP_BAND_THRESHOLD = Decimal(500000)
CLOSED_STATUSES = frozenset({"closed", "paid", "released", "satisfied"})
FIRST_POSITIONS = frozenset({"first", "1", "1st"})


def _q(value: Decimal | None, quantum: Decimal = Q2) -> Decimal | None:
    return None if value is None else value.quantize(quantum, rounding=ROUND_HALF_UP)


def _tracked(block) -> Decimal | None:
    return block.value if block and block.value is not None else None


def _round_5000(value: Decimal) -> Decimal:
    return (value / GRID_ROUND).quantize(Decimal(1), rounding=ROUND_HALF_UP) * GRID_ROUND


def _v_as_is(underwriting: UnderwritingResult, scenario: Scenario) -> Decimal | None:
    return {Scenario.CONSERVATIVE: underwriting.value.v_low,
            Scenario.EXPECTED: underwriting.value.v_expected,
            Scenario.OPTIMISTIC: underwriting.value.v_high}[scenario]


def _resale_pct(assumptions: AssumptionSet) -> Decimal:
    resale = assumptions.resale
    values = (resale.commission_pct, resale.seller_closing_pct,
              resale.concessions_pct, resale.misc_pct)
    total = sum(values, ZERO)
    if any(value < ZERO for value in values) or total >= ONE:
        raise ValueError("resale percentages must be nonnegative and total less than 100%")
    return total


def _acquisition_pct(assumptions: AssumptionSet) -> Decimal:
    acquisition = assumptions.acquisition
    values = (acquisition.closing_pct, acquisition.title_pct, acquisition.acq_fee_pct,
              transfer_tax_rate(acquisition.transfer_tax_lookup_key))
    total = sum(values, ZERO)
    if any(value < ZERO for value in values) or total >= ONE:
        raise ValueError("acquisition percentages must be nonnegative and total less than 100%")
    return total


def _acquisition_flat(assumptions: AssumptionSet) -> Decimal:
    acquisition = assumptions.acquisition
    values = (acquisition.escrow_flat, acquisition.inspection_flat, acquisition.legal_flat)
    if any(value < ZERO for value in values):
        raise ValueError("flat acquisition costs must be nonnegative")
    return sum(values, ZERO)


def _acquisition_cost(price: Decimal, assumptions: AssumptionSet) -> Decimal:
    return _q(price * _acquisition_pct(assumptions) + _acquisition_flat(assumptions))


def _cash_mao(value: Decimal, cost: CostBlock, assumptions: AssumptionSet) -> Decimal:
    if not ZERO <= assumptions.strategy.cash_target_margin < ONE:
        raise ValueError("cash target margin must be between zero and one")
    available = (value * (ONE - assumptions.strategy.cash_target_margin)
                 - cost.repairs - cost.holding - cost.resale
                 - _acquisition_flat(assumptions))
    return _q(available / (ONE + _acquisition_pct(assumptions)))


def _echo(price: Decimal | None) -> dict[str, Decimal]:
    return {"purchase_price": price} if price else {}


def _unavailable(strategy: StrategyType, scenario: Scenario, reason: str, price: Decimal | None = None) -> StrategyResult:
    return StrategyResult(strategy=strategy, scenario=scenario, status="unavailable",
                          unavailable_reason=reason, inputs_echo=_echo(price))


def _sqft(record: NormalizedProperty) -> Decimal | None:
    return _tracked(record.attributes.sqft)


def _costs(underwriting: UnderwritingResult, scenario: Scenario) -> CostBlock | None:
    return underwriting.costs.get(scenario)


def data_confidence(record: NormalizedProperty, as_of: date | None = None) -> Decimal:
    """Data Confidence Score, 0-100 (spec §10 DCS formula)."""
    quality = record.data_quality
    as_of = as_of or datetime.now(UTC).date()
    corroborated = sum(1 for count in quality.source_counts_by_field.values() if count >= 2)
    corroboration = min(ONE, _q(Decimal(corroborated) / Decimal(22), Q6))
    conflict_penalty = min(ONE, max(ZERO, Decimal(quality.material_conflict_count) / Decimal(5)))
    verification = min(ONE, _q(Decimal(quality.verified_field_count) / Decimal(22), Q6))
    coverage = min(ONE, max(ZERO, quality.critical_field_coverage))
    extraction = min(ONE, max(ZERO, quality.mean_extraction_confidence))
    if quality.newest_report_date is None:
        recency = ZERO
    else:
        age_days = Decimal(max(0, (as_of - quality.newest_report_date).days))
        recency = Decimal("0.5") ** (age_days / Decimal(180))
    return _q(Decimal(100) * (Decimal("0.30") * coverage
                                + Decimal("0.20") * corroboration + Decimal("0.20") * recency
                                + Decimal("0.15") * (ONE - conflict_penalty)
                                + Decimal("0.10") * verification
                                + Decimal("0.05") * extraction), Q4)


def _months_base_from_condition(record: NormalizedProperty, assumptions: AssumptionSet) -> Decimal:
    holding = assumptions.holding
    condition = record.condition.condition if record.condition else "moderate"
    repair_months = holding.repair_months_by_condition.get(
        condition, holding.repair_months_by_condition.get("moderate", Decimal(3)))
    return holding.acquisition_months + repair_months + Decimal(holding.market_days_default) / Decimal(30)


def _months_base_from_costs(underwriting: UnderwritingResult, assumptions: AssumptionSet) -> Decimal:
    """Recover the holding-months base from the CostBlocks alone.

    `offer_grid` receives no record, so the condition is unreadable; months_base =
    acquisition_months + repair_months[condition] + market_days/30 is snapped to the
    repair-months candidate whose predicted scenario holdings reproduce the actual
    (quantized) holding costs. Exact for any cost block produced by WP-6.
    """
    if underwriting.holding_months_base is not None:
        return underwriting.holding_months_base
    holding = assumptions.holding
    market = Decimal(holding.market_days_default) / Decimal(30)
    fallback = holding.acquisition_months + holding.repair_months_by_condition.get("moderate", Decimal(3)) + market
    values = {scenario: _v_as_is(underwriting, scenario) for scenario in SCENARIO_ORDER}
    if any(values[scenario] is None or scenario not in underwriting.costs for scenario in SCENARIO_ORDER):
        return fallback
    rate = (holding.insurance_pct_yr + holding.maintenance_pct_yr) / Decimal(12)
    candidates = {holding.repair_months_by_condition.get("moderate", Decimal(3)),
                  *holding.repair_months_by_condition.values()}
    best = None
    for repair_months in candidates:
        months = holding.acquisition_months + repair_months + market
        months_exp = _q(months * HOLDING_MULT[Scenario.EXPECTED], Q4)
        if not months_exp:
            continue
        # monthly_expected is linear in v: monthly = flat + v*rate. Solve the flat
        # part from the expected scenario, then score against the other two.
        flat_est = underwriting.costs[Scenario.EXPECTED].holding / months_exp - rate * values[Scenario.EXPECTED]
        error = ZERO
        for scenario in (Scenario.CONSERVATIVE, Scenario.OPTIMISTIC):
            predicted = (flat_est + rate * values[scenario]) * _q(months * HOLDING_MULT[scenario], Q4)
            error += abs(underwriting.costs[scenario].holding - predicted)
        if best is None or error < best[0]:
            best = (error, months)
    return best[1] if best else fallback


def _hard_money(assumptions: AssumptionSet) -> tuple[Decimal, Decimal, Decimal]:
    hard_money = assumptions.strategy.hard_money
    rate = hard_money.get("rate", Decimal(".1"))
    points = hard_money.get("points", Decimal(".02"))
    ltv = hard_money.get("ltv", Decimal(".85"))
    if rate < ZERO or points < ZERO or not ZERO <= ltv <= ONE:
        raise ValueError("hard-money rate/points must be nonnegative and LTV must be 0-1")
    return rate, points, ltv


def _flip_margin(bands: dict[str, Decimal], arv: Decimal | None) -> Decimal:
    default = bands.get("default", Decimal(".2"))
    if arv is not None and arv >= FLIP_BAND_THRESHOLD:
        margin = bands.get("over_500k", default)
    else:
        margin = bands.get("under_500k", default)
    if not ZERO <= margin < ONE:
        raise ValueError("flip target margin must be between zero and one")
    return margin


def _flip_mao(arv: Decimal, cost: CostBlock, resale_flip: Decimal, margin_target: Decimal,
              assumptions: AssumptionSet, months: Decimal) -> Decimal:
    """Solve target-margin MAO including price-dependent acquisition/financing."""
    rate, points, ltv = _hard_money(assumptions)
    coeff = ltv * (points + rate / Decimal(12) * months)
    k_const = (arv * (ONE - margin_target) - cost.repairs - cost.holding - resale_flip
               - _acquisition_flat(assumptions) - assumptions.acquisition.financing_flat)
    return _q(
        (k_const - coeff * cost.repairs)
        / (ONE + coeff + _acquisition_pct(assumptions))
    )


def _resale_flip(arv: Decimal, assumptions: AssumptionSet) -> Decimal:
    """Flip carries staging on top of the standard resale % (spec §7.5: staging is flip-only)."""
    return _q(arv * _resale_pct(assumptions) + assumptions.resale.staging_flat)


def cash(record: NormalizedProperty, underwriting: UnderwritingResult, assumptions: AssumptionSet, purchase_price: Decimal, scenario: Scenario) -> StrategyResult:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        value = _v_as_is(underwriting, scenario)
        cost = _costs(underwriting, scenario)
        if value is None or cost is None:
            return _unavailable(StrategyType.CASH, scenario, "no_value_data", purchase_price)
        if purchase_price <= ZERO:
            return _unavailable(StrategyType.CASH, scenario, "invalid_purchase_price", purchase_price)
        acquisition = _acquisition_cost(purchase_price, assumptions)
        all_in = _q(purchase_price + acquisition + cost.repairs + cost.holding)
        profit = _q(value * (ONE - _resale_pct(assumptions)) - all_in)
        mao = _cash_mao(value, cost, assumptions)
        return StrategyResult(strategy=StrategyType.CASH, scenario=scenario,
                              status="viable" if profit >= 0 else "not_viable",
                              mao=mao, all_in_basis=all_in, profit=profit,
                              roi=_q(profit / all_in, Q6) if all_in else None,
                              margin_of_safety=_q((value - all_in) / value, Q6) if value else None,
                              inputs_echo=_echo(purchase_price))


def flip(record: NormalizedProperty, underwriting: UnderwritingResult, assumptions: AssumptionSet, purchase_price: Decimal, scenario: Scenario = Scenario.EXPECTED) -> StrategyResult:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        value = _v_as_is(underwriting, scenario)
        arv = underwriting.value.arv_by_scenario.get(scenario)
        cost = _costs(underwriting, scenario)
        if value is None or cost is None:
            return _unavailable(StrategyType.FLIP, scenario, "no_value_data", purchase_price)
        if purchase_price <= ZERO:
            return _unavailable(StrategyType.FLIP, scenario, "invalid_purchase_price", purchase_price)
        if _sqft(record) is None or arv is None:
            return _unavailable(StrategyType.FLIP, scenario, "no_sqft_data", purchase_price)
        months = _q(_months_base_from_condition(record, assumptions) * HOLDING_MULT[scenario], Q4)
        rate, points, ltv = _hard_money(assumptions)
        loan = _q(ltv * (purchase_price + cost.repairs))
        financing = _q(points * loan + assumptions.acquisition.financing_flat + loan * rate / Decimal(12) * months)
        resale_flip = _resale_flip(arv, assumptions)
        acquisition = _acquisition_cost(purchase_price, assumptions)
        all_in = _q(purchase_price + cost.repairs + cost.holding + financing + acquisition + resale_flip)
        profit = _q(arv - all_in)
        down = _q(purchase_price + cost.repairs - loan)
        coc_denominator = down + cost.holding + acquisition
        margin_target = _flip_margin(assumptions.strategy.flip_target_margin_by_arv_band, arv)
        return StrategyResult(strategy=StrategyType.FLIP, scenario=scenario,
                              status="viable" if profit >= 0 else "not_viable",
                              mao=_flip_mao(arv, cost, resale_flip, margin_target, assumptions, months),
                              all_in_basis=all_in, profit=profit,
                              roi=_q(profit / all_in, Q6) if all_in else None,
                              margin_of_safety=_q((arv - all_in) / arv, Q6) if arv else None,
                              metrics={"coc": _q(profit / coc_denominator, Q6) if coc_denominator else None,
                                       "margin": _q(profit / arv, Q6) if arv else None,
                                       "financing": financing, "loan": loan},
                              inputs_echo=_echo(purchase_price))


def wholesale(underwriting: UnderwritingResult, assumptions: AssumptionSet, contract_price: Decimal, scenario: Scenario, data_confidence: Decimal | None = None) -> StrategyResult:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        arv = underwriting.value.arv_by_scenario.get(scenario)
        cost = _costs(underwriting, scenario)
        if arv is None or cost is None:
            reason = "no_value_data" if _v_as_is(underwriting, scenario) is None else "no_sqft_data"
            return _unavailable(StrategyType.WHOLESALE, scenario, reason, contract_price)
        if contract_price <= ZERO:
            return _unavailable(StrategyType.WHOLESALE, scenario, "invalid_purchase_price", contract_price)
        dcs = data_confidence if data_confidence is not None else _q((underwriting.confidence or ZERO) * Decimal(100), Q4)
        if (not ZERO <= assumptions.strategy.wholesale_investor_pct <= ONE
                or assumptions.strategy.min_assignment_spread < ZERO):
            raise ValueError("wholesale percentage/spread assumptions are invalid")
        threshold = _q(arv * assumptions.strategy.wholesale_investor_pct - cost.repairs)
        max_contract = _q(threshold - assumptions.strategy.min_assignment_spread)
        spread = _q(threshold - contract_price)
        viable = spread >= assumptions.strategy.min_assignment_spread and dcs >= DCS_WHOLESALE_MIN
        return StrategyResult(strategy=StrategyType.WHOLESALE, scenario=scenario,
                              status="viable" if viable else "not_viable",
                              mao=max_contract, profit=spread,
                              metrics={"investor_threshold": threshold, "spread": spread, "dcs": dcs},
                              inputs_echo=_echo(contract_price))


def _annual_debt_service(loan: Decimal, annual_rate: Decimal, amort_years: int) -> Decimal:
    if loan <= 0:
        return ZERO
    if annual_rate < ZERO or amort_years <= 0:
        raise ValueError("rental rate must be nonnegative and amortization term must be positive")
    monthly_rate = annual_rate / Decimal(12)
    payments = amort_years * 12
    if monthly_rate == 0:
        return _q(loan / Decimal(amort_years))
    payment = loan * monthly_rate / (ONE - (ONE + monthly_rate) ** -payments)
    return _q(payment * Decimal(12))


def rental(record: NormalizedProperty, underwriting: UnderwritingResult, assumptions: AssumptionSet, price: Decimal, scenario: Scenario) -> StrategyResult:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        rent = _tracked(record.rental.rent_estimate)
        if rent is None:
            return _unavailable(StrategyType.RENTAL, scenario, "no_rent_data", price)
        value = _v_as_is(underwriting, scenario)
        cost = _costs(underwriting, scenario)
        if price <= ZERO:
            return _unavailable(StrategyType.RENTAL, scenario, "invalid_purchase_price", price)
        if value is None or cost is None:
            return _unavailable(StrategyType.RENTAL, scenario, "no_value_data", price)
        if _sqft(record) is None:
            return _unavailable(StrategyType.RENTAL, scenario, "no_sqft_data", price)
        config = assumptions.strategy.rental
        vacancy = config.get("vacancy", Decimal(".06"))
        operating_rates = (
            config.get("maintenance_pct", Decimal(".08")),
            config.get("management_pct", Decimal(".08")),
            config.get("reserves_pct", Decimal(".05")),
        )
        if (not ZERO <= vacancy < ONE or any(value < ZERO for value in operating_rates)
                or sum(operating_rates, ZERO) >= ONE):
            raise ValueError("rental vacancy and operating percentages are invalid")
        owner_utilities = config.get("owner_utilities_monthly", ZERO)
        if owner_utilities < ZERO:
            raise ValueError("owner-paid utilities must be nonnegative")
        egi = _q(rent * Decimal(12) * (ONE - vacancy))
        opex = _q((_tracked(record.taxes.annual_taxes) or ZERO)
                  + value * assumptions.holding.insurance_pct_yr
                  + (_tracked(record.hoa.monthly_dues) or ZERO) * Decimal(12)
                  + owner_utilities * Decimal(12)
                  + egi * sum(operating_rates, ZERO))
        noi = _q(egi - opex)
        acquisition = _acquisition_cost(price, assumptions)
        invested = price + acquisition + cost.repairs
        cash_flow = noi
        dscr = None
        ltv, rate = config.get("ltv"), config.get("rate")
        if ltv is not None and rate is not None:
            amort_years = int(config.get("amort_years", 30))
            if not ZERO <= ltv <= ONE or rate < ZERO or amort_years <= 0:
                return _unavailable(
                    StrategyType.RENTAL, scenario,
                    "invalid_rental_financing_assumptions", price,
                )
            loan = _q(price * ltv)
            debt_service = _annual_debt_service(loan, rate, amort_years)
            cash_flow = _q(noi - debt_service)
            dscr = _q(noi / debt_service, Q6) if debt_service > 0 else None
            invested = price - loan + acquisition + cost.repairs
        return StrategyResult(strategy=StrategyType.RENTAL, scenario=scenario,
                              status="viable" if cash_flow >= 0 else "not_viable", profit=cash_flow,
                              metrics={"egi": egi, "opex": opex, "noi": noi,
                                       "cap_rate": _q(noi / price, Q6) if price else None,
                                       "cash_flow": cash_flow,
                                       "coc": _q(cash_flow / invested, Q6) if invested else None,
                                       "dscr": dscr},
                              inputs_echo=_echo(price))


def _first_mortgage(record: NormalizedProperty):
    candidates = [
        mortgage for mortgage in record.mortgages
        if mortgage.is_open and is_first(mortgage.position)
    ]
    return max(candidates, key=lambda mortgage: _tracked(mortgage.estimated_balance) or ZERO, default=None)


def _mortgage_position_key(position: str) -> str:
    return position_key(position)


def _distress_present(record: NormalizedProperty) -> bool:
    if record.foreclosure and record.foreclosure.is_active:
        return True
    if any(bankruptcy.status.casefold() == "active" for bankruptcy in record.bankruptcies):
        return True
    return (_tracked(record.taxes.delinquent_amount) or ZERO) > 0


def subject_to(record: NormalizedProperty, underwriting: UnderwritingResult, assumptions: AssumptionSet, price: Decimal, scenario: Scenario) -> StrategyResult:
    """Subject-to / creative: detection only (spec §8). Always requires_human_review;
    the four spec conditions are reported in metrics. rate_vs_market is null because
    the contract carries no market-rate input."""
    value = _v_as_is(underwriting, scenario)
    first = _first_mortgage(record)
    balance = _tracked(first.estimated_balance) if first else None
    if balance is None or not value:
        balance_condition = None
    else:
        balance_condition = Decimal(1) if balance <= value * Decimal("0.8") else Decimal(0)
    return StrategyResult(strategy=StrategyType.SUBJECT_TO, scenario=scenario, status="requires_human_review",
                          notices=["detection only - due-on-sale / legal review required"],
                          metrics={"condition_rate_200bps_below_market": None,
                                   "condition_balance_le_80pct_value": balance_condition,
                                   "condition_no_acceleration": Decimal(0) if record.foreclosure and record.foreclosure.is_active else Decimal(1),
                                   "condition_distress_present": Decimal(1) if _distress_present(record) else Decimal(0)},
                          inputs_echo=_echo(price))


def foreclosure(record: NormalizedProperty, underwriting: UnderwritingResult, assumptions: AssumptionSet, price: Decimal, scenario: Scenario) -> StrategyResult:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        state = record.foreclosure
        if not (state and state.is_active):
            return _unavailable(StrategyType.FORECLOSURE, scenario, "no_active_foreclosure", price)
        v_low = underwriting.value.v_low
        if v_low is None:
            return _unavailable(StrategyType.FORECLOSURE, scenario, "no_value_data", price)
        bid = _tracked(state.published_bid)
        first = _first_mortgage(record)
        obligations = bid if bid is not None else ((_tracked(first.estimated_balance) if first else ZERO) or ZERO)
        # Reports often repeat the same mortgage with position aliases (for
        # example "second" and "2"). Mirror finance's conservative conflict
        # handling: keep the largest balance at each junior priority instead of
        # double-counting duplicates as separate surviving debt.
        junior_by_position: dict[str, Decimal] = {}
        for mortgage in record.mortgages:
            position = _mortgage_position_key(mortgage.position)
            if not mortgage.is_open or position == "1":
                continue
            balance = _tracked(mortgage.estimated_balance)
            if balance is not None:
                junior_by_position[position] = max(junior_by_position.get(position, ZERO), balance)
        junior_debt = sum(junior_by_position.values(), ZERO)
        junior_debt += sum((_tracked(lien.amount) or ZERO for lien in record.liens
                            if lien.status.casefold() not in CLOSED_STATUSES
                            and lien.attachment_basis == AttachmentBasis.RECORDED_AGAINST_PROPERTY), ZERO)
        transfer_costs = _acquisition_cost(obligations, assumptions) if obligations > ZERO else ZERO
        total_obligations = _q(obligations + junior_debt
                               + (_tracked(record.taxes.delinquent_amount) or ZERO)
                               + transfer_costs)
        holding = assumptions.holding
        monthly = _q((_tracked(record.taxes.annual_taxes) or ZERO) / Decimal(12)
                     + v_low * (holding.insurance_pct_yr + holding.maintenance_pct_yr) / Decimal(12)
                     + holding.utilities_monthly + (_tracked(record.hoa.monthly_dues) or ZERO))
        auction_holding = _q(monthly * Decimal(2))
        interior_unknown = record.condition is None
        repair_scenario = Scenario.CONSERVATIVE if interior_unknown else scenario
        cost = underwriting.costs.get(repair_scenario) or CostBlock()
        repairs = cost.repairs
        spread = _q(v_low - total_obligations - repairs - auction_holding)
        active_liens = [lien for lien in record.liens if lien.status.casefold() not in CLOSED_STATUSES]
        flags = {
            "flag_junior_liens_present": (junior_debt > 0, "mortgages/liens"),
            "flag_irs_lien": (any(lien.lien_type == "federal_tax" for lien in active_liens), "liens"),
            "flag_hoa_super_priority": (bool(record.hoa.has_lien), "hoa"),
            "flag_owner_occupied": (bool(record.ownership.is_owner_occupied), "ownership"),
            "flag_interior_unknown": (interior_unknown, "condition"),
            "flag_postponements_ge_3": (state.postponement_count >= 3, "foreclosure"),
        }
        return StrategyResult(strategy=StrategyType.FORECLOSURE, scenario=scenario,
                              status="viable" if spread > 0 else "not_viable", profit=spread,
                              metrics={"total_obligations": total_obligations,
                                       "transfer_costs": transfer_costs,
                                       "auction_holding": auction_holding,
                                       "spread": spread,
                                       **{name: Decimal(1) if fired else Decimal(0) for name, (fired, _) in flags.items()}},
                              notices=[f"{name} (source: {source})" for name, (fired, source) in flags.items() if fired],
                              inputs_echo=_echo(price))


def all_strategies(
    record: NormalizedProperty, underwriting: UnderwritingResult,
    assumptions: AssumptionSet, price: Decimal, as_of: date | None = None,
) -> list[StrategyResult]:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        dcs = data_confidence(record, as_of=as_of)
        results = []
        for scenario in SCENARIO_ORDER:
            results.extend([cash(record, underwriting, assumptions, price, scenario),
                            flip(record, underwriting, assumptions, price, scenario),
                            wholesale(underwriting, assumptions, price, scenario, data_confidence=dcs),
                            rental(record, underwriting, assumptions, price, scenario),
                            subject_to(record, underwriting, assumptions, price, scenario),
                            foreclosure(record, underwriting, assumptions, price, scenario)])
        return results


def _payoff_fees(underwriting: UnderwritingResult) -> Decimal:
    """$1,200 payoff interest/fees when any open mortgage exists (spec §9.2).

    Inferred from the liabilities breakdown (offer_grid receives no record): WP-6
    emits one `mortgage:*` entry per open mortgage with a computable balance.
    """
    has_mortgage = any(item.get("label", "").startswith("mortgage:") for item in underwriting.liabilities.breakdown)
    return PAYOFF_FEES_DEFAULT if has_mortgage else ZERO


def _weighted_potential(underwriting: UnderwritingResult, assumptions: AssumptionSet) -> Decimal:
    """Σ potential_i × attachment_probability[basis_i] over potential liens, per-term quantized."""
    weighted = ZERO
    for item in underwriting.liabilities.breakdown:
        if item.get("expected_amount") is not None:
            weighted += _q(item["expected_amount"])
            continue
        basis = item.get("basis")
        if basis == "undrawn_heloc_capacity":
            weighted += _q(item.get("amount", ZERO) * assumptions.attachment_probability.get(basis, Decimal("0.50")))
            continue
        if basis not in (AttachmentBasis.OWNER_NAMED_ONLY.value, AttachmentBasis.UNKNOWN.value):
            continue
        probability = min(ONE, max(ZERO, assumptions.attachment_probability.get(AttachmentBasis(basis), ONE)))
        weighted += _q(item.get("amount", ZERO) * probability)
    return weighted


def offer_point(underwriting: UnderwritingResult, assumptions: AssumptionSet, offer: Decimal, scenario: Scenario, label: str | None = None) -> OfferPoint:
    """Authoritative per-offer math (spec §9.2). Every output is linear in the offer
    price, so slider interpolation between grid points is exact (spec §9.3)."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        if offer <= ZERO:
            raise ValueError("offer must be greater than zero")
        if underwriting.liabilities.confirmed is None or underwriting.liabilities.potential is None:
            raise ValueError("liability data is required to calculate seller proceeds")
        cost = _costs(underwriting, scenario) or CostBlock()
        value = _v_as_is(underwriting, scenario)
        confirmed_payoffs = _q(underwriting.liabilities.confirmed + _payoff_fees(underwriting))
        potential_payoffs = underwriting.liabilities.potential
        closing = _q(offer * (
            assumptions.acquisition.title_pct
            + transfer_tax_rate(assumptions.acquisition.transfer_tax_lookup_key)
        ) + assumptions.acquisition.escrow_flat)
        proceeds_high = _q(offer - confirmed_payoffs - closing)
        proceeds_expected = _q(proceeds_high - _weighted_potential(underwriting, assumptions))
        proceeds_low = _q(proceeds_high - potential_payoffs)
        buyer_basis = _q(offer + _acquisition_cost(offer, assumptions) + cost.repairs + cost.holding)
        profit = _q(value * (ONE - _resale_pct(assumptions)) - buyer_basis) if value is not None else None
        return OfferPoint(offer_price=offer, scenario=scenario, confirmed_payoffs=confirmed_payoffs,
                          potential_payoffs=potential_payoffs, closing_costs=closing,
                          proceeds_low=proceeds_low, proceeds_expected=proceeds_expected,
                          proceeds_high=proceeds_high, buyer_basis=buyer_basis,
                          profit=profit if profit is not None else ZERO,
                          roi=_q(profit / buyer_basis, Q6) if buyer_basis and profit is not None else None,
                          is_short_sale=proceeds_low < 0, label=label)


def offer_grid(underwriting: UnderwritingResult, property_id, assumptions: AssumptionSet, center: Decimal) -> OfferGrid:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        v_exp = underwriting.value.v_expected
        if (v_exp is None or underwriting.status != "ok"
                or underwriting.liabilities.confirmed is None
                or underwriting.liabilities.potential is None):
            return OfferGrid(property_id=property_id, points=[])
        offers = [_round_5000(v_exp * (Decimal("0.60") + Decimal(index) * Decimal("0.05"))) for index in range(9)]
        months_base = _months_base_from_costs(underwriting, assumptions)
        points = []
        for scenario in SCENARIO_ORDER:
            arv = underwriting.value.arv_by_scenario.get(scenario)
            cost = _costs(underwriting, scenario) or CostBlock()
            entries: list[tuple[Decimal, str | None]] = [(offer, None) for offer in offers]
            mao_cash = _cash_mao(_v_as_is(underwriting, scenario), cost, assumptions)
            markers: list[tuple[str, Decimal | None]] = [("mao_cash", mao_cash), ("mao_flip", None)]
            if arv is not None:
                months = _q(months_base * HOLDING_MULT[scenario], Q4)
                resale_flip = _resale_flip(arv, assumptions)
                margin_target = _flip_margin(assumptions.strategy.flip_target_margin_by_arv_band, arv)
                markers[1] = ("mao_flip", _flip_mao(arv, cost, resale_flip, margin_target, assumptions, months))
            entries.extend((marker, label) for label, marker in markers if marker is not None and marker > ZERO)
            for offer, label in sorted(entries, key=lambda entry: entry[0]):
                points.append(offer_point(underwriting, assumptions, offer, scenario, label=label))
        return OfferGrid(property_id=property_id, points=points, interpolatable=True)


def short_sale_flag_requests(grid: OfferGrid) -> list[FlagRequest]:
    affected = [point for point in grid.points if point.is_short_sale]
    if not affected:
        return []
    prices = [point.offer_price for point in affected]
    proceeds = [point.proceeds_low for point in affected]
    scenarios = sorted({point.scenario.value for point in affected})
    payload = {
        "affected_offer_points": len(affected),
        "affected_scenarios": len(affected),
        "underwriting_scenario_count": len(scenarios),
        "scenarios": scenarios,
        "offer_price_min": str(min(prices)),
        "offer_price_max": str(max(prices)),
        "proceeds_low_min": str(min(proceeds)),
        "proceeds_low_max": str(max(proceeds)),
        "reason": "Seller proceeds are insufficient to satisfy estimated obligations within part of the analyzed offer range.",
        "review_guidance": "Confirm payoff amounts and determine whether lender approval for a short sale is required.",
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return [FlagRequest(
        property_id=grid.property_id,
        flag_type=FlagType.SHORT_SALE_CANDIDATE,
        payload=payload,
        financial_impact_usd=abs(min(proceeds)),
        raised_by="strategies",
        dedupe_key=f"{grid.property_id}:short_sale_candidate",
        logical_key="short_sale_candidate",
        fingerprint=fingerprint,
    )]
