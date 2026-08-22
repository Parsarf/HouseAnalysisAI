"""Portfolio-level endpoints: batches, flags, saved views, rankings, dashboard,
problems, changes, assumption sets, exports, realized deals (WP-11, spec §16)."""
import logging
import tempfile
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from auth.dependencies import User, current_user, write_user
from common.errors import AcqError, ErrorCode
from common.serializers import json_safe
from common.settings import settings
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
from exports import full_export, stream_properties
from flags import is_gating
from jobs.postgres import PostgresJobQueue

from . import analysis as analysis_store
from . import serializers
from .deps import enqueue, get_queue, get_session
from .filters import translate_filters
from .serializers import dump

router = APIRouter(prefix="/api", tags=["portfolio"])
log = logging.getLogger(__name__)

PRICE_PER_1K_TOKENS = Decimal("0.003")
DEFAULT_EXPORT_COLUMNS = ["id", "address_line1", "city", "state", "zip5", "pipeline_status", "tags"]
_BATCH_STATUS_LOG_STATE: dict[UUID, tuple] = {}
_BATCH_STATUS_LOG_LOCK = Lock()


def _batch_status_changed(batch_id: UUID, state: tuple) -> bool:
    with _BATCH_STATUS_LOG_LOCK:
        if _BATCH_STATUS_LOG_STATE.get(batch_id) == state:
            return False
        if len(_BATCH_STATUS_LOG_STATE) >= 4096:
            _BATCH_STATUS_LOG_STATE.clear()
        _BATCH_STATUS_LOG_STATE[batch_id] = state
        return True


def _batch_or_404(session: Session, batch_id: UUID) -> dbm.Batch:
    batch = session.get(dbm.Batch, batch_id)
    if batch is None:
        raise AcqError(ErrorCode.NOT_FOUND, "batch not found")
    return batch


def _batch_payload(batch: dbm.Batch, session: Session) -> dict:
    reports = session.query(dbm.Report).filter(dbm.Report.batch_id == batch.id).all()
    property_ids = sorted(
        {report.property_id for report in reports if report.property_id is not None},
        key=str,
    )
    properties = (
        session.query(dbm.Property).filter(dbm.Property.id.in_(property_ids)).all()
        if property_ids else []
    )
    reports_by_property: dict[UUID, list[str]] = {}
    for report in reports:
        if report.property_id is not None:
            reports_by_property.setdefault(report.property_id, []).append(str(report.id))
    results = [{
        "property_id": str(row.id),
        "report_ids": reports_by_property.get(row.id, []),
        "address_line1": row.address_line1,
        "city": row.city,
        "state": row.state,
        "zip5": row.zip5,
        "apn": row.apn,
    } for row in properties]
    unresolved = []
    for report in reports:
        if not (report.property_id is None
                and report.failure_reason == ErrorCode.IDENTITY_UNRESOLVED.value):
            continue
        extraction = (session.query(dbm.ReportExtraction)
                      .filter(dbm.ReportExtraction.report_id == report.id).first())
        identity = None
        if extraction is not None:
            normalized = extraction.normalized_json if isinstance(extraction.normalized_json, dict) else {}
            source = normalized.get("source") or extraction.raw_json or {}
            identity = source.get("property_identity") if isinstance(source, dict) else None
        item = {
            "report_id": str(report.id),
            "reason": report.failure_reason or ErrorCode.IDENTITY_UNRESOLVED.value,
        }
        if identity is not None:
            item["identity"] = identity
        unresolved.append(item)
    return json_safe({"id": str(batch.id), "name": batch.name, "status": batch.status,
                      "total": batch.total_count, "completed": batch.completed_count,
                      "failed": batch.failed_count, "estimated_cost_usd": batch.estimated_cost_usd,
                      "actual_cost_usd": batch.actual_cost_usd,
                      "awaiting_confirmation": bool(batch.awaiting_confirmation),
                      "property_ids": [str(value) for value in property_ids],
                      "results": results, "unresolved_reports": unresolved})


