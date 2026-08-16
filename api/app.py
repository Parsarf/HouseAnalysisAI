from decimal import Decimal
from uuid import UUID

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

from auth.dependencies import User, current_user
from auth.dependencies import make_session
from auth.service import verify_password
from common.errors import ErrorCode
from common.serializers import json_safe
from contracts import FilterClause
from common.db import db_session
from common.settings import settings
from ingestion import store_pdf
from jobs.postgres import PostgresJobQueue
from db.models import Batch, Property, Report

app = FastAPI(title="ACQ", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/login")
def login(password: str = Form(...), read_only: bool = Form(default=False)):
    if settings.auth_password_hash is None or not verify_password(password, settings.auth_password_hash):
        return JSONResponse(status_code=401, content={"error": {"code": "invalid_input", "message": "invalid credentials"}})
    response = JSONResponse({"ok": True})
    response.set_cookie("session_cookie", make_session("owner", read_only, settings.session_secret), httponly=True, samesite="lax", secure=settings.secure_cookie)
    return response


@app.exception_handler(Exception)
async def error_handler(request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": {"code": ErrorCode.INTERNAL.value, "message": str(exc), "details": {}}})


@app.get("/api/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"id": user.id, "read_only": user.read_only}


@app.post("/api/filter/validate")
def validate_filter(filters: list[FilterClause], user: User = Depends(current_user)) -> dict:
    return {"filters": [item.model_dump(mode="json") for item in filters]}


@app.post("/api/uploads")
async def upload(files: list[UploadFile] = File(...), batch_name: str | None = Form(default=None), user: User = Depends(current_user)) -> dict:
    batch_id = uuid4()
    root = settings.document_root
    root.mkdir(parents=True, exist_ok=True)
    reports = []
    with db_session() as session:
        batch = Batch(id=batch_id, name=batch_name, file_count=len(files), total_count=len(files), status="uploaded")
        session.add(batch)
        for upload_file in files:
            with NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                while chunk := await upload_file.read(1024 * 1024):
                    tmp.write(chunk)
                temp_path = Path(tmp.name)
            try:
                report_id, digest = store_pdf(temp_path, root)
            finally:
                temp_path.unlink(missing_ok=True)
            report = Report(id=report_id, batch_id=batch_id, file_path=str(root / str(report_id) / "original.pdf"), sha256=digest, status="uploaded")
            session.add(report)
            reports.append(str(report_id))
            PostgresJobQueue().enqueue(session, "ingest_document", json.dumps({"report_id": str(report_id)}), f"ingest:{report_id}")
    return {"batch_id": str(batch_id), "report_ids": reports, "count": len(reports)}


@app.get("/api/batches/{batch_id}")
def batch_status(batch_id: UUID, user: User = Depends(current_user)) -> dict:
    with db_session() as session:
        batch = session.get(Batch, batch_id)
        if batch is None:
            return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "batch not found"}})
        return {"id": str(batch.id), "status": batch.status, "total": batch.total_count, "completed": batch.completed_count, "failed": batch.failed_count, "estimated_cost_usd": json_safe(batch.estimated_cost_usd)}


@app.get("/api/properties")
def properties(limit: int = 50, user: User = Depends(current_user)) -> dict:
    limit = max(1, min(limit, 500))
    with db_session() as session:
        rows = session.query(Property).filter(Property.merged_into_id.is_(None)).limit(limit).all()
        return {"items": [{"id": str(row.id), "address": row.address_line1, "city": row.city, "state": row.state, "zip5": row.zip5, "status": row.pipeline_status, "tags": row.tags, "gut_rating": row.gut_rating} for row in rows], "next_cursor": None}


@app.patch("/api/properties/{property_id}")
def update_property(property_id: UUID, changes: dict, user: User = Depends(current_user)) -> dict:
    if user.read_only:
        return JSONResponse(status_code=403, content={"error": {"code": "read_only", "message": "read-only user cannot mutate"}})
    allowed = {"pipeline_status", "tags", "next_action", "next_action_date", "gut_rating", "is_watchlisted"}
    with db_session() as session:
        row = session.get(Property, property_id)
        if row is None:
            return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "property not found"}})
        for key, value in changes.items():
            if key in allowed:
                setattr(row, key, value)
        return {"id": str(row.id), "status": row.pipeline_status, "tags": row.tags, "next_action": row.next_action, "gut_rating": row.gut_rating}


@app.get("/api/properties/{property_id}/analysis")
def analysis(property_id: UUID, scenario: str = "expected", user: User = Depends(current_user)) -> dict:
    # The endpoint shape is stable while persistence-backed analysis is wired in.
    return {"property_id": str(property_id), "scenario": scenario, "normalized": None, "underwriting": None, "strategies": [], "offers": None, "scores": None, "flags": [], "timeline": []}
