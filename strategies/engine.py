from decimal import Decimal

from contracts import AssumptionSet, OfferGrid, OfferPoint, Scenario, StrategyResult, StrategyType, UnderwritingResult


def flip(underwriting: UnderwritingResult, assumptions: AssumptionSet, purchase_price: Decimal, scenario: Scenario = Scenario.EXPECTED) -> StrategyResult:
    arv = underwriting.value.arv_by_scenario.get(scenario)
    if arv is None:
        return StrategyResult(strategy=StrategyType.FLIP, scenario=scenario, status="unavailable", unavailable_reason="missing value")
    repairs = (assumptions.repairs.psf_by_condition.get("moderate", Decimal("42")) * Decimal("1000"))
    resale = arv * (assumptions.resale.commission_pct + assumptions.resale.seller_closing_pct)
    basis = purchase_price + repairs + resale + underwriting.liabilities.maximum
    profit = arv - basis
    roi = profit / basis if basis else None
    return StrategyResult(strategy=StrategyType.FLIP, scenario=scenario, status="viable" if profit > 0 else "not_viable",
                          mao=arv - repairs - resale - assumptions.strategy.flip_target_margin_by_arv_band.get("default", Decimal(".2")) * arv,
                          all_in_basis=basis, profit=profit, roi=roi, margin_of_safety=(profit / arv if arv else None),
                          inputs_echo={"arv": arv, "purchase_price": purchase_price, "repairs": repairs, "resale": resale})


def offer_grid(underwriting: UnderwritingResult, property_id, assumptions: AssumptionSet, center: Decimal) -> OfferGrid:
    points: list[OfferPoint] = []
    for scenario, value in underwriting.value.arv_by_scenario.items():
        if value is None:
            continue
        for multiplier in (Decimal(".8"), Decimal(".9"), Decimal("1"), Decimal("1.1"), Decimal("1.2")):
            offer = (center * multiplier).quantize(Decimal("0.01"))
            closing = offer * assumptions.acquisition.closing_pct
            confirmed, potential = underwriting.liabilities.confirmed, underwriting.liabilities.potential
            proceeds_expected = value - offer - confirmed - closing
            points.append(OfferPoint(offer_price=offer, scenario=scenario, confirmed_payoffs=confirmed, potential_payoffs=potential,
                                     closing_costs=closing, proceeds_low=proceeds_expected - potential, proceeds_expected=proceeds_expected,
                                     proceeds_high=proceeds_expected, buyer_basis=offer + closing, profit=proceeds_expected))
    return OfferGrid(property_id=property_id, points=sorted(points, key=lambda point: (point.scenario.value, point.offer_price)))