@router.get("/batches/{batch_id}")
def batch_status(batch_id: UUID, session: Session = Depends(get_session),
                 user: User = Depends(current_user)) -> dict:
    batch = _batch_or_404(session, batch_id)
    payload = _batch_payload(batch, session)
    report_count = session.query(dbm.Report).filter(dbm.Report.batch_id == batch_id).count()
    state = (batch.status, batch.total_count, batch.completed_count, batch.failed_count,
             report_count, tuple(payload["property_ids"]),
             tuple(item["report_id"] for item in payload["unresolved_reports"]))
    if _batch_status_changed(batch_id, state):
        log.info("batch status read", extra={
            "event": "batch_status_returned",
            "batch_id": batch_id,
            "batch_status_after": batch.status,
            "total_count": batch.total_count,
            "completed_count": batch.completed_count,
            "failed_count": batch.failed_count,
            "report_count": report_count,
            "property_ids": payload["property_ids"],
            "unresolved_report_count": len(payload["unresolved_reports"]),
            "transaction_status": "database_read",
        })
    return payload


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
    unit_report_ids = {unit.report_id for unit in units}
    pending = [report for report in reports
               if report.status in ("uploaded", "ocr_pending") and report.id not in unit_report_ids]
    if pending:
        raise AcqError(ErrorCode.CONFLICT, "batch ingestion is still running",
                       {"pending_reports": len(pending)})
    if not units:
        raise AcqError(ErrorCode.CONFLICT, "batch has no extraction units",
                       {"failed_reports": sum(report.status == "failed" for report in reports)})
    total_tokens = sum(unit.token_estimate or 0 for unit in units)
    estimated = (Decimal(total_tokens) / Decimal(1000) * PRICE_PER_1K_TOKENS).quantize(Decimal("0.01"))
    batch.estimated_cost_usd = estimated
    batch.awaiting_confirmation = True
    batch.status = "awaiting_confirmation"
    session.flush()
    log.info("batch extraction estimate completed", extra={
        "event": "batch_estimated",
        "batch_id": batch_id,
        "eligible_units": len(units),
        "report_count": len(reports),
        "estimated_cost_usd": estimated,
        "batch_status_after": batch.status,
    })
    return dump(BatchEstimate(batch_id=batch_id, report_count=len(reports),
                              total_tokens=total_tokens, estimated_cost_usd=estimated,
                              awaiting_confirmation=True))


@router.post("/batches/{batch_id}/start")
def batch_start(batch_id: UUID, session: Session = Depends(get_session),
                queue: PostgresJobQueue = Depends(get_queue), user: User = Depends(write_user)) -> dict:
    batch = _batch_or_404(session, batch_id)
    log.info("batch start received", extra={
        "event": "batch_start_received",
        "batch_id": batch_id,
    })
    if not batch.awaiting_confirmation:
        raise AcqError(ErrorCode.CONFLICT, "batch is not awaiting confirmation",
                       {"status": batch.status})
    reports = session.query(dbm.Report).filter(dbm.Report.batch_id == batch_id).all()
    report_ids = [report.id for report in reports]
    all_units = (session.query(dbm.ExtractionUnit)
                 .filter(dbm.ExtractionUnit.report_id.in_(report_ids)).all()) if report_ids else []
    units = [unit for unit in all_units if unit.status == "queued"]
    unit_statuses = Counter(unit.status or "unset" for unit in all_units)
    excluded_unit_statuses = Counter(
        unit.status or "unset" for unit in all_units if unit.status != "queued"
    )
    log.info("batch eligible extraction units", extra={
        "event": "extraction_units_eligible",
        "batch_id": batch_id,
        "eligible_units": len(units),
        "eligible_unit_count": len(units),
        "queued_jobs": 0,
        "unit_ids": [str(unit.id) for unit in units],
        "unit_statuses": dict(unit_statuses),
        "excluded_unit_statuses": dict(excluded_unit_statuses),
    })
    if not units:
        report_statuses = Counter(report.status or "unset" for report in reports)
        log.warning("batch start rejected; no queued extraction units", extra={
            "event": "batch_start_rejected",
            "stage": "extraction_start",
            "success": False,
            "batch_id": batch_id,
            "eligible_units": 0,
            "eligible_unit_count": 0,
            "queued_jobs": 0,
            "unit_ids": [str(unit.id) for unit in all_units],
            "unit_statuses": dict(unit_statuses),
            "excluded_unit_statuses": dict(excluded_unit_statuses),
        })
        raise AcqError(ErrorCode.CONFLICT, "batch has no queued extraction units", {
            "report_count": len(reports),
            "report_statuses": dict(report_statuses),
            "unit_count": len(all_units),
            "unit_statuses": dict(unit_statuses),
        })
    batch.awaiting_confirmation = False
    batch.status = "running"
    batch.total_count = len(units)
    batch.completed_count = 0
    batch.failed_count = 0
    for report in reports:
        if report.status != "failed":
            report.status = "extracting"
    for unit in units:
        job_id = enqueue(session, queue, "extract_unit", {"unit_id": str(unit.id)},
                         f"extract_unit:{unit.id}")
        job = session.get(dbm.Job, job_id)
        log.info("batch extraction job inserted", extra={
            "event": "extract_job_created",
            "batch_id": batch_id,
            "report_id": unit.report_id,
            "unit_id": unit.id,
            "job_id": job_id,
            "job_name": "extract_unit",
            "job_status": job.status if job is not None else "queued",
            "dedupe_key": f"extract_unit:{unit.id}",
            "queued_jobs": 1,
        })
    session.flush()
    log.info("batch start transaction staged", extra={
        "event": "batch_start_staged",
        "batch_id": batch_id,
        "eligible_units": len(units),
        "queued_jobs": len(units),
        "jobs_inserted": len(units),
        "batch_status_after": batch.status,
    })
    if hasattr(session, "info"):
        session.info["transaction_log_context"] = {
            "event": "batch_start_committed",
            "stage": "extraction_start",
            "success": True,
            "batch_id": batch_id,
            "batch_status_after": batch.status,
            "eligible_units": len(units),
            "jobs_inserted": len(units),
        }
    return _batch_payload(batch, session)


