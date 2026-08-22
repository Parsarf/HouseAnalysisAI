"""Streaming grounded chat over deterministic portfolio data."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from decimal import Decimal
from itertools import pairwise
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from analyst.comparison import why_above
from auth.dependencies import User, current_user
from chat import ChatProviderClient, answer_chat
from common.errors import AcqError, ErrorCode
from common.serializers import json_safe
from common.settings import settings
from common.storage import get_document_storage
from db import models as dbm
from ops.chat_budget import (
    cache_document_text,
    cached_document_text,
    chat_session_key,
    reconcile_chat_session_tokens,
    reconcile_daily_chat_budget,
    reserve_chat_session_tokens,
    reserve_daily_chat_budget,
)
from report_analysis.provider import PermanentProviderError, ProviderError

from . import analysis as analysis_store
from .deps import get_session
from .routes_owner import owner_profile_payload
from .serializers import score_set

router = APIRouter(prefix="/api", tags=["chat"])
log = logging.getLogger(__name__)

TOOL_DEFINITIONS = [
    {
        "type": "function", "name": "list_documents",
        "description": "List source documents and document kinds for one property.",
        "strict": True,
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {"property_id": {"type": "string"}},
            "required": ["property_id"],
        },
    },
    {
        "type": "function", "name": "get_document_text",
        "description": "Retrieve a bounded page range from a property source document.",
        "strict": True,
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "report_id": {"type": "string"}, "page_start": {"type": "integer"},
                "page_end": {"type": ["integer", "null"]},
            },
            "required": ["report_id", "page_start", "page_end"],
        },
    },
    {
        "type": "function", "name": "compare_properties",
        "description": "Run ACQ's deterministic comparison over two or more properties.",
        "strict": True,
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "property_ids": {"type": "array", "items": {"type": "string"},
                                 "minItems": 2, "maxItems": 10},
                "scenario": {"type": "string", "enum": ["conservative", "expected", "optimistic"]},
            },
            "required": ["property_ids", "scenario"],
        },
    },
    {
        "type": "function", "name": "search_portfolio",
        "description": "Find active portfolio properties using database filters.",
        "strict": True,
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "address": {"type": ["string", "null"]},
                "city": {"type": ["string", "null"]},
                "state": {"type": ["string", "null"]},
                "pipeline_status": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["address", "city", "state", "pipeline_status", "limit"],
        },
    },
    {
        "type": "function", "name": "get_owner_profile",
        "description": "Retrieve owner contacts, liens, and bankruptcies only when owner data is needed.",
        "strict": True,
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {"property_id": {"type": "string"}},
            "required": ["property_id"],
        },
    },
]


def _structured_property(session: Session, property_id: UUID) -> dict:
    if session.get(dbm.Property, property_id) is None:
        raise AcqError(ErrorCode.NOT_FOUND, f"property {property_id} not found")
    record = analysis_store.load_normalized(session, property_id)
    underwriting = analysis_store.load_underwriting(session, property_id, record)
    owner = owner_profile_payload(session, property_id)
    return json_safe({
        "property_id": property_id,
        "normalized": record,
        "underwriting": underwriting,
        "scores": analysis_store.load_scores(session, property_id),
        "strategies": analysis_store.load_strategies(session, property_id),
        "flags": analysis_store.load_flags(session, property_id),
        "owner_summary": {
            "owners": [{
                "id": item["id"], "full_name": item["full_name"],
                "is_absentee": item["is_absentee"],
            } for item in owner["owners"]],
            "serial_filing": owner["serial_filing"],
            "owner_lien_total": owner["owner_lien_total"],
        },
    })


def _list_documents(session: Session, property_id: UUID) -> list[dict]:
    rows = session.query(dbm.Report).filter(dbm.Report.property_id == property_id).all()
    owner_ids = {row[0] for row in session.query(dbm.PropertyOwner.owner_id).filter(
        dbm.PropertyOwner.property_id == property_id,
    ).all()}
    if owner_ids:
        for extraction, report in session.query(dbm.ReportExtraction, dbm.Report).join(
            dbm.Report, dbm.Report.id == dbm.ReportExtraction.report_id,
        ).filter(dbm.Report.doc_kind == "owner_profile").all():
            owner_id = (extraction.normalized_json or {}).get("owner_id")
            if owner_id and UUID(str(owner_id)) in owner_ids:
                rows.append(report)
    unique = {row.id: row for row in rows}
    return [{
        "report_id": str(row.id), "doc_kind": row.doc_kind or "property_profile",
        "report_type": row.report_type, "generated_date": row.generated_date,
        "page_count": row.page_count,
    } for row in unique.values()]


def _document_text(session: Session, report_id: UUID, session_key: str,
                   page_start: int = 1, page_end: int | None = None) -> dict:
    report = session.get(dbm.Report, report_id)
    if report is None:
        raise AcqError(ErrorCode.NOT_FOUND, "document not found")
    if report.doc_kind == "owner_profile":
        raise AcqError(
            ErrorCode.INVALID_INPUT,
            "owner documents are available only through the owner-profile tool",
        )
    start = max(1, page_start)
    end_requested = max(start, page_end or start)
    end_requested = min(end_requested, start + 9)
    cache_key = f"{report_id}:{start}:{end_requested}"
    cached = cached_document_text(session, session_key, cache_key)
    if cached is not None:
        return cached
    storage = get_document_storage()
    with storage.materialize(report.file_path) as path:
        import fitz

        with fitz.open(path) as document:
            end = min(end_requested, len(document))
            pages = [{"page": number, "text": document[number - 1].get_text()}
                     for number in range(start, end + 1)]
    result = {"report_id": str(report_id), "pages": pages}
    cache_document_text(session, session_key, cache_key, result)
    return result


def _compare(session: Session, property_ids: list[UUID], scenario: str) -> dict:
    scores = []
    scenario_results = []
    for property_id in property_ids:
        row = session.query(dbm.Score).filter(dbm.Score.property_id == property_id).order_by(
            dbm.Score.computed_at.desc().nullslast(), dbm.Score.id.desc(),
        ).first()
        if row is not None:
            scores.append((property_id, score_set(row)))
        strategies = [
            result for result in analysis_store.load_strategies(session, property_id)
            if result.scenario.value == scenario
        ]
        scenario_results.append({"property_id": property_id, "strategies": strategies})
    comparisons = []
    for (left_id, left), (right_id, right) in pairwise(scores):
        comparison, explanation = why_above(left, right, a_label=str(left_id), b_label=str(right_id))
        comparisons.append({"comparison": comparison, "explanation": explanation})
    return json_safe({
        "scenario": scenario, "score_comparisons": comparisons,
        "scenario_strategy_results": scenario_results,
    })


def _search_portfolio(session: Session, arguments: dict) -> list[dict]:
    query = session.query(dbm.Property).filter(
        dbm.Property.merged_into_id.is_(None), dbm.Property.archived_at.is_(None),
    )
    if arguments.get("address"):
        query = query.filter(dbm.Property.address_line1.ilike(f"%{arguments['address']}%"))
    for name in ("city", "state", "pipeline_status"):
        if arguments.get(name):
            query = query.filter(getattr(dbm.Property, name) == str(arguments[name]))
    rows = query.order_by(dbm.Property.id).limit(max(1, min(int(arguments.get("limit") or 20), 50))).all()
    return [{
        "property_id": str(row.id), "address": row.address_line1,
        "city": row.city, "state": row.state, "pipeline_status": row.pipeline_status,
    } for row in rows]


def _tool_executor(session: Session, session_key: str) -> Callable[[str, dict], object]:
    def execute(name: str, arguments: dict) -> object:
        try:
            if name == "list_documents":
                return _list_documents(session, UUID(str(arguments["property_id"])))
            if name == "get_document_text":
                return _document_text(
                    session, UUID(str(arguments["report_id"])), session_key,
                    int(arguments.get("page_start") or 1),
                    int(arguments["page_end"]) if arguments.get("page_end") else None,
                )
            if name == "compare_properties":
                property_ids = [UUID(str(value)) for value in arguments.get("property_ids") or []]
                if len(property_ids) < 2:
                    raise ValueError("at least two properties are required")
                return _compare(session, property_ids, str(arguments.get("scenario") or "expected"))
            if name == "search_portfolio":
                return _search_portfolio(session, arguments)
            if name == "get_owner_profile":
                property_id = UUID(str(arguments["property_id"]))
                if session.get(dbm.Property, property_id) is None:
                    raise AcqError(ErrorCode.NOT_FOUND, "property not found")
                return owner_profile_payload(session, property_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise AcqError(ErrorCode.INVALID_INPUT, f"invalid {name} tool arguments") from exc
        raise AcqError(ErrorCode.INVALID_INPUT, f"unknown chat tool: {name}")
    return execute


@router.post("/chat")
def chat(body: dict, session: Session = Depends(get_session),
         user: User = Depends(current_user)) -> StreamingResponse:
    raw_messages = body.get("messages") or []
    if not isinstance(raw_messages, list) or not raw_messages:
        raise AcqError(ErrorCode.INVALID_INPUT, "messages are required")
    messages = []
    for message in raw_messages:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            raise AcqError(ErrorCode.INVALID_INPUT, "chat messages must use user or assistant roles")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 20_000:
            raise AcqError(ErrorCode.INVALID_INPUT, "chat message content is invalid")
        messages.append({"role": message["role"], "content": content.strip()})
    try:
        property_ids = [UUID(str(value)) for value in body.get("property_ids") or []]
        chat_session_id = UUID(str(body["session_id"])) if body.get("session_id") else uuid4()
    except ValueError as exc:
        raise AcqError(ErrorCode.INVALID_INPUT, "invalid property or chat session id") from exc
    if len(property_ids) > 10:
        raise AcqError(ErrorCode.INVALID_INPUT, "select no more than 10 properties")
    context = {str(property_id): _structured_property(session, property_id)
               for property_id in property_ids}
    tools: dict = {}
    encoded_size = len(json.dumps({"c": context, "m": messages}, default=str))
    estimated_tokens = encoded_size // 4 + 2_048
    session_key = chat_session_key(user.id, chat_session_id)
    if not reserve_chat_session_tokens(
        session, session_key, estimated_tokens, settings.chat_session_token_cap,
    ):
        raise AcqError(ErrorCode.BUDGET_PAUSED, "chat session token cap reached")
    estimated = max(
        Decimal("0.01"), Decimal(estimated_tokens) * Decimal(10) / Decimal(1_000_000),
    )
    if not reserve_daily_chat_budget(session, estimated, settings.chat_daily_spend_cap_usd):
        reconcile_chat_session_tokens(session, session_key, estimated_tokens, 0)
        raise AcqError(ErrorCode.BUDGET_PAUSED, "daily chat spend cap reached")
    try:
        turn = answer_chat(
            ChatProviderClient(), messages, context, tools,
            tool_definitions=TOOL_DEFINITIONS,
            execute_tool=_tool_executor(session, session_key),
        )
    except ValueError as exc:
        reconcile_daily_chat_budget(session, estimated, Decimal(0))
        reconcile_chat_session_tokens(session, session_key, estimated_tokens, 0)
        raise AcqError(ErrorCode.INTERNAL, "chat response failed grounding validation") from exc
    except (PermanentProviderError, ProviderError) as exc:
        reconcile_daily_chat_budget(session, estimated, Decimal(0))
        reconcile_chat_session_tokens(session, session_key, estimated_tokens, 0)
        raise AcqError(ErrorCode.INTERNAL, "chat provider unavailable") from exc
    reconcile_daily_chat_budget(session, estimated, turn.cost_usd)
    reconcile_chat_session_tokens(
        session, session_key, estimated_tokens, turn.input_tokens + turn.output_tokens,
    )
    log.info("chat turn completed", extra={
        "event": "chat_turn_completed", "model": turn.model,
        "input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens,
        "cost_usd": turn.cost_usd,
    })

    def stream():
        for chunk in (turn.text[index:index + 160] for index in range(0, len(turn.text), 160)):
            yield f"data: {json.dumps({'delta': chunk})}\n\n"
        yield f"data: {json.dumps({'done': True, 'cost_usd': str(turn.cost_usd), 'session_id': str(chat_session_id)})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
