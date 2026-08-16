from decimal import Decimal
from uuid import UUID

from contracts import NormalizedProperty, ScoreSet, StrategyResult, StrategyType, UnderwritingResult


ZERO = Decimal("0")


def n(value: Decimal | None, low: Decimal, high: Decimal) -> Decimal:
    if value is None or high == low:
        return ZERO
    return max(ZERO, min(Decimal("1"), (value - low) / (high - low)))


def _best_strategy(strategies: list[StrategyResult]) -> StrategyResult | None:
    viable = [item for item in strategies if item.status == "viable" and item.strategy in (StrategyType.CASH, StrategyType.FLIP)]
    return max(viable, key=lambda item: item.profit or ZERO) if viable else None


def _distress(record: NormalizedProperty) -> Decimal:
    points = ZERO
    if record.foreclosure and record.foreclosure.is_active:
        points += Decimal("30") if record.foreclosure.current_sale_date else Decimal("18")
        points += min(Decimal("16"), Decimal(record.foreclosure.postponement_count) * Decimal("8"))
    points += Decimal("12") * Decimal(len(record.bankruptcies))
    for lien in record.liens:
        if lien.status == "active":
            points += Decimal("10") if lien.attachment_basis.value == "recorded_against_property" and lien.lien_type == "property_tax" else Decimal("3")
    if record.taxes.delinquent_years and record.taxes.delinquent_years >= 2:
        points += Decimal("10")
    if record.ownership.is_absentee:
        points += Decimal("5")
    if record.ownership.years_owned and record.ownership.years_owned > Decimal("15"):
        points += Decimal("4")
    return min(Decimal("100"), points)


def _dcs(record: NormalizedProperty) -> Decimal:
    quality = record.data_quality
    conflict_penalty = min(Decimal("1"), Decimal(quality.material_conflict_count) / Decimal("5"))
    return Decimal("100") * (Decimal(".30") * quality.critical_field_coverage + Decimal(".20") * min(Decimal("1"), quality.verified_field_count / Decimal("22")) + Decimal(".15") * (Decimal("1") - conflict_penalty) + Decimal(".10") * min(Decimal("1"), quality.verified_field_count / Decimal("22")) + Decimal(".25") * quality.mean_extraction_confidence)


def score(record: NormalizedProperty, underwriting: UnderwritingResult, scoring_config_id: UUID, strategies: list[StrategyResult] | None = None) -> ScoreSet:
    strategies = strategies or []
    best = _best_strategy(strategies)
    expected_value = underwriting.value.v_expected
    profit = best.profit if best else None
    roi = best.roi if best else None
    equity_pct = underwriting.equity.get(next(iter(underwriting.equity), None)).equity_pct if underwriting.equity else None
    discount = (expected_value - (best.mao or expected_value)) / expected_value if expected_value and best else ZERO
    margin = best.margin_of_safety if best else ZERO
    fos = Decimal("100") * (Decimal(".30") * n(profit, ZERO, Decimal("150000")) + Decimal(".25") * n(roi, ZERO, Decimal(".50")) + Decimal(".20") * n(equity_pct, ZERO, Decimal(".60")) + Decimal(".15") * n(discount, ZERO, Decimal(".35")) + Decimal(".10") * n(margin, ZERO, Decimal(".35")))
    distress = _distress(record)
    dcs = _dcs(record)
    risk = min(Decimal("100"), Decimal(len(record.liens) * 6) + Decimal("15" if record.bankruptcies else "0") + Decimal("12" if record.foreclosure and record.foreclosure.is_active else "0") + Decimal("10" if any(item.attachment_basis.value == "owner_named_only" and item.amount and item.amount.value and item.amount.value > Decimal("10000") for item in record.liens) else "0") + Decimal("8" if record.hoa.arrears and record.hoa.arrears.value else "0") + Decimal(record.data_quality.material_conflict_count * 10) + Decimal("12" if dcs < Decimal("50") else "0"))
    overall = max(ZERO, min(Decimal("100"), Decimal(".50") * fos + Decimal(".20") * distress + Decimal(".20") * dcs - Decimal(".25") * risk))
    gates = []
    if underwriting.status != "ok":
        gates.append("insufficient_data")
    if dcs < Decimal("40"):
        gates.append("needs_review")
        overall = min(overall, Decimal("45"))
    if any(item.is_gating for item in record.open_flags):
        gates.append("open_gating_flag")
    return ScoreSet(property_id=record.property_id, scoring_config_id=scoring_config_id, fos=fos, distress=distress, data_confidence=dcs, risk=risk, overall=overall,
                    components={"profit": profit or ZERO, "roi": roi or ZERO, "equity_pct": equity_pct or ZERO, "discount_to_value": discount, "margin_of_safety": margin},
                    gates_applied=gates, is_rankable=underwriting.status == "ok" and "open_gating_flag" not in gates and "needs_review" not in gates,
                    recommended_strategy=best.strategy if best else None)
