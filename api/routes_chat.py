"""Streaming grounded chat over deterministic portfolio data."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from itertools import combinations
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from analyst.comparison import why_above
from auth.dependencies import User, current_user
from chat import ChatProviderClient, answer_chat
from common.errors import AcqError, ErrorCode
from common.settings import settings
from common.storage import get_document_storage
from contracts import Scenario, ScoreSet
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


def _chat_safe(value):
    """Keep nested contracts structured instead of stringifying Pydantic models."""
    if isinstance(value, BaseModel):
        return _chat_safe(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _chat_safe(asdict(value))
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _chat_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_chat_safe(item) for item in value]
    return value


def _property_label(row: dbm.Property) -> str:
    return ", ".join(str(item) for item in (
        row.address_line1, row.city, row.state, row.zip5,
    ) if item) or str(row.id)


def _source_evidence(session: Session, property_id: UUID) -> dict:
    from report_analysis.read_model import active_entities

    entities = active_entities(session, property_id)
    facts = session.query(dbm.ExtractedFact).filter(
        dbm.ExtractedFact.property_id == property_id,
        dbm.ExtractedFact.is_active.is_(True),
    ).order_by(dbm.ExtractedFact.field_path, dbm.ExtractedFact.page_number).all()
    return {
        "document_entities": [{
            "report_id": entity.report_id,
            "role": entity.role,
            "source_pages": entity.source_pages,
            "source_references": (entity.raw_json or {}).get("source_references", []),
            "additional_facts": (entity.raw_json or {}).get("additional_facts", []),
        } for entity in entities],
        "field_evidence": [{
            "field_path": fact.field_path,
            "value_raw": fact.value_raw,
            "value_parsed": fact.value_parsed,
            "value_text": fact.value_text,
            "value_date": fact.value_date,
            "value_bool": fact.value_bool,
            "unit": fact.unit,
            "as_of_date": fact.as_of_date,
            "report_id": fact.report_id,
            "page_number": fact.page_number,
            "snippet": fact.snippet,
            "extraction_confidence": fact.extraction_confidence,
            "source_kind": fact.source_kind,
        } for fact in facts],
    }


def _structured_property(session: Session, property_id: UUID) -> dict:
    row = session.get(dbm.Property, property_id)
    if row is None:
        raise AcqError(ErrorCode.NOT_FOUND, f"property {property_id} not found")
    record = analysis_store.load_normalized(session, property_id)
    underwriting = analysis_store.load_underwriting(session, property_id, record)
    owner = owner_profile_payload(session, property_id)
    offers = {
        scenario.value: analysis_store.load_offers(
            session, property_id, scenario, underwriting,
        )
        for scenario in Scenario
    }
    return _chat_safe({
        "property_id": property_id,
        "label": _property_label(row),
        "property": {
            "apn": row.apn,
            "address_line1": row.address_line1,
            "city": row.city,
            "state": row.state,
            "zip5": row.zip5,
            "county_fips": row.fips_county,
            "latitude": row.lat,
            "longitude": row.lng,
            "property_type": row.property_type,
            "beds": row.beds,
            "baths": row.baths,
            "sqft": row.sqft,
            "lot_sqft": row.lot_sqft,
            "year_built": row.year_built,
            "units": row.units,
            "pipeline_status": row.pipeline_status,
            "underwriting_status": row.underwriting_status,
            "tags": row.tags,
            "next_action": row.next_action,
            "next_action_date": row.next_action_date,
            "gut_rating": row.gut_rating,
            "is_watchlisted": row.is_watchlisted,
        },
        "normalized": record,
        "underwriting": underwriting,
        "scores": analysis_store.load_scores(session, property_id),
        "strategies": analysis_store.load_strategies(session, property_id),
        "offers_by_scenario": offers,
        "flags": analysis_store.load_flags(session, property_id),
        "timeline": analysis_store.load_timeline(session, property_id),
        "source_documents": _list_documents(session, property_id),
        "source_evidence": _source_evidence(session, property_id),
        "owner_analysis": {
            "owners": [{
                "id": item["id"], "full_name": item["full_name"],
                "age": item["age"], "gender": item["gender"],
                "is_absentee": item["is_absentee"],
            } for item in owner["owners"]],
            "liens": owner["liens"],
            "bankruptcies": owner["bankruptcies"],
            "serial_filing": owner["serial_filing"],
            "timeline": owner["timeline"],
            "owner_lien_total": owner["owner_lien_total"],
        },
    })


def _list_documents(session: Session, property_id: UUID) -> list[dict]:
    from report_analysis.read_model import reports_for_property
    rows = reports_for_property(session, property_id)
    owner_ids = {row[0] for row in session.query(dbm.PropertyOwner.owner_id).filter(
        dbm.PropertyOwner.property_id == property_id,
    ).all()}
    if owner_ids:
        owner_entities = session.query(dbm.ReportEntityExtraction).filter(
            dbm.ReportEntityExtraction.owner_id.in_(owner_ids),
            dbm.ReportEntityExtraction.active.is_(True),
        ).all()
        rows.extend(session.query(dbm.Report).filter(
            dbm.Report.id.in_([entity.report_id for entity in owner_entities]),
        ).all())
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
    from report_analysis.read_model import source_report_id
    owner_entities = session.query(dbm.ReportEntityExtraction).filter(
        dbm.ReportEntityExtraction.report_id == source_report_id(session, report),
        dbm.ReportEntityExtraction.kind == "owner",
    ).all()
    if any(start <= page <= end_requested for entity in owner_entities for page in entity.source_pages):
        raise AcqError(ErrorCode.INVALID_INPUT, "Pages containing owner profiles require the owner-profile tool")
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
    rows = {
        property_id: session.get(dbm.Property, property_id)
        for property_id in property_ids
    }
    missing = [str(property_id) for property_id, row in rows.items() if row is None]
    if missing:
        raise AcqError(ErrorCode.NOT_FOUND, f"properties not found: {', '.join(missing)}")
    labels = {
        property_id: _property_label(row)
        for property_id, row in rows.items() if row is not None
    }
    scores: list[tuple[UUID, ScoreSet]] = []
    results_by_property: dict[UUID, list] = {}
    for property_id in property_ids:
        score = analysis_store.load_scores(session, property_id)
        if score is not None:
            scores.append((property_id, score))
        results_by_property[property_id] = [
            result for result in analysis_store.load_strategies(session, property_id)
            if result.scenario.value == scenario
        ]
    score_comparisons = []
    for (left_id, left), (right_id, right) in combinations(scores, 2):
        comparison, explanation = why_above(
            left, right, a_label=labels[left_id], b_label=labels[right_id],
        )
        score_comparisons.append({
            "left_property_id": left_id,
            "right_property_id": right_id,
            "comparison": comparison,
            "explanation": explanation,
        })

    strategies = sorted({
        result.strategy.value
        for results in results_by_property.values() for result in results
    })
    strategy_comparisons = []
    metric_names = (
        "mao", "all_in_basis", "profit", "roi", "margin_of_safety",
        "purchase_price", "repairs", "holding", "financing", "resale",
        "arv", "cap_rate", "cash_flow", "coc",
    )
    for strategy in strategies:
        entries = []
        numeric_by_property: dict[UUID, dict[str, Decimal]] = {}
        for property_id in property_ids:
            result = next((item for item in results_by_property[property_id]
                           if item.strategy.value == strategy), None)
            if result is None:
                continue
            dumped = result.model_dump(mode="python")
            metrics = dict(dumped.get("metrics") or {})
            values = {
                name: dumped.get(name) if dumped.get(name) is not None else metrics.get(name)
                for name in metric_names
            }
            numeric_by_property[property_id] = {
                name: Decimal(str(value)) for name, value in values.items()
                if value is not None
            }
            entries.append({
                "property_id": property_id,
                "label": labels[property_id],
                "result": result,
            })
        differences = []
        for left_id, right_id in combinations(numeric_by_property, 2):
            left_metrics = numeric_by_property[left_id]
            right_metrics = numeric_by_property[right_id]
            differences.append({
                "left_property_id": left_id,
                "left_label": labels[left_id],
                "right_property_id": right_id,
                "right_label": labels[right_id],
                "left_minus_right": {
                    name: left_metrics[name] - right_metrics[name]
                    for name in sorted(set(left_metrics) & set(right_metrics))
                },
            })
        strategy_comparisons.append({
            "strategy": strategy,
            "entries": entries,
            "precomputed_pairwise_differences": differences,
        })
    return _chat_safe({
        "scenario": scenario,
        "selected_property_order": property_ids,
        "property_labels": labels,
        "score_comparisons": score_comparisons,
        "strategy_comparisons": strategy_comparisons,
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
    if len(property_ids) >= 2:
        tools["selected_property_comparison"] = _compare(
            session, property_ids, Scenario.EXPECTED.value,
        )
    encoded_size = len(json.dumps({"c": context, "t": tools, "m": messages}, default=str))
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
