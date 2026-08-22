"""OpenAI Responses client for one original PDF and one canonical JSON result."""

from __future__ import annotations

import base64
import json
import logging
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from common.settings import settings
from extraction.client import MODEL_PRICING

from .classification import DocumentKind
from .schemas import canonical_schema, owner_schema

log = logging.getLogger(__name__)

PROMPT_VERSION = "whole-pdf-v1"

SYSTEM_PROMPT = """Analyze the attached original property report as one document. Use page text,
visual layout, labels, tables, and relationships across the entire PDF. Return only the canonical
property JSON required by the schema.

Evidence rules:
- Extract only information grounded in the PDF. Never fabricate or estimate a source fact.
- Return null for a scalar when the document does not provide enough evidence.
- Return [] when an array has no supported entries.
- Preserve APNs as text, money as numeric values, and dates as source dates where possible.
- Put useful grounded information that does not map to a canonical field in additional_facts.
- Do not duplicate a fact in additional_facts when it maps to a canonical field.
- Add concise source_references for important identity, value, debt, lien, foreclosure, ownership,
  tax, and transaction fields when a page can be identified.
- Do not calculate equity, LTV, offer prices, profit, ROI, scores, or rankings. Python calculates
  derived financial outputs after extraction.
"""

OWNER_SYSTEM_PROMPT = """Analyze the attached owner or skip-trace profile as one document.
Return only the owner-profile JSON required by the schema. Extract every phone and email as a
separate candidate with its source and confidence. Extract person liens and bankruptcy cases.
Never infer that a lien attaches to a property, and never invent a property address or APN.
"""


class ProviderError(RuntimeError):
    """A retryable provider or transport failure exhausted its local retries."""


class ProviderTimeout(ProviderError):
    """The provider did not answer within the configured bounded timeout."""


class PermanentProviderError(RuntimeError):
    """A 4xx request rejection that retrying unchanged cannot repair."""


@dataclass(frozen=True)
class ProviderAnalysis:
    payload: dict
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    duration_ms: int
    attempts: int


Transport = Callable[[str, str, dict[str, str], bytes, float], tuple[int, dict]]


def _urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes, timeout: float,
) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {"error": {"message": str(error.reason)}}
        return error.code, payload


def _is_timeout(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or (
        isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)
    )


def _positive_timeout(value: float) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("ACQ_EXTRACTION_TIMEOUT_SECONDS must be a positive finite number")
    return timeout


def _response_text(response: dict) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    for item in response.get("output") or []:
        for content in item.get("content") or []:
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                return text
    raise ProviderError("provider response did not contain output_text")


def _cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    input_price, output_price = MODEL_PRICING.get(
        model, (Decimal("2.50"), Decimal("10.00")),
    )
    return (
        (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price)
        / Decimal(1_000_000)
    ).quantize(Decimal("0.000001"))


