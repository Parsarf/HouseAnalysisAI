from .models import (
    AssumptionSet,
    ExtractedFactDraft,
    FilterClause,
    FlagRequest,
    JobPayload,
    MoneyResponse,
    NormalizedProperty,
    OfferGrid,
    ScoreSet,
    StrategyResult,
    TrackedValue,
    UnderwritingResult,
)


def export_schemas() -> dict[str, dict]:
    models = [TrackedValue, ExtractedFactDraft, NormalizedProperty, FlagRequest,
              FilterClause, MoneyResponse, JobPayload, AssumptionSet, UnderwritingResult,
              StrategyResult, OfferGrid, ScoreSet]
    return {model.__name__: model.model_json_schema() for model in models}
