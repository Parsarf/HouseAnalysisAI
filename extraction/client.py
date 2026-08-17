"""Provider client, cost accounting, and recorded-response replay.

Live calls go to an OpenAI-compatible chat-completions endpoint, configured by
environment (``ACQ_EXTRACTION_API_KEY``, ``ACQ_EXTRACTION_BASE_URL``,
``ACQ_EXTRACTION_CHEAP_MODEL``, ``ACQ_EXTRACTION_FRONTIER_MODEL``). Tests never
touch the network: they inject a fake ``transport`` or replay recorded
responses from ``fixtures/recorded_responses/``.
"""

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from common.errors import AcqError, ErrorCode
from contracts import ExtractedFactDraft, RecordedResponse

from .flatten import flatten_payload
from .prompts import prompt_version
from .schemas import route_model, schema_for, top_level_key
from .validation import run_gauntlet

ENV_API_KEY = "ACQ_EXTRACTION_API_KEY"
ENV_BASE_URL = "ACQ_EXTRACTION_BASE_URL"
ENV_CHEAP_MODEL = "ACQ_EXTRACTION_CHEAP_MODEL"
ENV_FRONTIER_MODEL = "ACQ_EXTRACTION_FRONTIER_MODEL"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_CHEAP_MODEL = "gpt-4o-mini"
DEFAULT_FRONTIER_MODEL = "gpt-4o"

# USD per 1M tokens: (input, output). Unknown models fall back to frontier pricing.
MODEL_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
}
_FALLBACK_PRICING = (Decimal("2.50"), Decimal("10.00"))

# These reasoning-model families reject custom sampling temperatures when used
# with this client's default Chat Completions configuration. Omitting the field
# lets the provider apply its supported default.
_DEFAULT_TEMPERATURE_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


@dataclass(frozen=True)
class ExtractionResult:
    facts: list[ExtractedFactDraft]
    dropped: int
    prompt_version: str
    inactive: list[ExtractedFactDraft] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    model: str | None = None
    cost_usd: Decimal | None = None
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResponse:
    payload: dict
    model: str
    usage: dict[str, int]
    cost_usd: Decimal
    attempts: int


def request_hash(unit_text: str, unit_type: str) -> str:
    return hashlib.sha256(f"{unit_type}\n{unit_text}".encode()).hexdigest()


def compute_cost(model: str, usage: dict[str, int]) -> Decimal:
    input_price, output_price = MODEL_PRICING.get(model, _FALLBACK_PRICING)
    prompt_tokens = Decimal(usage.get("prompt_tokens", 0))
    completion_tokens = Decimal(usage.get("completion_tokens", 0))
    return ((prompt_tokens * input_price + completion_tokens * output_price) / Decimal(1_000_000)).quantize(
        Decimal("0.000001")
    )


# transport(method, url, headers, body, timeout) -> (status, response_json)
Transport = Callable[[str, str, dict[str, str], bytes, float], tuple[int, dict]]


def _supports_temperature_zero(model: str) -> bool:
    """Return whether this model should receive the deterministic temperature."""
    model_name = model.rsplit("/", 1)[-1].lower()
    return not model_name.startswith(_DEFAULT_TEMPERATURE_MODEL_PREFIXES)


def _is_temperature_compatibility_error(status: int, response: dict) -> bool:
    """Recognize the provider error used by aliases that require the default."""
    if status != 400:
        return False
    message = str((response.get("error") or {}).get("message", "")).lower()
    return "temperature" in message and (
        "only the default" in message
        or "does not support" in message
        or "unsupported value" in message
    )


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode())
        except Exception:
            payload = {"error": {"message": error.reason}}
        return error.code, payload


