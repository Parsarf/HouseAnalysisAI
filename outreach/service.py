"""Compliant cash-offer outreach drafting."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

from analyst.comparison import allowed_numbers, extract_numbers
from common.settings import settings
from report_analysis.provider import (
    PermanentProviderError,
    ProviderError,
    _cost,
    _response_text,
    _urllib_transport,
)

log = logging.getLogger(__name__)

DISTRESS_PATTERN = re.compile(
    r"\b(foreclos\w*|auction\w*|default\w*|bankrupt\w*|distress\w*|trustee(?:'s)? sale|notice of sale|notice of default)\b",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """Write a concise plain-text residential real-estate cash-offer email.
Lead with the exact cash offer supplied in CONTEXT. Mention cash, no financing contingency,
flexible timing, correct property details, and a low-pressure exit. Never mention or imply
foreclosure, auction, default, bankruptcy, financial distress, liens, urgency caused by legal
proceedings, or the owner's personal circumstances. Use no number absent from CONTEXT. Return JSON
with exactly subject and body. Append the configurable disclosure verbatim when it is non-empty.
"""


@dataclass(frozen=True)
class Draft:
    subject: str
    body: str


class OutreachProviderClient:
    def __init__(self, *, api_key: str | None = None, transport=None):
        self.api_key = api_key if api_key is not None else settings.extraction_api_key
        self.transport = transport or _urllib_transport

    def _request(self, payload: dict) -> dict:
        attempts = max(1, settings.extraction_max_retries)
        for attempt in range(1, attempts + 1):
            try:
                status, response = self.transport(
                    "POST", f"{settings.extraction_base_url.rstrip('/')}/responses",
                    {"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
                    json.dumps(payload).encode(), settings.extraction_timeout_seconds,
                )
            except Exception as exc:
                if attempt >= attempts:
                    raise ProviderError("outreach provider transport failed") from exc
                time.sleep(0.25 * (2 ** (attempt - 1)))
                continue
            if 200 <= status < 300:
                return response
            if 400 <= status < 500 and status != 429:
                raise PermanentProviderError(f"outreach provider rejected request ({status})")
            if attempt >= attempts:
                raise ProviderError(f"outreach provider failed ({status})")
            time.sleep(0.25 * (2 ** (attempt - 1)))
        raise ProviderError("outreach provider failed")

    def generate(self, context: dict, *, prior_draft: dict | None = None,
                 instruction: str | None = None) -> Draft:
        if not self.api_key:
            raise ProviderError("outreach provider API key is not configured")
        payload = {
            "model": settings.chat_model,
            "input": [{"role": "system", "content": SYSTEM_PROMPT}, {
                "role": "user", "content": json.dumps({
                    "context": context, "prior_draft": prior_draft,
                    "revision_instruction": instruction,
                    "disclosure": settings.outreach_disclosure,
                }, default=str),
            }],
            "text": {"format": {"type": "json_schema", "name": "outreach_draft", "strict": True,
                                  "schema": {"type": "object", "additionalProperties": False,
                                             "properties": {"subject": {"type": "string"},
                                                            "body": {"type": "string"}},
                                             "required": ["subject", "body"]}}},
        }
        response = self._request(payload)
        usage = response.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        model = str(response.get("model") or settings.chat_model)
        log.info("outreach draft provider response completed", extra={
            "event": "outreach_provider_response_completed", "model": model,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cost_usd": _cost(model, input_tokens, output_tokens),
        })
        parsed = json.loads(_response_text(response))
        return Draft(subject=str(parsed["subject"]), body=str(parsed["body"]))


def _with_disclosure(draft: Draft) -> Draft:
    disclosure = settings.outreach_disclosure.strip()
    if not disclosure or disclosure in draft.body:
        return draft
    return Draft(draft.subject, f"{draft.body.rstrip()}\n\n{disclosure}")


def validate_draft(draft: Draft, context: dict | None = None) -> bool:
    text = f"{draft.subject}\n{draft.body}"
    if DISTRESS_PATTERN.search(text):
        return False
    if context is None:
        return True
    grounded = allowed_numbers({"context": context, "disclosure": settings.outreach_disclosure})
    return all(number in grounded for number in extract_numbers(text))


def generate_draft(provider: OutreachProviderClient, context: dict, *,
                   prior_draft: dict | None = None, instruction: str | None = None) -> Draft:
    draft = _with_disclosure(provider.generate(
        context, prior_draft=prior_draft, instruction=instruction,
    ))
    if validate_draft(draft, context):
        return draft
    regenerated = _with_disclosure(provider.generate(
        context, prior_draft=prior_draft,
        instruction=(instruction or "")
        + " Remove prohibited topics and use only numbers present in the supplied context.",
    ))
    if not validate_draft(regenerated, context):
        raise ValueError("generated draft violated outreach content policy")
    return regenerated
