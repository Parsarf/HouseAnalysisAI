from decimal import Decimal
from uuid import UUID

from common.money import money
from contracts import (AssumptionSet, EquityBlock, NormalizedProperty, LiabilityBlock,
                        Scenario, UnderwritingResult, ValueBlock)


ENGINE_VERSION = "finance-1"


def _candidate_values(property: NormalizedProperty) -> list[Decimal]:
    return [item.value.value for item in property.valuation_candidates if item.value.value is not None]


def underwrite(property: NormalizedProperty, assumptions: AssumptionSet) -> UnderwritingResult:
    values = sorted(_candidate_values(property))
    if not values:
        return UnderwritingResult(property_id=property.property_id, assumption_set_id=assumptions.id,
                                  engine_version=ENGINE_VERSION, status="insufficient_data",
                                  unavailable_reason="no valuation candidates", value=ValueBlock(),
                                  liabilities=LiabilityBlock(), debt_data_present=bool(property.mortgages), confidence=Decimal("0"))
    low, high = values[0], values[-1]
    expected = values[len(values) // 2]
    confirmed = Decimal("0")
    potential = Decimal("0")
    breakdown: list[dict[str, object]] = []
    for lien in property.liens:
        if not lien.amount or lien.amount.value is None or lien.status in ("released", "satisfied"):
            continue
        amount = money(lien.amount.value) or Decimal("0")
        bucket = "confirmed" if lien.attachment_basis.value == "recorded_against_property" else "potential"
        if bucket == "confirmed":
            confirmed += amount
        else:
            potential += amount
        breakdown.append({"label": lien.lien_type, "amount": amount, "basis": lien.attachment_basis.value, "is_estimated": lien.amount_is_estimated})
    mortgage_total = sum((m.estimated_balance.value for m in property.mortgages if m.estimated_balance and m.estimated_balance.value is not None), Decimal("0"))
    confirmed += mortgage_total
    max_liability = confirmed + potential
    value = ValueBlock(v_low=low, v_expected=expected, v_high=high, dispersion=(high - low) / expected if expected else None,
                       arv_by_scenario={Scenario.CONSERVATIVE: low, Scenario.EXPECTED: expected, Scenario.OPTIMISTIC: high},
                       valuation_confidence=property.data_quality.mean_extraction_confidence)
    equity = {scenario: EquityBlock(gross=amount - confirmed, adjusted=amount - max_liability, net_realizable=amount - max_liability)
              for scenario, amount in ((Scenario.CONSERVATIVE, low), (Scenario.EXPECTED, expected), (Scenario.OPTIMISTIC, high))}
    return UnderwritingResult(property_id=property.property_id, assumption_set_id=assumptions.id, engine_version=ENGINE_VERSION,
                              status="ok", value=value, liabilities=LiabilityBlock(confirmed=confirmed, potential=potential, maximum=max_liability, breakdown=breakdown),
                              equity=equity, debt_data_present=bool(property.mortgages or property.liens),
                              confidence=property.data_quality.mean_extraction_confidence)
