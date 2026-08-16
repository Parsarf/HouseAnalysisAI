from decimal import Decimal
from uuid import UUID

from contracts import NormalizedProperty, ScoreSet, StrategyType, UnderwritingResult


def score(property: NormalizedProperty, underwriting: UnderwritingResult, scoring_config_id: UUID) -> ScoreSet:
    coverage = max(Decimal("0"), min(Decimal("100"), property.data_quality.critical_field_coverage * 100))
    distress = Decimal("60") if property.foreclosure and property.foreclosure.is_active else Decimal("0")
    risk = min(Decimal("100"), Decimal(property.data_quality.material_conflict_count * 15) + (Decimal("20") if underwriting.liabilities.potential else Decimal("0")))
    fos = max(Decimal("0"), min(Decimal("100"), underwriting.confidence * 100))
    overall = max(Decimal("0"), min(Decimal("100"), fos + distress / 2 - risk / 2))
    gates = ["missing_value"] if underwriting.status != "ok" else []
    if property.data_quality.material_conflict_count:
        gates.append("material_conflict")
    return ScoreSet(property_id=property.property_id, scoring_config_id=scoring_config_id, fos=fos, distress=distress,
                    data_confidence=coverage, risk=risk, overall=overall, components={"fos": fos, "distress": distress, "risk": risk},
                    gates_applied=gates, is_rankable=not gates, recommended_strategy=StrategyType.FLIP if not gates else None)
