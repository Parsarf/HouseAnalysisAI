import re

from common.errors import ErrorCode
from contracts import ExtractedFactDraft


def grounded(fact: ExtractedFactDraft, page_text: str) -> bool:
    normalized = lambda value: re.sub(r"\s+", " ", value.casefold()).strip()
    return normalized(fact.snippet) in normalized(page_text)


def validate_grounding(fact: ExtractedFactDraft, page_text: str) -> tuple[ExtractedFactDraft | None, str | None]:
    if not grounded(fact, page_text):
        return None, ErrorCode.GROUNDING_FAILED.value
    if fact.value_parsed is None and fact.value_raw is not None and fact.null_reason is None:
        return None, ErrorCode.INVALID_INPUT.value
    return fact, None
