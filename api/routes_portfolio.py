"""Portfolio-level endpoints: batches, flags, saved views, rankings, dashboard,
problems, changes, assumption sets, exports, realized deals (WP-11, spec §16)."""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from auth.dependencies import User, current_user, write_user
from common.errors import AcqError, ErrorCode
from common.serializers import json_safe
from contracts import (
    AssumptionSet,
    BatchEstimate,
    FilterClause,
    FlagResolution,
    FlagType,
    SavedViewCreate,
    SavedViewRecord,
)
from db import models as dbm
from exports import stream_properties
from flags import is_gating
from jobs.postgres import PostgresJobQueue

from . import analysis as analysis_store
from . import serializers
from .deps import enqueue, get_queue, get_session
from .filters import translate_filters
from .serializers import dump

router = APIRouter(prefix="/api", tags=["portfolio"])

PRICE_PER_1K_TOKENS = Decimal("0.003")
DEFAULT_EXPORT_COLUMNS = ["id", "address_line1", "city", "state", "zip5", "pipeline_status", "tags"]


def _batch_or_404(session: Session, batch_id: UUID) -> dbm.Batch:
    batch = session.get(dbm.Batch, batch_id)
    if batch is None:
        raise AcqError(ErrorCode.NOT_FOUND, "batch not found")
    return batch


def _batch_payload(batch: dbm.Batch) -> dict:
    return json_safe({"id": str(batch.id), "name": batch.name, "status": batch.status,
                      "total": batch.total_count, "completed": batch.completed_count,
                      "failed": batch.failed_count, "estimated_cost_usd": batch.estimated_cost_usd,
                      "actual_cost_usd": batch.actual_cost_usd,
                      "awaiting_confirmation": bool(batch.awaiting_confirmation)})


@router.get("/batches/{batch_id}")
def batch_status(batch_id: UUID, session: Session = Depends(get_session),
                 user: User = Depends(current_user)) -> dict:
    return _batch_payload(_batch_or_404(session, batch_id))


@router.post("/batches/{batch_id}/estimate")
def batch_estimate(batch_id: UUID, session: Session = Depends(get_session),
                   user: User = Depends(current_user)) -> dict:
    """Pre-flight estimate (spec §19): token counts and dollar estimate before
    anything hits the extraction API; the batch waits for confirmation."""
    batch = _batch_or_404(session, batch_id)
    reports = session.query(dbm.Report).filter(dbm.Report.batch_id == batch_id).all()
    report_ids = [report.id for report in reports]
    units = (session.query(dbm.ExtractionUnit)
             .filter(dbm.ExtractionUnit.report_id.in_(report_ids)).all()) if report_ids else []
    total_tokens = sum(unit.token_estimate or 0 for unit in units)
    estimated = (Decimal(total_tokens) / Decimal("1000") * PRICE_PER_1K_TOKENS).quantize(Decimal("0.01"))
    batch.estimated_cost_usd = estimated
    batch.awaiting_confirmation = True
    batch.status = "awaiting_confirmation"
    session.flush()
    return dump(BatchEstimate(batch_id=batch_id, report_count=len(reports),
                              total_tokens=total_tokens, estimated_cost_usd=estimated,
                              awaiting_confirmation=True))


@router.post("/batches/{batch_id}/start")
def batch_start(batch_id: UUID, session: Session = Depends(get_session),
                queue: PostgresJobQueue = Depends(get_queue), user: User = Depends(write_user)) -> dict:
    batch = _batch_or_404(session, batch_id)
    batch.awaiting_confirmation = False
    batch.status = "running"
    reports = (session.query(dbm.Report)
               .filter(dbm.Report.batch_id == batch_id, dbm.Report.status == "uploaded").all())
    for report in reports:
        enqueue(session, queue, "ingest_document", {"report_id": str(report.id)}, f"ingest:{report.id}")
    session.flush()
    return _batch_payload(batch)


@router.get("/flags")
def list_flags(status: str = Query(default="open"), property_id: UUID | None = Query(default=None),
               limit: int = Query(default=200), session: Session = Depends(get_session),
               user: User = Depends(current_user)) -> dict:
    limit = max(1, min(limit, 1000))
    query = session.query(dbm.Flag)
    if status != "all":
        query = query.filter(dbm.Flag.status == status)
    if property_id is not None:
        query = query.filter(dbm.Flag.property_id == property_id)
    rows = query.order_by(dbm.Flag.financial_impact_usd.desc()).limit(limit).all()
    return {"items": dump([serializers.flag_record(row) for row in rows])}