class WholePdfProviderClient:
    """Sends one complete original PDF to the OpenAI Responses API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        base_delay: float = 0.5,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.api_key = api_key if api_key is not None else settings.extraction_api_key
        self.base_url = (base_url or settings.extraction_base_url).rstrip("/")
        self.model = model or settings.whole_pdf_model
        self.timeout = _positive_timeout(
            timeout if timeout is not None else settings.extraction_timeout_seconds
        )
        self.max_retries = max_retries or settings.extraction_max_retries
        self.base_delay = base_delay
        self.transport = transport or _urllib_transport
        self.sleep = sleep

    def analyze_pdf(self, pdf_path: Path, *, doc_kind: DocumentKind = "property_profile",
                    log_context: dict | None = None) -> ProviderAnalysis:
        if not self.api_key:
            raise PermanentProviderError("extraction API key is not configured")
        pdf_bytes = pdf_path.read_bytes()
        if not pdf_bytes.startswith(b"%PDF-"):
            raise PermanentProviderError("stored document is not a PDF")
        pdf_data_url = (
            "data:application/pdf;base64,"
            f"{base64.b64encode(pdf_bytes).decode('ascii')}"
        )
        payload = {
            "model": self.model,
            "input": [{
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "filename": pdf_path.name or "report.pdf",
                        "file_data": pdf_data_url,
                    },
                    {"type": "input_text", "text": OWNER_SYSTEM_PROMPT if doc_kind == "owner_profile" else SYSTEM_PROMPT},
                ],
            }],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "owner_profile_extraction" if doc_kind == "owner_profile" else "property_report_extraction",
                    "schema": owner_schema() if doc_kind == "owner_profile" else canonical_schema(),
                    "strict": True,
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/responses"
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        correlation = dict(log_context or {})
        started = time.monotonic()
        attempt = 0
        while attempt < self.max_retries:
            attempt += 1
            log.info("whole PDF provider request started", extra={
                **correlation,
                "event": "provider_request_started",
                "stage": "provider_request",
                "model": self.model,
                "provider_host": urlparse(self.base_url).hostname,
                "timeout_seconds": self.timeout,
                "attempt": attempt,
            })
            try:
                status, response = self.transport("POST", url, headers, body, self.timeout)
            except Exception as exc:
                timeout_error = _is_timeout(exc)
                will_retry = attempt < self.max_retries
                log.warning(
                    "whole PDF provider timed out" if timeout_error else "provider transport failed",
                    extra={
                        **correlation,
                        "event": "provider_timeout" if timeout_error else "provider_error",
                        "stage": "provider_request",
                        "success": False,
                        "model": self.model,
                        "provider_host": urlparse(self.base_url).hostname,
                        "timeout_seconds": self.timeout,
                        "attempt": attempt,
                        "retry_count": attempt - 1,
                        "will_retry": will_retry,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                    exc_info=True,
                )
                if not will_retry:
                    error_type = ProviderTimeout if timeout_error else ProviderError
                    raise error_type(str(exc)) from exc
                self.sleep(self.base_delay * (2 ** (attempt - 1)))
                continue

            if 200 <= status < 300:
                try:
                    extracted = json.loads(_response_text(response))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ProviderError("provider returned invalid structured JSON") from exc
                usage = response.get("usage") or {}
                input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                model_used = str(response.get("model") or self.model)
                duration_ms = round((time.monotonic() - started) * 1000)
                log.info("whole PDF provider response completed", extra={
                    **correlation,
                    "event": "provider_response_completed",
                    "stage": "provider_request",
                    "success": True,
                    "model": model_used,
                    "provider_status_code": status,
                    "duration_ms": duration_ms,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "retry_count": attempt - 1,
                })
                return ProviderAnalysis(
                    extracted, model_used, input_tokens, output_tokens,
                    _cost(model_used, input_tokens, output_tokens), duration_ms, attempt,
                )

            message = str((response.get("error") or {}).get("message") or f"HTTP {status}")
            if 400 <= status < 500 and status != 429:
                log.error("whole PDF provider rejected request", extra={
                    **correlation,
                    "event": "provider_error",
                    "stage": "provider_request",
                    "success": False,
                    "model": self.model,
                    "provider_status_code": status,
                    "attempt": attempt,
                    "will_retry": False,
                    "error_type": "provider_rejection",
                    "error_message": message,
                })
                raise PermanentProviderError(f"provider rejected request ({status}): {message}")
            will_retry = attempt < self.max_retries
            log.warning("whole PDF provider request failed", extra={
                **correlation,
                "event": "provider_error",
                "stage": "provider_request",
                "success": False,
                "model": self.model,
                "provider_status_code": status,
                "attempt": attempt,
                "retry_count": attempt - 1,
                "will_retry": will_retry,
                "error_type": "provider_http_error",
                "error_message": message,
            })
            if not will_retry:
                raise ProviderError(f"provider failed after {attempt} attempts ({status}): {message}")
            self.sleep(self.base_delay * (2 ** (attempt - 1)))
        raise ProviderError("provider retries exhausted")
