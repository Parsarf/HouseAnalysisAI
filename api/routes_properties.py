"""Property-scoped endpoints (WP-11, spec §16)."""
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from auth.dependencies import User, current_user, write_user
from common.errors import AcqError, ErrorCode
from common.settings import settings
from contracts import (
    AddressBlock,
    AnalysisPayload,
    ExtractedFactDraft,
    FilterClause,
    NormalizedProperty,
    NoteCreate,
    NoteRecord,
    OfferRequest,
    PropertyDetail,
    PropertyListPage,
    Scenario,
    SourceKind,
)
from db import models as dbm
from exports import deal_sheet_html, net_sheet_html, write_export
from jobs.postgres import PostgresJobQueue
from outreach import Draft, OutreachProviderClient, generate_draft, validate_draft

from . import analysis as analysis_store
from . import serializers
from .deps import enqueue, get_queue, get_session
from .filters import cursor_condition, parse_sort, translate_filters
from .pagination import decode_cursor, encode_cursor
from .routes_owner import owner_profile_payload
from .serializers import dump, money_envelope

router = APIRouter(prefix="/api/properties", tags=["properties"])

MAX_LIMIT = 500


def _not_found(message: str = "property not found") -> AcqError:
    return AcqError(ErrorCode.NOT_FOUND, message)


def _get_property(session: Session, property_id: UUID) -> dbm.Property:
    row = session.get(dbm.Property, property_id)
    if row is None:
        raise _not_found()
    return row


def _latest_scores(session: Session, property_ids: list[UUID]) -> dict[UUID, dbm.Score]:
    if not property_ids:
        return {}
    rows = (session.query(dbm.Score)
            .filter(dbm.Score.property_id.in_(property_ids))
            .order_by(dbm.Score.computed_at.desc())
            .all())
    latest: dict[UUID, dbm.Score] = {}
    for row in rows:
        if row.property_id is not None:
            latest.setdefault(row.property_id, row)
    return latest


def _latest_ranks(session: Session, property_ids: list[UUID]) -> dict[UUID, int]:
    if not property_ids:
        return {}
    rows = (session.query(dbm.Ranking)
            .filter(dbm.Ranking.property_id.in_(property_ids))
            .order_by(dbm.Ranking.ranked_at.desc())
            .all())
    latest: dict[UUID, int] = {}
    for row in rows:
        if row.property_id is not None and row.rank is not None:
            latest.setdefault(row.property_id, row.rank)
    return latest


def _open_flag_counts(session: Session, property_ids: list[UUID]) -> dict[UUID, int]:
    counts: dict[UUID, int] = {}
    if not property_ids:
        return counts
    rows = (session.query(dbm.Flag)
            .filter(dbm.Flag.property_id.in_(property_ids), dbm.Flag.status == "open")
            .all())
    for row in rows:
        counts[row.property_id] = counts.get(row.property_id, 0) + 1
    return counts