@router.get("/flags")
def list_flags(status: str = Query(default="open"), property_id: UUID | None = Query(default=None),
               limit: int = Query(default=200), session: Session = Depends(get_session),
               user: User = Depends(current_user)) -> dict:
    limit = max(1, min(limit, 1000))
    active_ids = {row.id for row in session.query(dbm.Property).filter(
        dbm.Property.archived_at.is_(None),
    ).all()}
    if not active_ids:
        return {"items": []}
    query = session.query(dbm.Flag)
    if status != "all":
        query = query.filter(dbm.Flag.status == status)
    # Migration-consolidated duplicates remain in the database for auditability,
    # but are not part of the human review queue or resolved-history view.
    query = query.filter(or_(dbm.Flag.resolution.is_(None), dbm.Flag.resolution != "superseded_duplicate"))
    query = query.filter(dbm.Flag.property_id.in_(active_ids))
    if property_id is not None:
        query = query.filter(dbm.Flag.property_id == property_id)
    rows = query.order_by(
        dbm.Flag.financial_impact_usd.desc().nullslast(), dbm.Flag.id,
    ).limit(limit).all()
    property_ids = {row.property_id for row in rows}
    properties = session.query(dbm.Property).filter(dbm.Property.id.in_(property_ids)).all() if property_ids else []
    by_id = {row.id: row for row in properties}
    return {"items": dump([serializers.flag_record(row, by_id.get(row.property_id)) for row in rows])}