class ProviderClient:
    """OpenAI-compatible structured-output client with retry/backoff (spec §5.2, §5.5)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        cheap_model: str | None = None,
        frontier_model: str | None = None,
        max_retries: int = 5,
        base_delay: float = 0.5,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        transport: Transport | None = None,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get(ENV_API_KEY)
        self.base_url = (base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        self.cheap_model = cheap_model or os.environ.get(ENV_CHEAP_MODEL) or DEFAULT_CHEAP_MODEL
        self.frontier_model = frontier_model or os.environ.get(ENV_FRONTIER_MODEL) or DEFAULT_FRONTIER_MODEL
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.timeout = timeout
        self.sleep = sleep
        self.transport = transport or _urllib_transport

    def model_for(self, unit_type: str) -> str:
        return route_model(unit_type, cheap_model=self.cheap_model, frontier_model=self.frontier_model)

    def complete(
        self,
        unit_type: str,
        unit_text: str,
        *,
        subject: str | None = None,
        system_prompt: str,
    ) -> ProviderResponse:
        if not self.api_key:
            raise AcqError(ErrorCode.EXTRACTION_FAILED, "extraction API key is not configured")
        model = self.model_for(unit_type)
        schema = schema_for(unit_type)
        key = top_level_key(unit_type)
        user = f"{subject}\n\n{unit_text}" if subject else unit_text
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}]
        body, attempts, usage = self._call_with_retries(model, schema, unit_type, messages)
        # One repair retry on schema-shape failure only (spec §5.3 step 1).
        if not isinstance(body.get(key), list):
            messages = messages + [
                {"role": "assistant", "content": json.dumps(body)},
                {"role": "user", "content": f"Response must be a JSON object with a top-level array named \"{key}\"."},
            ]
            body, repair_attempts, repair_usage = self._call_with_retries(model, schema, unit_type, messages)
            attempts += repair_attempts
            usage = {k: usage.get(k, 0) + repair_usage.get(k, 0) for k in set(usage) | set(repair_usage)}
            if not isinstance(body.get(key), list):
                raise AcqError(ErrorCode.INVALID_INPUT, f"provider response failed schema validation for unit type {unit_type!r}")
        return ProviderResponse(body, model, usage, compute_cost(model, usage), attempts)

    def _call_with_retries(self, model: str, schema: dict, unit_type: str, messages: list[dict]) -> tuple[dict, int, dict[str, int]]:
        payload = {
            "model": model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": f"{unit_type}_extraction", "schema": schema, "strict": True},
            },
        }
        if _supports_temperature_zero(model):
            payload["temperature"] = 0
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/chat/completions"
        attempts = 0
        retried_without_temperature = False
        while True:
            attempts += 1
            body = json.dumps(payload).encode()
            try:
                status, response = self.transport("POST", url, headers, body, self.timeout)
            except Exception:
                status, response = 0, {}
            if status == 200:
                break
            if (
                "temperature" in payload
                and not retried_without_temperature
                and _is_temperature_compatibility_error(status, response)
            ):
                payload.pop("temperature")
                retried_without_temperature = True
                continue
            retryable = status == 429 or status >= 500 or status == 0
            if not retryable:
                message = (response.get("error") or {}).get("message", f"HTTP {status}")
                raise AcqError(ErrorCode.EXTRACTION_FAILED, f"provider rejected the request: {message}")
            if attempts > self.max_retries:
                raise AcqError(ErrorCode.RETRY_EXHAUSTED, f"provider still failing after {attempts - 1} retries")
            self.sleep(self.base_delay * (2 ** (attempts - 1)))
        content = (response.get("choices") or [{}])[0].get("message", {}).get("content")
        try:
            parsed = json.loads(content) if isinstance(content, str) else content
        except json.JSONDecodeError:
            parsed = None
        usage = dict(response.get("usage") or {})
        return (parsed if isinstance(parsed, dict) else {}), attempts, usage


def replay_response(payload: RecordedResponse, page_text_by_number: dict[int, str], *, unit_type: str = "liens") -> ExtractionResult:
    """Replay a recorded provider response offline. Supports both the legacy
    flat ``{"facts": [...]}`` shape and schema-shaped payloads (``{"liens": [...]}``)."""
    counters: dict[str, int] = {}
    dropped = 0
    drafts: list[ExtractedFactDraft] = []
    raw_facts = payload.response.get("facts")
    if raw_facts is not None:
        for raw in raw_facts:
            try:
                drafts.append(ExtractedFactDraft.model_validate(raw))
            except Exception:
                dropped += 1
    else:
        zero = UUID(int=0)
        drafts, dropped = flatten_payload(
            unit_type, payload.response, report_id=zero, extraction_unit_id=zero
        )
    outcome = run_gauntlet(drafts, page_text_by_number, dropped=dropped, counters=counters)
    return ExtractionResult(
        outcome.active, outcome.dropped, prompt_version(),
        inactive=outcome.inactive, counters=outcome.counters, model=payload.model,
    )


def serialize_recorded(response_id: str, model: str, input_hash: str, response: dict) -> str:
    return json.dumps(RecordedResponse(response_id=response_id, model=model, prompt_version=prompt_version(), input_hash=input_hash, response=response).model_dump(mode="json"), indent=2)
