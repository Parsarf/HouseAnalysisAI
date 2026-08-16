from decimal import Decimal

from contracts import (AssumptionSet, NormalizedProperty, OfferGrid, OfferPoint,
                       Scenario, StrategyResult, StrategyType, UnderwritingResult)


def _unavailable(strategy: StrategyType, scenario: Scenario, reason: str) -> StrategyResult:
    return StrategyResult(strategy=strategy, scenario=scenario, status="unavailable", unavailable_reason=reason)


def cash(record: NormalizedProperty, underwriting: UnderwritingResult, assumptions: AssumptionSet, purchase_price: Decimal, scenario: Scenario) -> StrategyResult:
    value = underwriting.value.arv_by_scenario.get(scenario)
    if value is None:
        return _unavailable(StrategyType.CASH, scenario, "missing_value")
    costs = underwriting.costs.get(scenario)
    if costs is None:
        return _unavailable(StrategyType.CASH, scenario, "missing_costs")
    basis = purchase_price + costs.repairs + costs.holding + costs.acquisition
    resale = value * assumptions.resale.seller_closing_pct
    profit = value - resale - basis
    return StrategyResult(strategy=StrategyType.CASH, scenario=scenario, status="viable" if profit >= 0 else "not_viable",
                          mao=value * (Decimal("1") - assumptions.strategy.cash_target_margin) - costs.repairs - costs.holding - costs.acquisition - resale,
                          all_in_basis=basis, profit=profit, roi=profit / basis if basis else None,
                          margin_of_safety=(value - basis) / value if value else None)


def flip(record: NormalizedProperty, underwriting: UnderwritingResult, assumptions: AssumptionSet, purchase_price: Decimal, scenario: Scenario = Scenario.EXPECTED) -> StrategyResult:
    sqft = record.attributes.sqft.value if record.attributes.sqft and record.attributes.sqft.value is not None else None
    if sqft is None:
        return _unavailable(StrategyType.FLIP, scenario, "no_sqft_data")
    value = underwriting.value.arv_by_scenario.get(scenario)
    costs = underwriting.costs.get(scenario)
    if value is None or costs is None:
        return _unavailable(StrategyType.FLIP, scenario, "missing_value_or_costs")
    financing = purchase_price * assumptions.strategy.hard_money.get("points", Decimal(".02"))
    basis = purchase_price + costs.repairs + costs.holding + costs.acquisition + costs.resale + financing
    profit = value - basis
    margin_target = assumptions.strategy.flip_target_margin_by_arv_band.get("default", Decimal(".2"))
    return StrategyResult(strategy=StrategyType.FLIP, scenario=scenario, status="viable" if profit >= 0 else "not_viable",
                          mao=value * (Decimal("1") - margin_target) - costs.repairs - costs.holding - costs.acquisition - costs.resale - financing,
                          all_in_basis=basis, profit=profit, roi=profit / basis if basis else None,
                          margin_of_safety=profit / value if value else None, metrics={"coc": profit / purchase_price if purchase_price else None})


def wholesale(underwriting: UnderwritingResult, assumptions: AssumptionSet, contract_price: Decimal, scenario: Scenario) -> StrategyResult:
    value = underwriting.value.arv_by_scenario.get(scenario)
    repairs = underwriting.costs.get(scenario).repairs if underwriting.costs.get(scenario) else None
    if value is None or repairs is None:
        return _unavailable(StrategyType.WHOLESALE, scenario, "missing_value_or_repairs")
    threshold = value * assumptions.strategy.wholesale_investor_pct - repairs
    spread = threshold - contract_price
    return StrategyResult(strategy=StrategyType.WHOLESALE, scenario=scenario, status="viable" if spread >= assumptions.strategy.min_assignment_spread and underwriting.confidence >= Decimal(".6") else "not_viable", mao=threshold, profit=spread, metrics={"spread": spread})


def rental(record: NormalizedProperty, underwriting: UnderwritingResult, assumptions: AssumptionSet, price: Decimal, scenario: Scenario) -> StrategyResult:
    if record.rental.rent_estimate is None or record.rental.rent_estimate.value is None:
        return _unavailable(StrategyType.RENTAL, scenario, "no_rent_data")
    value = underwriting.value.arv_by_scenario.get(scenario)
    if value is None:
        return _unavailable(StrategyType.RENTAL, scenario, "missing_value")
    annual_rent = record.rental.rent_estimate.value * Decimal("12")
    vacancy = assumptions.strategy.rental.get("vacancy", Decimal(".06"))
    opex = annual_rent * (vacancy + assumptions.strategy.rental.get("maintenance_pct", Decimal(".08")) + assumptions.strategy.rental.get("management_pct", Decimal(".08")))
    noi = annual_rent * (Decimal("1") - vacancy) - opex
    return StrategyResult(strategy=StrategyType.RENTAL, scenario=scenario, status="viable", metrics={"noi": noi, "cap_rate": noi / price if price else None, "cash_flow": noi, "coc": noi / price if price else None})


def all_strategies(record: NormalizedProperty, underwriting: UnderwritingResult, assumptions: AssumptionSet, price: Decimal) -> list[StrategyResult]:
    results = []
    for scenario in Scenario:
        results.extend([cash(record, underwriting, assumptions, price, scenario), flip(record, underwriting, assumptions, price, scenario), wholesale(underwriting, assumptions, price, scenario), rental(record, underwriting, assumptions, price, scenario)])
        results.append(StrategyResult(strategy=StrategyType.SUBJECT_TO, scenario=scenario, status="requires_human_review", notices=["Legal review required; detection only"]))
        results.append(StrategyResult(strategy=StrategyType.FORECLOSURE, scenario=scenario, status="requires_human_review", notices=["Auction/title review required"]))
    return results


def offer_grid(underwriting: UnderwritingResult, property_id, assumptions: AssumptionSet, center: Decimal) -> OfferGrid:
    value = underwriting.value.v_expected
    if value is None:
        return OfferGrid(property_id=property_id, points=[])
    points = []
    for scenario in Scenario:
        scenario_value = underwriting.value.arv_by_scenario.get(scenario) or value
        for index in range(9):
            offer = (value * (Decimal(".60") + Decimal(index) * Decimal(".05"))).quantize(Decimal("1"))
            closing = offer * assumptions.resale.seller_closing_pct
            confirmed, potential = underwriting.liabilities.confirmed, underwriting.liabilities.potential
            expected = offer - confirmed - closing - potential * Decimal(".5")
            low = offer - confirmed - closing - potential
            high = offer - confirmed - closing
            points.append(OfferPoint(offer_price=offer, scenario=scenario, confirmed_payoffs=confirmed, potential_payoffs=potential, closing_costs=closing,
                                     proceeds_low=low, proceeds_expected=expected, proceeds_high=high, buyer_basis=offer, profit=scenario_value - offer, is_short_sale=low < 0))
    return OfferGrid(property_id=property_id, points=points)
