"""Grounded, bounded portfolio chat provider surface."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

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

SYSTEM_PROMPT = """You are ACQ's property-analysis assistant.
Every number you state must be copied from STRUCTURED_CONTEXT or TOOL_RESULTS. Never calculate,
estimate, interpolate, or recompute a figure. Cite the source field name for structured figures or
the report page for document passages. If the supplied data cannot answer, say so. Owner contacts,
liens, and bankruptcies are reference-only and never alter underwriting, scoring, offer grids, or
strategy viability. Do not expose owner contact data unless it appears in TOOL_RESULTS.
"""


@dataclass(frozen=True)
class ChatTurn:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    model: str
    tool_results: dict = field(default_factory=dict)


class ChatProviderClient:
    def __init__(self, *, api_key: str | None = None, model: str | None = None,
                 transport=None):
        self.api_key = api_key if api_key is not None else settings.extraction_api_key
        self.model = model or settings.chat_model
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
                    raise ProviderError("chat provider transport failed") from exc
                time.sleep(0.25 * (2 ** (attempt - 1)))
                continue
            if 200 <= status < 300:
                return response
            if 400 <= status < 500 and status != 429:
                raise PermanentProviderError(f"chat provider rejected request ({status})")
            if attempt >= attempts:
                raise ProviderError(f"chat provider failed ({status})")
            time.sleep(0.25 * (2 ** (attempt - 1)))
        raise ProviderError("chat provider failed")

    def complete(
        self, messages: list[dict], structured_context: dict, tool_results: dict,
        *, tool_definitions: list[dict] | None = None,
        execute_tool: Callable[[str, dict], object] | None = None,
    ) -> ChatTurn:
        if not self.api_key:
            raise PermanentProviderError("chat provider API key is not configured")
        payload: dict = {
            "model": self.model,
            "input": [{"role": "system", "content": SYSTEM_PROMPT}, {
                "role": "user",
                "content": "STRUCTURED_CONTEXT:\n" + json.dumps(structured_context, default=str)
                + "\nTOOL_RESULTS:\n" + json.dumps(tool_results, default=str),
            }, *messages],
        }
        if tool_definitions:
            payload["tools"] = tool_definitions
        gathered = dict(tool_results)
        total_input = total_output = 0
        total_cost = Decimal(0)
        model = self.model
        for _round in range(5):
            response = self._request(payload)
            usage = response.get("usage") or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            model = str(response.get("model") or self.model)
            total_input += input_tokens
            total_output += output_tokens
            total_cost += _cost(model, input_tokens, output_tokens)
            calls = [item for item in response.get("output") or []
                     if item.get("type") == "function_call"]
            if not calls:
                return ChatTurn(
                    _response_text(response), total_input, total_output,
                    total_cost, model, gathered,
                )
            if execute_tool is None:
                raise ProviderError("chat provider requested a tool without an executor")
            response_id = response.get("id")
            if not response_id:
                raise ProviderError("tool-calling response did not include an id")
            outputs = []
            for call in calls:
                name = str(call.get("name") or "")
                raw_arguments = call.get("arguments") or "{}"
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                if not isinstance(arguments, dict):
                    raise ProviderError("chat tool arguments were not an object")
                result = execute_tool(name, arguments)
                if name in gathered:
                    current = gathered[name]
                    gathered[name] = current + [result] if isinstance(current, list) else [current, result]
                else:
                    gathered[name] = result
                outputs.append({
                    "type": "function_call_output", "call_id": call.get("call_id"),
                    "output": json.dumps(result, default=str),
                })
            payload = {
                "model": self.model, "previous_response_id": response_id,
                "input": outputs,
            }
            if tool_definitions:
                payload["tools"] = tool_definitions
        raise ProviderError("chat exceeded the maximum tool-call depth")


def _enriched_allowed_numbers(context: dict, tool_results: dict) -> set[Decimal]:
    """Grounding set with phrasing tolerance: percent forms of ratios (including
    human-style rounding like 92% for 0.9167), money rounded to thousands, and
    K/M shorthand forms ("$725K" extracts as 725, "$1.2M" as 1.2)."""
    allowed = allowed_numbers({"context": context, "tools": tool_results})
    enriched = set(allowed)
    for value in allowed:
        if Decimal(0) <= value <= Decimal(1):
            percent = value * Decimal(100)
            enriched.add(percent)
            enriched.add(percent.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
            enriched.add(percent.to_integral_value(rounding=ROUND_HALF_UP))
        magnitude = abs(value)
        if magnitude >= Decimal(1000):
            enriched.add((value / Decimal(1000)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))
        if magnitude >= Decimal(1_000_000):
            enriched.add((value / Decimal(1_000_000)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))
    return enriched


def _is_incidental(number: Decimal) -> bool:
    """Small whole numbers (either sign — page ranges like '7-9' extract as -9)
    are counts, dates, day/month references, or list indices; never material
    financial claims."""
    return number == number.to_integral_value() and abs(number) <= Decimal(31)


def ungrounded_numbers(text: str, context: dict, tool_results: dict) -> list[Decimal]:
    """Numbers in ``text`` with no basis in the supplied data (diagnostics)."""
    allowed = _enriched_allowed_numbers(context, tool_results)
    return [value for value in extract_numbers(text)
            if value not in allowed and not _is_incidental(value)]


def validate_grounded_numbers(text: str, context: dict, tool_results: dict) -> bool:
    return not ungrounded_numbers(text, context, tool_results)


_SENTENCE_RE = __import__("re").compile(r"(?<=[.!?])\s+")

_SALVAGE_NOTE = ("\n\n(Some figures were omitted from this answer because they could not be "
                 "verified against the deal data.)")


def _salvage_grounded_sentences(text: str, context: dict,
                                tool_results: dict) -> tuple[str, int]:
    """Keep every sentence whose numbers are grounded; drop only offending ones.

    A single computed aside ("$50k higher") no longer discards an otherwise
    correct comparison. Returns (filtered_text | None, dropped_count); None
    when there is nothing grounded to keep."""
    sentences = _SENTENCE_RE.split(text.strip())
    kept = []
    dropped = 0
    for sentence in sentences:
        if not sentence.strip():
            continue
        if ungrounded_numbers(sentence, context, tool_results):
            dropped += 1
        else:
            kept.append(sentence.strip())
    if dropped == 0 or not kept:
        return None, dropped
    return " ".join(kept) + _SALVAGE_NOTE, dropped


_GROUNDED_FALLBACK = ("I couldn't answer that using only the grounded deal data. "
                      "Rephrase the question, or ask me to pull a specific document page.")


def answer_chat(provider: ChatProviderClient, messages: list[dict],
                structured_context: dict, tool_results: dict, *,
                tool_definitions: list[dict] | None = None,
                execute_tool: Callable[[str, dict], object] | None = None) -> ChatTurn:
    trimmed = messages[-settings.chat_history_messages:]
    approximate_tokens = sum(len(str(message.get("content", ""))) for message in trimmed) // 4
    if approximate_tokens > settings.chat_session_token_cap:
        raise ValueError("chat session token cap exceeded")
    if tool_definitions is None:
        turn = provider.complete(trimmed, structured_context, tool_results)
    else:
        turn = provider.complete(
            trimmed, structured_context, tool_results,
            tool_definitions=tool_definitions, execute_tool=execute_tool,
        )
    grounding_tools = {**tool_results, **turn.tool_results}
    if validate_grounded_numbers(turn.text, structured_context, grounding_tools):
        return turn

    def _diagnose(text: str, tools: dict) -> dict:
        return {"event": "chat_grounding_rejected",
                "ungrounded_numbers": [str(value) for value in
                                       ungrounded_numbers(text, structured_context, tools)]}

    # First resort: keep the grounded sentences and drop only offending ones —
    # one computed aside must not discard an otherwise correct comparison.
    salvaged, dropped = _salvage_grounded_sentences(turn.text, structured_context, grounding_tools)
    if salvaged is not None:
        log.warning("chat reply partially grounded; salvaged sentences", extra={
            "event": "chat_grounding_salvaged", "dropped_sentences": dropped,
            "ungrounded_numbers": _diagnose(turn.text, grounding_tools)["ungrounded_numbers"],
        })
        return ChatTurn(salvaged, turn.input_tokens, turn.output_tokens,
                        turn.cost_usd, turn.model, grounding_tools)

    log.warning("chat reply failed grounding entirely; retrying",
                extra=_diagnose(turn.text, grounding_tools))
    # Second resort: one corrective retry with explicit verbatim instructions.
    retry_messages = [*trimmed,
                       {"role": "assistant", "content": turn.text},
                       {"role": "user", "content":
                        "Your previous reply contained numbers that do not appear in STRUCTURED_CONTEXT "
                        "or TOOL_RESULTS. Answer again using ONLY numbers copied verbatim from those "
                        "sources. If the data cannot answer the question, say so plainly."}]
    second = provider.complete(
        retry_messages, structured_context, dict(grounding_tools),
        tool_definitions=tool_definitions, execute_tool=execute_tool,
    )
    merged_tools = {**grounding_tools, **second.tool_results}
    total_input = turn.input_tokens + second.input_tokens
    total_output = turn.output_tokens + second.output_tokens
    total_cost = turn.cost_usd + second.cost_usd
    if validate_grounded_numbers(second.text, structured_context, merged_tools):
        return ChatTurn(second.text, total_input, total_output, total_cost,
                        second.model, merged_tools)
    salvaged, dropped = _salvage_grounded_sentences(second.text, structured_context, merged_tools)
    if salvaged is not None:
        log.warning("retry partially grounded; salvaged sentences", extra={
            "event": "chat_grounding_salvaged", "dropped_sentences": dropped,
            **_diagnose(second.text, merged_tools)})
        return ChatTurn(salvaged, total_input, total_output, total_cost,
                        second.model, merged_tools)
    log.warning("chat grounding failed after retry; returning safe fallback", extra={
        "event": "chat_grounding_fallback", **_diagnose(second.text, merged_tools)})
    return ChatTurn(_GROUNDED_FALLBACK, total_input, total_output, total_cost,
                    second.model, merged_tools)
