"""Grounded, bounded portfolio chat provider surface."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

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


def validate_grounded_numbers(text: str, context: dict, tool_results: dict) -> bool:
    allowed = allowed_numbers({"context": context, "tools": tool_results})
    return all(value in allowed for value in extract_numbers(text))


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
    if not validate_grounded_numbers(turn.text, structured_context, grounding_tools):
        raise ValueError("chat response contained an ungrounded number")
    return turn
