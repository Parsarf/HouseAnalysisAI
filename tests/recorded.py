import hashlib
import json
from pathlib import Path

from contracts.extended import RecordedResponse


ROOT = Path(__file__).parents[1] / "fixtures" / "recorded_responses"


def input_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def load_response(response_id: str) -> RecordedResponse:
    return RecordedResponse.model_validate_json((ROOT / f"{response_id}.json").read_text())
