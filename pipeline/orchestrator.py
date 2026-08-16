from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from contracts import AssumptionSet, NormalizedProperty, ScoreSet, StrategyResult, UnderwritingResult
from finance import underwrite
from scoring import score
from strategies import all_strategies


@dataclass(frozen=True)
class Computation:
    underwriting: UnderwritingResult
    strategies: list[StrategyResult]
    score: ScoreSet


def recompute_property(property: NormalizedProperty, assumptions: AssumptionSet, scoring_config_id: UUID, purchase_price: Decimal) -> Computation:
    underwriting_result = underwrite(property, assumptions)
    strategy_results = all_strategies(property, underwriting_result, assumptions, purchase_price)
    score_result = score(property, underwriting_result, scoring_config_id, strategy_results)
    return Computation(underwriting_result, strategy_results, score_result)
