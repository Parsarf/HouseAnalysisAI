from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from contracts import AssumptionSet, FullNormalizedProperty, ScoreSet, StrategyResult, UnderwritingResult
from finance import underwrite
from scoring import score
from strategies import flip


@dataclass(frozen=True)
class Computation:
    underwriting: UnderwritingResult
    strategies: list[StrategyResult]
    score: ScoreSet


def recompute_property(property: FullNormalizedProperty, assumptions: AssumptionSet, scoring_config_id: UUID, purchase_price: Decimal) -> Computation:
    underwriting_result = underwrite(property, assumptions)
    strategy_results = [flip(underwriting_result, assumptions, purchase_price)]
    score_result = score(property, underwriting_result, scoring_config_id)
    return Computation(underwriting_result, strategy_results, score_result)