@router.post("/flags/{flag_id}/resolve")
def resolve_flag(flag_id: UUID, body: FlagResolution, session: Session = Depends(get_session),
                 queue: PostgresJobQueue = Depends(get_queue), user: User = Depends(write_user)) -> dict:
    row = session.get(dbm.Flag, flag_id)
    if row is None:
        raise AcqError(ErrorCode.NOT_FOUND, "flag not found")
    if row.status == "resolved":
        raise AcqError(ErrorCode.CONFLICT, "flag is already resolved", {"flag_id": str(flag_id)})
    before = {"status": row.status, "resolution": row.resolution}
    row.status = "resolved"
    row.resolution = body.resolution
    row.note = body.note
    row.resolved_value = body.resolved_value
    row.resolved_at = datetime.now(UTC)
    if hasattr(session, "execute"):
        from flags.workflow import _write_history
        _write_history(session, row.id, f"flag_{body.resolution}", before, {
            "status": row.status, "resolution": row.resolution, "note": body.note,
            "user_id": user.id,
        })
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
    active_property_ids = {row.id for row in session.query(dbm.Property).filter(
        dbm.Property.archived_at.is_(None),
    ).all()}
    rows = (session.query(dbm.Ranking)
            .filter(dbm.Ranking.scope_type == scope_type)
            .order_by(dbm.Ranking.ranked_at.desc())
            .all())
    rows = [row for row in rows if row.property_id in active_property_ids]
    if not rows:
        return {"items": [], "ranked_at": None}
    latest_at = rows[0].ranked_at
    entries = [serializers.ranking_entry(row) for row in rows if row.ranked_at == latest_at]
    entries.sort(key=lambda entry: entry.rank)
    return {"items": dump(entries), "ranked_at": latest_at.isoformat() if latest_at else None}


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    properties = session.query(dbm.Property).filter(
        dbm.Property.merged_into_id.is_(None), dbm.Property.archived_at.is_(None),
    ).all()
    by_status = Counter(row.pipeline_status or "new" for row in properties)
    valued_ids = {row.property_id for row in
                  session.query(dbm.Valuation).filter(dbm.Valuation.is_active.is_(True)).all()}
    active_ids = {row.id for row in properties}
    open_flags = sum(
        row.property_id in active_ids
        for row in session.query(dbm.Flag).filter(dbm.Flag.status == "open").all()
    )
    failed_reports = sum(
        row.property_id is None or row.property_id in active_ids
        for row in session.query(dbm.Report).filter(dbm.Report.status.in_([
        "failed", "failed_provider", "failed_validation", "failed_computation",
        "unresolved_identity",
        ])).all()
    )
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
    active_ids = {row.id for row in session.query(dbm.Property).filter(
        dbm.Property.archived_at.is_(None),
    ).all()}
    open_rows = [row for row in session.query(dbm.Flag).filter(
        dbm.Flag.status == "open",
    ).all() if row.property_id in active_ids]
    gating = [serializers.flag_record(row) for row in open_rows
              if row.flag_type in FlagType.__members__.values() and is_gating(FlagType(row.flag_type))]
    failed = [row for row in session.query(dbm.Report).filter(dbm.Report.status.in_([
        "failed", "failed_provider", "failed_validation", "failed_computation",
        "unresolved_identity",
    ])).all() if row.property_id is None or row.property_id in active_ids]
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
                            effective_from=datetime.now(UTC).date())
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
            .filter(dbm.Property.merged_into_id.is_(None), dbm.Property.archived_at.is_(None), *criteria)
            .order_by(dbm.Property.created_at.desc(), dbm.Property.id.desc())
            .all())
    selected = [column.strip() for column in columns.split(",")] if columns else DEFAULT_EXPORT_COLUMNS
    allowed = set(dbm.Property.__table__.columns.keys()) | set(DEFAULT_EXPORT_COLUMNS)
    unknown = [column for column in selected if column not in allowed]
    if unknown:
        raise AcqError(ErrorCode.INVALID_INPUT, "unknown export column", {"columns": unknown})
    records = (json_safe({column: getattr(row, column, None) for column in selected}) for row in rows)
    stream = stream_properties(records, selected)
    return StreamingResponse(stream, media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=properties.csv"})


@router.get("/exports/full")
def export_full(session: Session = Depends(get_session),
                include_owner_contacts: bool = Query(default=False),
                user: User = Depends(current_user)) -> FileResponse:
    """Create the complete flat export and documents archive."""
    root = Path(tempfile.mkdtemp(prefix="acq-export-"))
    output = root / "acq-export"
    full_export(session.connection(), output, settings.document_root,
                include_owner_contacts=include_owner_contacts)
    archive = root / "acq-export.zip"
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        for path in output.rglob("*"):
            if path.is_file():
                bundle.write(path, path.relative_to(output))
    return FileResponse(archive, media_type="application/zip", filename="acq-export.zip")


@router.get("/calibration")
def calibration_summary(session: Session = Depends(get_session),
                        user: User = Depends(current_user)) -> dict:
    rows = (session.query(dbm.RealizedDeal)
            .join(dbm.Property, dbm.Property.id == dbm.RealizedDeal.property_id)
            .filter(dbm.Property.archived_at.is_(None))
            .order_by(dbm.RealizedDeal.created_at.desc()).all())
    return {"count": len(rows), "items": [json_safe({
        "id": str(row.id), "property_id": str(row.property_id), "outcome": row.outcome,
        "purchase_price": row.purchase_price, "sale_price": row.sale_price,
        "actual_repairs": row.actual_repairs, "actual_costs": row.actual_costs,
    }) for row in rows]}


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