@router.get("")
def list_properties(filters: str | None = Query(default=None), sort: str | None = Query(default=None),
                    limit: int = Query(default=50), cursor: str | None = Query(default=None),
                    show_archived: bool = Query(default=False),
                    session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    limit = max(1, min(limit, MAX_LIMIT))
    clauses = _parse_filter_param(filters)
    criteria = translate_filters(clauses)
    sort_field, sort_column, descending = parse_sort(sort)
    query = session.query(dbm.Property).filter(dbm.Property.merged_into_id.is_(None), *criteria)
    if not show_archived:
        query = query.filter(dbm.Property.archived_at.is_(None))
    if cursor:
        sort_value, cursor_id = decode_cursor(cursor, sort_field)
        query = query.filter(cursor_condition(sort_column, descending, sort_value, cursor_id))
    order = [sort_column.desc() if descending else sort_column.asc(),
             dbm.Property.id.desc() if descending else dbm.Property.id.asc()]
    rows = query.order_by(*order).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    ids = [row.id for row in rows]
    scores = _latest_scores(session, ids)
    ranks = _latest_ranks(session, ids)
    flag_counts = _open_flag_counts(session, ids)
    items = [serializers.property_summary(row, score=scores[row.id].overall if row.id in scores else None,
                                          rank=ranks.get(row.id), open_flags=flag_counts.get(row.id, 0))
             for row in rows]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        sort_value = getattr(last, sort_field) if sort_field != "id" else last.id
        next_cursor = encode_cursor(sort_value, last.id)
    return dump(PropertyListPage(items=items, next_cursor=next_cursor))


def _parse_filter_param(raw: str | None) -> list[FilterClause]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
        return [FilterClause(**item) for item in payload]
    except AcqError:
        raise
    except Exception:
        raise AcqError(ErrorCode.INVALID_INPUT, "malformed filters parameter",
                       {"hint": "filters must be a JSON array of {field, op, value} clauses"})


# Static paths must be declared before /{property_id} so they are not captured.
@router.post("/merge")
def merge_properties(body: dict, session: Session = Depends(get_session),
                     user: User = Depends(write_user)) -> dict:
    source_id, target_id = UUID(str(body.get("source_id"))), UUID(str(body.get("target_id")))
    if source_id == target_id:
        raise AcqError(ErrorCode.INVALID_INPUT, "cannot merge a property into itself")
    source = _get_property(session, source_id)
    _get_property(session, target_id)
    source.merged_into_id = target_id
    return {"source_id": str(source_id), "merged_into_id": str(target_id)}


@router.post("/unmerge")
def unmerge_property(body: dict, session: Session = Depends(get_session),
                     user: User = Depends(write_user)) -> dict:
    source = _get_property(session, UUID(str(body.get("source_id"))))
    source.merged_into_id = None
    return {"source_id": str(source.id), "merged_into_id": None}


@router.post("/quick-add")
def quick_add(body: dict, session: Session = Depends(get_session),
              user: User = Depends(write_user)) -> dict:
    address = (body.get("address_line1") or "").strip()
    if not address:
        raise AcqError(ErrorCode.INVALID_INPUT, "address_line1 is required")
    row = dbm.Property(id=uuid4(), apn=body.get("apn"), address_line1=address,
                       city=body.get("city"), state=body.get("state"), zip5=body.get("zip5"),
                       pipeline_status="new", tags=list(body.get("tags") or []))
    session.add(row)
    session.flush()
    return dump(serializers.property_summary(row))


@router.get("/{property_id}")
def property_detail(property_id: UUID, session: Session = Depends(get_session),
                    user: User = Depends(current_user)) -> dict:
    row = _get_property(session, property_id)
    scores = _latest_scores(session, [property_id])
    ranks = _latest_ranks(session, [property_id])
    flag_counts = _open_flag_counts(session, [property_id])
    summary = serializers.property_summary(row, score=scores[property_id].overall if property_id in scores else None,
                                           rank=ranks.get(property_id), open_flags=flag_counts.get(property_id, 0))
    valuation_row = (session.query(dbm.Valuation)
                     .filter(dbm.Valuation.property_id == property_id, dbm.Valuation.is_active.is_(True))
                     .order_by(dbm.Valuation.as_of_date.desc())
                     .first())
    latest_valuation = None
    if valuation_row is not None:
        is_estimate = (valuation_row.valuation_type or "") in ("avm", "estimate", "zestimate")
        latest_valuation = money_envelope(serializers.tracked_money(
            valuation_row.value, confidence=float(valuation_row.confidence_reported or 1.0),
            source_kind=SourceKind.API if is_estimate else SourceKind.REPORT))
        if is_estimate and latest_valuation is not None:
            latest_valuation.is_estimated = True
    detail = PropertyDetail(**summary.model_dump(), lat=row.lat, lng=row.lng,
                            created_at=row.created_at, updated_at=row.updated_at,
                            latest_valuation=latest_valuation)
    return dump(detail)


@router.patch("/{property_id}")
def update_property(property_id: UUID, changes: dict, session: Session = Depends(get_session),
                    user: User = Depends(write_user)) -> dict:
    allowed = {"pipeline_status", "tags", "next_action", "next_action_date", "gut_rating", "is_watchlisted"}
    unknown = set(changes) - allowed
    if unknown:
        raise AcqError(ErrorCode.INVALID_INPUT, "unknown property fields", {"fields": sorted(unknown)})
    row = _get_property(session, property_id)
    for key, value in changes.items():
        setattr(row, key, value)
    session.flush()
    return dump(serializers.property_summary(row))


@router.post("/{property_id}/archive")
def archive_property(property_id: UUID, session: Session = Depends(get_session),
                     user: User = Depends(write_user)) -> dict:
    row = _get_property(session, property_id)
    if row.archived_at is None:
        row.archived_at = datetime.now(UTC)
        session.add(dbm.ChangeEvent(
            property_id=row.id, change_type="archived", field_path="archived_at",
            old_value={"archived_at": None}, new_value={"archived_at": row.archived_at.isoformat()},
        ))
    session.flush()
    return {"property_id": str(row.id), "archived_at": row.archived_at.isoformat()}


@router.post("/{property_id}/restore")
def restore_property(property_id: UUID, session: Session = Depends(get_session),
                     user: User = Depends(write_user)) -> dict:
    row = _get_property(session, property_id)
    previous = row.archived_at
    row.archived_at = None
    if previous is not None:
        session.add(dbm.ChangeEvent(
            property_id=row.id, change_type="restored", field_path="archived_at",
            old_value={"archived_at": previous.isoformat()}, new_value={"archived_at": None},
        ))
    session.flush()
    return {"property_id": str(row.id), "archived_at": None}


@router.post("/{property_id}/outreach-draft")
def outreach_draft(property_id: UUID, body: dict, session: Session = Depends(get_session),
                   user: User = Depends(write_user)) -> dict:
    property_row = _get_property(session, property_id)
    requested = body.get("offer_price")
    query = session.query(dbm.OfferScenario).filter(dbm.OfferScenario.property_id == property_id)
    if requested is not None:
        query = query.filter(dbm.OfferScenario.offer_price == Decimal(str(requested)))
    offer = query.order_by(
        dbm.OfferScenario.is_short_sale.asc(), dbm.OfferScenario.profit.desc().nullslast(),
        dbm.OfferScenario.offer_price.desc(),
    ).first()
    if offer is None:
        raise AcqError(
            ErrorCode.INVALID_INPUT,
            "offer_price must be an existing calculated offer-grid point"
            if requested is not None else "no calculated offer is available",
        )
    normalized = analysis_store.load_normalized(session, property_id)
    owner_profile = owner_profile_payload(session, property_id)
    context = {
        "property_id": str(property_id),
        "address": property_row.address_line1,
        "city": property_row.city, "state": property_row.state, "zip5": property_row.zip5,
        "property_type": property_row.property_type,
        "beds": property_row.beds, "baths": property_row.baths,
        "offer_price": offer.offer_price, "scenario": offer.scenario,
        "buyer_basis": offer.buyer_basis, "closing_costs": offer.closing_costs,
        "owner_names": normalized.ownership.owner_names if normalized else [],
    }
    try:
        draft = generate_draft(
            OutreachProviderClient(), context,
            prior_draft=body.get("prior_draft"), instruction=body.get("instruction"),
        )
    except ValueError as exc:
        raise AcqError(ErrorCode.INVALID_INPUT, str(exc)) from exc
    note = dbm.PropertyNote(
        property_id=property_id,
        body="[outreach-draft] " + json.dumps({
            "subject": draft.subject, "body": draft.body,
            "offer_price": str(offer.offer_price), "status": "draft",
        }),
    )
    session.add(note)
    session.flush()
    return dump({
        "draft_id": note.id, "subject": draft.subject, "body": draft.body,
        "offer_price": offer.offer_price,
        "recipients": [contact for contact in owner_profile["contacts"] if contact["kind"] == "email"],
        "mailing_addresses": [owner["mailing_address"] for owner in owner_profile["owners"]
                              if owner["mailing_address"]],
        "recipient_selected": None,
        "disclosure": settings.outreach_disclosure,
    })


@router.patch("/{property_id}/outreach-drafts/{draft_id}")
def update_outreach_draft(
    property_id: UUID, draft_id: UUID, body: dict,
    session: Session = Depends(get_session), user: User = Depends(write_user),
) -> dict:
    property_row = _get_property(session, property_id)
    note = session.get(dbm.PropertyNote, draft_id)
    prefix = "[outreach-draft] "
    if note is None or note.property_id != property_id or not note.body.startswith(prefix):
        raise AcqError(ErrorCode.NOT_FOUND, "outreach draft not found")
    try:
        stored = json.loads(note.body.removeprefix(prefix))
        offer_price = Decimal(str(stored["offer_price"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AcqError(ErrorCode.INTERNAL, "stored outreach draft is invalid") from exc
    subject = str(body.get("subject") or "").strip()
    draft_body = str(body.get("body") or "").strip()
    if not subject or not draft_body:
        raise AcqError(ErrorCode.INVALID_INPUT, "subject and body are required")
    context = {
        "address": property_row.address_line1, "city": property_row.city,
        "state": property_row.state, "zip5": property_row.zip5,
        "beds": property_row.beds, "baths": property_row.baths,
        "offer_price": offer_price,
    }
    draft = Draft(subject, draft_body)
    if not validate_draft(draft, context):
        raise AcqError(
            ErrorCode.INVALID_INPUT,
            "draft contains prohibited distress language or an ungrounded number",
        )
    status = str(body.get("status") or "draft")
    if status not in {"draft", "sent"}:
        raise AcqError(ErrorCode.INVALID_INPUT, "draft status must be draft or sent")
    recipient = str(body.get("recipient") or "").strip() or None
    if status == "sent":
        emails = {
            str(contact["value"]) for contact in owner_profile_payload(session, property_id)["contacts"]
            if contact["kind"] == "email"
        }
        if recipient not in emails:
            raise AcqError(ErrorCode.INVALID_INPUT, "select a current recipient before sending")
    stored.update({
        "subject": subject, "body": draft_body, "status": status,
        "recipient": recipient,
        "sent_at": datetime.now(UTC).isoformat() if status == "sent" else stored.get("sent_at"),
    })
    note.body = prefix + json.dumps(stored)
    session.flush()
    return dump({"draft_id": note.id, **stored})


@router.get("/{property_id}/analysis")
def property_analysis(property_id: UUID, scenario: Scenario = Query(default=Scenario.EXPECTED),
                      session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    row = _get_property(session, property_id)
    if row.merged_into_id is not None:
        raise AcqError(ErrorCode.CONFLICT, "property was merged into another record",
                       {"merged_into_id": str(row.merged_into_id)})
    normalized = analysis_store.load_normalized(session, property_id)
    underwriting = analysis_store.load_underwriting(session, property_id, normalized)
    owner_profile = owner_profile_payload(session, property_id)
    expected_equity = None
    if underwriting is not None and Scenario.EXPECTED in underwriting.equity:
        expected_equity = underwriting.equity[Scenario.EXPECTED].adjusted
    owner_lien_total = Decimal(str(owner_profile.get("owner_lien_total") or 0))
    owner_profile["equity_if_owner_liens_attach"] = (
        expected_equity - owner_lien_total if expected_equity is not None else None
    )
    owner_profile["owner_liens_included_in_underwriting"] = False
    payload = AnalysisPayload(
        property_id=property_id, scenario=scenario, normalized=normalized, underwriting=underwriting,
        strategies=analysis_store.load_strategies(session, property_id),
        offers=analysis_store.load_offers(session, property_id, scenario, underwriting),
        scores=analysis_store.load_scores(session, property_id),
        flags=analysis_store.load_flags(session, property_id),
        timeline=analysis_store.load_timeline(session, property_id),
        owner_profile=owner_profile)
    return dump(payload)


@router.get("/{property_id}/evidence/{field_path:path}")
def evidence(property_id: UUID, field_path: str, session: Session = Depends(get_session),
             user: User = Depends(current_user)) -> dict:
    _get_property(session, property_id)
    facts = (session.query(dbm.ExtractedFact)
             .filter(dbm.ExtractedFact.property_id == property_id,
                     dbm.ExtractedFact.field_path == field_path,
                     dbm.ExtractedFact.is_active.is_(True))
             .order_by(dbm.ExtractedFact.created_at.desc())
             .all())
    resolution = (session.query(dbm.FieldResolution)
                  .filter(dbm.FieldResolution.property_id == property_id,
                          dbm.FieldResolution.field_path == field_path)
                  .first())
    candidates = [{
        "fact_id": str(fact.id), "report_id": str(fact.report_id) if fact.report_id else None,
        "value_raw": fact.value_raw,
        "value_parsed": str(fact.value_parsed) if fact.value_parsed is not None else None,
        "value_text": fact.value_text,
        "value_date": fact.value_date.isoformat() if fact.value_date else None,
        "page_number": fact.page_number, "snippet": fact.snippet,
        "extraction_confidence": float(fact.extraction_confidence),
        "source_kind": fact.source_kind, "is_winner": resolution is not None and resolution.winning_fact_id == fact.id,
    } for fact in facts]
    return {
        "field_path": field_path,
        "resolution": None if resolution is None else {
            "method": resolution.method,
            "score": str(resolution.score) if resolution.score is not None else None,
            "has_conflict": resolution.has_conflict,
            "verification_state": resolution.verification_state,
            "winning_fact_id": str(resolution.winning_fact_id) if resolution.winning_fact_id else None,
        },
        "candidates": candidates,
    }


@router.get("/{property_id}/timeline")
def timeline(property_id: UUID, session: Session = Depends(get_session),
             user: User = Depends(current_user)) -> dict:
    _get_property(session, property_id)
    return {"property_id": str(property_id),
            "timeline": dump(analysis_store.load_timeline(session, property_id))}


@router.get("/{property_id}/reports")
def property_reports(property_id: UUID, session: Session = Depends(get_session),
                     user: User = Depends(current_user)) -> dict:
    _get_property(session, property_id)
    rows = (session.query(dbm.Report)
            .filter(dbm.Report.property_id == property_id)
            .order_by(dbm.Report.created_at.desc())
            .all())
    return {"items": [{
        "id": str(row.id), "report_type": row.report_type, "vendor": row.vendor,
        "generated_date": row.generated_date.isoformat() if row.generated_date else None,
        "status": row.status, "failure_reason": row.failure_reason, "page_count": row.page_count,
        "ocr_applied": bool(row.ocr_applied), "created_at": row.created_at.isoformat() if row.created_at else None,
    } for row in rows]}


@router.post("/{property_id}/offers")
def create_offer(property_id: UUID, body: OfferRequest, session: Session = Depends(get_session),
                 user: User = Depends(current_user)) -> dict:
    _get_property(session, property_id)
    normalized = analysis_store.load_normalized(session, property_id)
    underwriting = analysis_store.load_underwriting(session, property_id, normalized)
    if underwriting is None or underwriting.status != "ok":
        raise AcqError(ErrorCode.INVALID_INPUT, "no underwriting available for this property",
                       {"property_id": str(property_id)})
    assumptions = analysis_store.load_assumption_set(session)
    if assumptions is None:
        raise AcqError(ErrorCode.INVALID_INPUT, "no assumption set available")
    from strategies import offer_point  # lazy: strategies sits downstream of finance

    point = offer_point(underwriting, assumptions, body.offer_price, body.scenario, label=body.label)
    return dump(point)


@router.post("/{property_id}/recompute")
def recompute(property_id: UUID, body: dict | None = None, session: Session = Depends(get_session),
              queue: PostgresJobQueue = Depends(get_queue), user: User = Depends(write_user)) -> dict:
    property_row = _get_property(session, property_id)
    if property_row.archived_at is not None:
        raise AcqError(ErrorCode.INVALID_INPUT, "restore this property before recomputing it")
    job_id = enqueue(session, queue, "recompute_property",
                     {"property_id": str(property_id), "reason": (body or {}).get("reason") or "manual"},
                     f"recompute:{property_id}")
    return {"enqueued": True, "job_id": str(job_id)}


@router.post("/{property_id}/facts")
def add_fact(property_id: UUID, body: ExtractedFactDraft, session: Session = Depends(get_session),
             queue: PostgresJobQueue = Depends(get_queue), user: User = Depends(write_user)) -> dict:
    _get_property(session, property_id)
    fact = dbm.ExtractedFact(
        id=uuid4(), property_id=property_id, report_id=body.report_id,
        extraction_unit_id=body.extraction_unit_id, entity_type=body.entity_type.value,
        entity_local_id=body.entity_local_id, field_path=body.field_path,
        value_raw=body.value_raw, value_parsed=body.value_parsed, value_text=body.value_text,
        value_date=body.value_date, value_bool=body.value_bool, unit=body.unit,
        as_of_date=body.as_of_date, page_number=body.page_number, snippet=body.snippet,
        extraction_confidence=body.extraction_confidence,
        null_reason=body.null_reason.value if body.null_reason else None,
        source_kind=SourceKind.HUMAN.value, is_active=True)
    session.add(fact)
    session.flush()
    enqueue(session, queue, "recompute_property",
            {"property_id": str(property_id), "reason": "human_fact"}, f"recompute:{property_id}")
    return {"fact_id": str(fact.id)}


@router.get("/{property_id}/notes")
def list_notes(property_id: UUID, session: Session = Depends(get_session),
               user: User = Depends(current_user)) -> dict:
    _get_property(session, property_id)
    rows = (session.query(dbm.PropertyNote)
            .filter(dbm.PropertyNote.property_id == property_id)
            .order_by(dbm.PropertyNote.created_at.desc())
            .all())
    return {"items": dump([NoteRecord(id=row.id, property_id=property_id, body=row.body,
                                      created_at=row.created_at) for row in rows])}


@router.post("/{property_id}/notes")
def create_note(property_id: UUID, body: NoteCreate, session: Session = Depends(get_session),
                user: User = Depends(write_user)) -> dict:
    _get_property(session, property_id)
    row = dbm.PropertyNote(id=uuid4(), property_id=property_id, body=body.body)
    session.add(row)
    session.flush()
    return dump(NoteRecord(id=row.id, property_id=property_id, body=row.body, created_at=row.created_at))


def _normalized_or_stub(session: Session, row: dbm.Property) -> NormalizedProperty:
    normalized = analysis_store.load_normalized(session, row.id)
    if normalized is not None:
        return normalized
    return NormalizedProperty(
        property_id=row.id, apn=row.apn,
        address=AddressBlock(line1=row.address_line1, city=row.city, state=row.state, zip5=row.zip5),
        resolution_version="none")


@router.post("/{property_id}/exports/deal-sheet", response_class=HTMLResponse)
def deal_sheet(property_id: UUID, session: Session = Depends(get_session),
               user: User = Depends(current_user)) -> str:
    row = _get_property(session, property_id)
    normalized = _normalized_or_stub(session, row)
    underwriting = analysis_store.load_underwriting(session, property_id, normalized)
    strategies = analysis_store.load_strategies(session, property_id)
    scores = analysis_store.load_scores(session, property_id)
    assumptions = analysis_store.load_assumption_set(session)
    return deal_sheet_html(normalized, underwriting=underwriting, strategies=strategies, scores=scores,
                           assumptions=assumptions)


@router.post("/{property_id}/exports/deal-sheet.pdf")
def deal_sheet_pdf(property_id: UUID, session: Session = Depends(get_session),
                   user: User = Depends(current_user)) -> FileResponse:
    """Render the same deal-sheet HTML as a stored PDF deliverable."""
    row = _get_property(session, property_id)
    normalized = _normalized_or_stub(session, row)
    html = deal_sheet_html(normalized, underwriting=analysis_store.load_underwriting(session, property_id, normalized),
                           strategies=analysis_store.load_strategies(session, property_id),
                           scores=analysis_store.load_scores(session, property_id))
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise AcqError(ErrorCode.INTERNAL, "PDF rendering is not installed") from exc
    path = write_export(settings.document_root, property_id, "deal-sheet.pdf", "")
    HTML(string=html, base_url=str(path.parent)).write_pdf(str(path))
    return FileResponse(path, media_type="application/pdf", filename="deal-sheet.pdf")


@router.post("/{property_id}/exports/net-sheet.pdf")
def net_sheet_pdf(property_id: UUID, body: OfferRequest, session: Session = Depends(get_session),
                  user: User = Depends(current_user)) -> FileResponse:
    row = _get_property(session, property_id)
    normalized = _normalized_or_stub(session, row)
    underwriting = analysis_store.load_underwriting(session, property_id, normalized)
    assumptions = analysis_store.load_assumption_set(session)
    if assumptions is None:
        raise AcqError(ErrorCode.INVALID_INPUT, "no assumption set available")
    if underwriting is None or assumptions is None:
        raise AcqError(ErrorCode.INVALID_INPUT, "no underwriting available for this property")
    from strategies import offer_point
    html = net_sheet_html(normalized, offer_point(underwriting, assumptions, body.offer_price,
                                                  body.scenario, label=body.label), underwriting)
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise AcqError(ErrorCode.INTERNAL, "PDF rendering is not installed") from exc
    path = write_export(settings.document_root, property_id, "net-sheet.pdf", "")
    HTML(string=html, base_url=str(path.parent)).write_pdf(str(path))
    return FileResponse(path, media_type="application/pdf", filename="net-sheet.pdf")


@router.post("/{property_id}/exports/net-sheet", response_class=HTMLResponse)
def net_sheet(property_id: UUID, body: OfferRequest, session: Session = Depends(get_session),
              user: User = Depends(current_user)) -> str:
    row = _get_property(session, property_id)
    normalized = _normalized_or_stub(session, row)
    underwriting = analysis_store.load_underwriting(session, property_id, normalized)
    if underwriting is None or underwriting.status != "ok":
        raise AcqError(ErrorCode.INVALID_INPUT, "no underwriting available for this property",
                       {"property_id": str(property_id)})
    assumptions = analysis_store.load_assumption_set(session)
    if assumptions is None:
        raise AcqError(ErrorCode.INVALID_INPUT, "no assumption set available")
    from strategies import offer_point  # lazy

    point = offer_point(underwriting, assumptions, body.offer_price, body.scenario, label=body.label)
    return net_sheet_html(normalized, point, underwriting=underwriting)