@router.post("/flags/{flag_id}/resolve")
def resolve_flag(flag_id: UUID, body: FlagResolution, session: Session = Depends(get_session),
                 queue: PostgresJobQueue = Depends(get_queue), user: User = Depends(write_user)) -> dict:
    row = session.get(dbm.Flag, flag_id)
    if row is None:
        raise AcqError(ErrorCode.NOT_FOUND, "flag not found")
    if row.status == "resolved":
        raise AcqError(ErrorCode.CONFLICT, "flag is already resolved", {"flag_id": str(flag_id)})
    row.status = "resolved"
    row.resolution = body.resolution
    row.note = body.note
    row.resolved_value = body.resolved_value
    row.resolved_at = datetime.now(timezone.utc)
    session.flush()
    enqueue(session, queue, "recompute_property",
            {"property_id": str(row.property_id), "reason": f"flag_resolved:{flag_id}"},
            f"recompute:{row.property_id}")
    return {"flag": dump(serializers.flag_record(row)), "recompute_enqueued": True}


@router.get("/saved-views")
def list_saved_views(session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    rows = session.query(dbm.SavedView).order_by(dbm.SavedView.created_at.desc()).all()
    return {"items": dump([_saved_view_record(row) for row in rows])}


def _saved_view_record(row: dbm.SavedView) -> SavedViewRecord:
    return SavedViewRecord(id=row.id, name=row.name or "",
                           filters=[FilterClause(**clause) for clause in (row.filters or [])],
                           columns=dict(row.columns or {}), created_at=row.created_at)


@router.post("/saved-views")
def create_saved_view(body: SavedViewCreate, session: Session = Depends(get_session),
                      user: User = Depends(write_user)) -> dict:
    translate_filters(body.filters)  # reject views that cannot be executed
    row = dbm.SavedView(id=uuid4(), name=body.name,
                        filters=[clause.model_dump(mode="json") for clause in body.filters],
                        columns=body.columns)
    session.add(row)
    session.flush()
    return dump(_saved_view_record(row))


@router.delete("/saved-views/{view_id}")
def delete_saved_view(view_id: UUID, session: Session = Depends(get_session),
                      user: User = Depends(write_user)) -> dict:
    row = session.get(dbm.SavedView, view_id)
    if row is None:
        raise AcqError(ErrorCode.NOT_FOUND, "saved view not found")
    session.delete(row)
    return {"deleted": str(view_id)}


@router.get("/rankings")
def rankings(scope_type: str = Query(default="portfolio"), session: Session = Depends(get_session),
             user: User = Depends(current_user)) -> dict:
    rows = (session.query(dbm.Ranking)
            .filter(dbm.Ranking.scope_type == scope_type)
            .order_by(dbm.Ranking.ranked_at.desc())
            .all())
    if not rows:
        return {"items": [], "ranked_at": None}
    latest_at = rows[0].ranked_at
    entries = [serializers.ranking_entry(row) for row in rows if row.ranked_at == latest_at]
    entries.sort(key=lambda entry: entry.rank)
    return {"items": dump(entries), "ranked_at": latest_at.isoformat() if latest_at else None}


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    properties = session.query(dbm.Property).filter(dbm.Property.merged_into_id.is_(None)).all()
    by_status = Counter(row.pipeline_status or "new" for row in properties)
    valued_ids = {row.property_id for row in
                  session.query(dbm.Valuation).filter(dbm.Valuation.is_active.is_(True)).all()}
    open_flags = session.query(dbm.Flag).filter(dbm.Flag.status == "open").count()
    failed_reports = session.query(dbm.Report).filter(dbm.Report.status == "failed").count()
    return {"total_properties": len(properties), "by_status": dict(by_status),
            "open_flags": open_flags, "failed_reports": failed_reports,
            "missing_valuation_count": sum(1 for row in properties if row.id not in valued_ids),
            "watchlisted": sum(1 for row in properties if row.is_watchlisted)}


@router.get("/changes")
def changes(limit: int = Query(default=100), session: Session = Depends(get_session),
            user: User = Depends(current_user)) -> dict:
    limit = max(1, min(limit, 1000))
    rows = session.query(dbm.ChangeEvent).order_by(dbm.ChangeEvent.detected_at.desc()).limit(limit).all()
    return {"items": [json_safe({"id": str(row.id), "property_id": str(row.property_id),
                                 "change_type": row.change_type, "field_path": row.field_path,
                                 "old_value": row.old_value, "new_value": row.new_value,
                                 "score_delta": row.score_delta,
                                 "detected_at": row.detected_at.isoformat() if row.detected_at else None})
                      for row in rows]}


@router.get("/problems")
def problems(session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    open_rows = session.query(dbm.Flag).filter(dbm.Flag.status == "open").all()
    gating = [serializers.flag_record(row) for row in open_rows
              if row.flag_type in FlagType.__members__.values() and is_gating(FlagType(row.flag_type))]
    failed = session.query(dbm.Report).filter(dbm.Report.status == "failed").all()
    return {"gating_flags": dump(gating),
            "failed_reports": [{"id": str(row.id), "batch_id": str(row.batch_id) if row.batch_id else None,
                                "failure_reason": row.failure_reason, "file_path": row.file_path}
                               for row in failed]}


@router.get("/assumption-sets")
def list_assumption_sets(session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    rows = session.query(dbm.AssumptionSet).order_by(dbm.AssumptionSet.version.desc()).all()
    return {"items": [json_safe({"id": str(row.id), "name": row.name, "version": row.version,
                                 "is_default": bool(row.is_default),
                                 "effective_from": row.effective_from.isoformat() if row.effective_from else None,
                                 "params": row.params})
                      for row in rows]}


def _validate_params(name: str, params: dict) -> AssumptionSet:
    try:
        return AssumptionSet(id=uuid4(), version=1, name=name, **params)
    except Exception as exc:
        raise AcqError(ErrorCode.INVALID_INPUT, "invalid assumption set params",
                       {"errors": [str(exc)]})


@router.post("/assumption-sets")
def create_assumption_set(body: dict = Body(...), session: Session = Depends(get_session),
                          user: User = Depends(write_user)) -> dict:
    name, params = body.get("name"), body.get("params") or {}
    if not name:
        raise AcqError(ErrorCode.INVALID_INPUT, "name is required")
    _validate_params(name, params)
    row = dbm.AssumptionSet(id=uuid4(), name=name, params=params,
                            is_default=bool(body.get("is_default", False)), version=1,
                            effective_from=datetime.now(timezone.utc).date())
    session.add(row)
    session.flush()
    return {"id": str(row.id), "name": row.name, "version": row.version}


@router.post("/assumption-sets/preview")
def preview_assumption_set(body: dict = Body(...), session: Session = Depends(get_session),
                           user: User = Depends(current_user)) -> dict:
    """Validate candidate params and, when property_id is given, recompute
    underwriting with them so the impact is visible before saving."""
    assumptions = _validate_params(body.get("name") or "preview", body.get("params") or {})
    property_id = body.get("property_id")
    if not property_id:
        return {"valid": True, "underwriting": None}
    property_id = UUID(str(property_id))
    normalized = analysis_store.load_normalized(session, property_id)
    if normalized is None:
        return {"valid": True, "underwriting": None}
    from finance import underwrite  # lazy

    return {"valid": True, "underwriting": dump(underwrite(normalized, assumptions))}


@router.get("/exports/csv")
def export_csv(filters: str | None = Query(default=None), columns: str | None = Query(default=None),
               session: Session = Depends(get_session), user: User = Depends(current_user)):
    from .routes_properties import _parse_filter_param  # shared grammar parsing

    criteria = translate_filters(_parse_filter_param(filters))
    rows = (session.query(dbm.Property)
            .filter(dbm.Property.merged_into_id.is_(None), *criteria)
            .order_by(dbm.Property.created_at.desc(), dbm.Property.id.desc())
            .all())
    selected = [column.strip() for column in columns.split(",")] if columns else DEFAULT_EXPORT_COLUMNS
    records = (json_safe({column: getattr(row, column, None) for column in selected}) for row in rows)
    stream = stream_properties(records, selected)
    return StreamingResponse(stream, media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=properties.csv"})


@router.post("/realized-deals")
def create_realized_deal(body: dict = Body(...), session: Session = Depends(get_session),
                         user: User = Depends(write_user)) -> dict:
    property_id = body.get("property_id")
    if not property_id:
        raise AcqError(ErrorCode.INVALID_INPUT, "property_id is required")
    if session.get(dbm.Property, UUID(str(property_id))) is None:
        raise AcqError(ErrorCode.NOT_FOUND, "property not found")

    def _money(key: str) -> Decimal | None:
        value = body.get(key)
        return Decimal(str(value)) if value is not None else None

    row = dbm.RealizedDeal(id=uuid4(), property_id=UUID(str(property_id)),
                           purchase_price=_money("purchase_price"), actual_repairs=_money("actual_repairs"),
                           actual_holding_days=body.get("actual_holding_days"),
                           sale_price=_money("sale_price"), actual_costs=_money("actual_costs"),
                           outcome=body.get("outcome"), notes=body.get("notes"))
    session.add(row)
    session.flush()
    return json_safe({"id": str(row.id), "property_id": str(row.property_id),
                      "purchase_price": row.purchase_price, "sale_price": row.sale_price,
                      "outcome": row.outcome})
