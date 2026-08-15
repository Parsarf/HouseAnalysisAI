from .models import (ExtractedFactDraft, FilterClause, FlagRequest, JobPayload,
                      MoneyResponse, NormalizedProperty, TrackedValue)


def export_schemas() -> dict[str, dict]:
    models = [TrackedValue, ExtractedFactDraft, NormalizedProperty, FlagRequest,
              FilterClause, MoneyResponse, JobPayload]
    return {model.__name__: model.model_json_schema() for model in models}
