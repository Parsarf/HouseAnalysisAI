from .models import (ExtractedFactDraft, FilterClause, FlagRequest, JobPayload,
                      MoneyResponse, NormalizedProperty, TrackedValue)
from .extended import AssumptionSet, OfferGrid, ScoreSet, StrategyResult, UnderwritingResult


def export_schemas() -> dict[str, dict]:
    models = [TrackedValue, ExtractedFactDraft, NormalizedProperty, FlagRequest,
              FilterClause, MoneyResponse, JobPayload, AssumptionSet, UnderwritingResult,
              StrategyResult, OfferGrid, ScoreSet]
    return {model.__name__: model.model_json_schema() for model in models}
