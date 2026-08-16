import hashlib
import json
from dataclasses import dataclass

from contracts import ExtractedFactDraft, RecordedResponse
from .prompts import prompt_version
from .validation import validate_grounding


@dataclass(frozen=True)
class ExtractionResult:
    facts: list[ExtractedFactDraft]
    dropped: int
    prompt_version: str


def request_hash(unit_text: str, unit_type: str) -> str:
    return hashlib.sha256(f"{unit_type}\n{unit_text}".encode()).hexdigest()


def replay_response(payload: RecordedResponse, page_text_by_number: dict[int, str]) -> ExtractionResult:
    facts = []
    dropped = 0
    for raw in payload.response.get("facts", []):
        try:
            fact = ExtractedFactDraft.model_validate(raw)
        except Exception:
            dropped += 1
            continue
        valid, _error = validate_grounding(fact, page_text_by_number.get(fact.page_number, ""))
        if valid is None:
            dropped += 1
            continue
        facts.append(valid)
    return ExtractionResult(facts, dropped, prompt_version())


def serialize_recorded(response_id: str, model: str, input_hash: str, response: dict) -> str:
    return json.dumps(RecordedResponse(response_id=response_id, model=model, prompt_version=prompt_version(), input_hash=input_hash, response=response).model_dump(mode="json"), indent=2)
