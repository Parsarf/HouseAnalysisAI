from decimal import Decimal
from statistics import fmean

from common.money import money
from contracts import (AssumptionSet, CostBlock, EquityBlock, LiabilityBlock,
                        NormalizedProperty, Scenario, UnderwritingResult, ValueBlock)

ENGINE_VERSION = "finance-2"
ZERO = Decimal("0")


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def _candidate_values(record: NormalizedProperty) -> list[tuple[str, Decimal, Decimal]]:
    values = []
    for candidate in record.valuation_candidates:
        if candidate.value.value is None:
            continue
        weight = candidate.weight_hint or Decimal("1")
        values.append((candidate.valuation_type, candidate.value.value, weight))
    return values


def _weighted_value(candidates: list[tuple[str, Decimal, Decimal]]) -> tuple[Decimal, Decimal, Decimal]:
    total_weight = sum((item[2] for item in candidates), ZERO)
    expected = sum((value * weight for _, value, weight in candidates), ZERO) / total_weight
    variance = sum((weight * (value - expected) ** 2 for _, value, weight in candidates), ZERO) / total_weight
    dispersion = Decimal("0.15") if len(candidates) == 1 else _clamp(variance.sqrt() / expected if expected else Decimal("0.30"), Decimal("0.04"), Decimal("0.30"))
    return expected, expected * (Decimal("1") - dispersion), expected * (Decimal("1") + dispersion)


def _liabilities(record: NormalizedProperty, assumptions: AssumptionSet) -> LiabilityBlock:
    confirmed = ZERO
    potential = ZERO
    breakdown: list[dict] = []
    for mortgage in record.mortgages:
        if mortgage.estimated_balance and mortgage.estimated_balance.value is not None and mortgage.is_open:
            amount = money(mortgage.estimated_balance.value) or ZERO
            confirmed += amount
            breakdown.append({"label": f"mortgage:{mortgage.position}", "amount": amount, "basis": "recorded", "is_estimated": mortgage.estimated_balance.is_estimated})
    for lien in record.liens:
        if lien.status in ("released", "satisfied"):
            continue
        amount = lien.amount.value if lien.amount and lien.amount.value is not None else assumptions.unknown_lien_medians.get(lien.lien_type, ZERO)
        amount = money(amount) or ZERO
        attached = lien.attachment_basis.value == "recorded_against_property"
        if attached:
            confirmed += amount
        else:
            potential += amount
        breakdown.append({"label": lien.lien_type, "amount": amount, "basis": lien.attachment_basis.value, "is_estimated": lien.amount is None or lien.amount_is_estimated})
    for label, tracked in (("delinquent_taxes", record.taxes.delinquent_amount), ("hoa_arrears", record.hoa.arrears)):
        if tracked and tracked.value is not None:
            confirmed += tracked.value
            breakdown.append({"label": label, "amount": tracked.value, "basis": "recorded", "is_estimated": tracked.is_estimated})
    return LiabilityBlock(confirmed=confirmed, potential=potential, maximum=confirmed + potential, breakdown=breakdown)


def _repairs(record: NormalizedProperty, assumptions: AssumptionSet) -> Decimal | None:
    sqft = record.attributes.sqft.value if record.attributes.sqft and record.attributes.sqft.value is not None else None
    condition = record.condition.condition if record.condition else "moderate"
    rate = assumptions.repairs.psf_by_condition.get(condition)
    return None if sqft is None or rate is None else money(sqft * rate * assumptions.repairs.regional_index)


def underwrite(record: NormalizedProperty, assumptions: AssumptionSet) -> UnderwritingResult:
    candidates = _candidate_values(record)
    if not candidates:
        return UnderwritingResult(property_id=record.property_id, assumption_set_id=assumptions.id, engine_version=ENGINE_VERSION,
                                  status="insufficient_data", unavailable_reason="no valuation candidates", debt_data_present=bool(record.mortgages or record.liens), confidence=ZERO)
    expected, low, high = _weighted_value(candidates)
    liabilities = _liabilities(record, assumptions)
    repair_base = _repairs(record, assumptions)
    repair_base = repair_base if repair_base is not None else ZERO
    confidence = record.data_quality.mean_extraction_confidence
    if len(candidates) == 1:
        confidence = min(confidence, Decimal("0.5"))
    values = {Scenario.CONSERVATIVE: low, Scenario.EXPECTED: expected, Scenario.OPTIMISTIC: high}
    equity = {}
    costs = {}
    resale_pct = assumptions.resale.commission_pct + assumptions.resale.seller_closing_pct + assumptions.resale.concessions_pct
    for scenario, value in values.items():
        potential_factor = {Scenario.CONSERVATIVE: Decimal("1"), Scenario.EXPECTED: Decimal(".5"), Scenario.OPTIMISTIC: ZERO}[scenario]
        adjusted = value - liabilities.confirmed - liabilities.potential * potential_factor
        holding = assumptions.holding.utilities_monthly * Decimal(assumptions.holding.acquisition_months)
        repair_cost = {Scenario.CONSERVATIVE: repair_base * assumptions.repairs.high_multiplier, Scenario.EXPECTED: repair_base, Scenario.OPTIMISTIC: repair_base * assumptions.repairs.low_multiplier}[scenario]
        resale = value * resale_pct
        costs[scenario] = CostBlock(acquisition=assumptions.acquisition.escrow_flat + assumptions.acquisition.inspection_flat,
                                    repairs=money(repair_cost) or ZERO, holding=money(holding) or ZERO, resale=money(resale) or ZERO)
        equity[scenario] = EquityBlock(gross=value - liabilities.confirmed, adjusted=adjusted,
                                       net_realizable=value * (Decimal("1") - resale_pct) - liabilities.confirmed - liabilities.potential * potential_factor - holding,
                                       equity_pct=(value - liabilities.confirmed) / value if value else None)
    return UnderwritingResult(property_id=record.property_id, assumption_set_id=assumptions.id, engine_version=ENGINE_VERSION,
                              status="ok", value=ValueBlock(v_low=low, v_expected=expected, v_high=high, dispersion=(high - low) / expected if expected else None,
                              arv_by_scenario=values, candidates_used=[{"type": kind, "value": value, "weight": weight} for kind, value, weight in candidates], valuation_confidence=confidence),
                              liabilities=liabilities, equity=equity, costs=costs, debt_data_present=bool(record.mortgages or record.liens), confidence=confidence)
