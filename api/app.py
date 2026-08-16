"""ACQ HTTP API (WP-11, spec §16).

Every error leaves the process as one structured envelope
``{error: {code, message, details}}`` — the closed code set is shared with the
frontend, and internal exception text never crosses the boundary.
"""
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from fastapi import Body, Depends, FastAPI, File, Form, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from auth.dependencies import User, current_user, make_session, write_user
from auth.service import verify_password
from common.errors import AcqError, ErrorCode
from common.settings import settings
from contracts import ErrorDetail, ErrorEnvelope, FilterClause
from db import models as dbm
from ingestion import ingest_paste, register_pdf
from jobs.postgres import PostgresJobQueue

from .deps import enqueue, get_queue, get_session
from .filters import translate_filters
from .routes_portfolio import router as portfolio_router
from .routes_properties import router as properties_router
from .serializers import dump

app = FastAPI(title="ACQ", version="0.1.0")

_STATUS_BY_CODE = {
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.DUPLICATE: 409,
    ErrorCode.CONFLICT: 409,
    ErrorCode.LOCKED: 423,
    ErrorCode.BUDGET_PAUSED: 409,
    ErrorCode.NOT_PDF: 422,
    ErrorCode.ENCRYPTED: 422,
    ErrorCode.CORRUPT: 422,
}

_HTTP_CODE_BY_STATUS = {400: "invalid_input", 401: "unauthorized", 403: "read_only",
                        404: "not_found", 405: "invalid_input", 409: "conflict",
                        422: "invalid_input", 423: "locked", 429: "budget_paused"}


def _envelope(code: str, message: str, details: dict | None = None) -> dict:
    return dump(ErrorEnvelope(error=ErrorDetail(code=code, message=message, details=details or {})))


@app.exception_handler(AcqError)
async def acq_error_handler(request, exc: AcqError):
    return JSONResponse(status_code=_STATUS_BY_CODE.get(exc.code, 500),
                        content=_envelope(exc.code.value, exc.message, exc.details))


@app.exception_handler(HTTPException)
async def http_error_handler(request, exc: HTTPException):
    code = _HTTP_CODE_BY_STATUS.get(exc.status_code, "internal")
    message = exc.detail if isinstance(exc.detail, str) else "request failed"
    return JSONResponse(status_code=exc.status_code, content=_envelope(code, message))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError):
    errors = json.loads(json.dumps(exc.errors(), default=str))
    return JSONResponse(status_code=422, content=_envelope(
        ErrorCode.INVALID_INPUT.value, "request validation failed",
        {"errors": errors}))


@app.exception_handler(Exception)
async def error_handler(request, exc: Exception):
    # Never leak str(exc) to the client (WP-11); details stay in the logs.
    return JSONResponse(status_code=500, content=_envelope(
        ErrorCode.INTERNAL.value, "internal server error"))


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/login")
def login(password: str = Form(...), read_only: bool = Form(default=False)):
    if settings.auth_password_hash is None or not verify_password(password, settings.auth_password_hash):
        return JSONResponse(status_code=401, content=_envelope("invalid_input", "invalid credentials"))
    response = JSONResponse({"ok": True})
    response.set_cookie("session_cookie", make_session("owner", read_only, settings.session_secret),
                        httponly=True, samesite="lax", secure=settings.secure_cookie)
    return response


@app.get("/api/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"id": user.id, "read_only": user.read_only}


@app.post("/api/filter/validate")
def validate_filter(filters: list[FilterClause], user: User = Depends(current_user)) -> dict:
    # Validation is real: unknown fields/operators raise invalid_input.
    translate_filters(filters)
    return {"valid": True, "filters": dump(filters)}


@app.post("/api/uploads")
async def upload(files: list[UploadFile] = File(...), batch_name: str | None = Form(default=None),
                 session: Session = Depends(get_session), queue: PostgresJobQueue = Depends(get_queue),
                 user: User = Depends(write_user)) -> dict:
    batch_id = uuid4()
    root = settings.document_root
    root.mkdir(parents=True, exist_ok=True)
    batch = dbm.Batch(id=batch_id, name=batch_name, file_count=len(files),
                      total_count=len(files), status="uploaded")
    session.add(batch)
    reports = []
    for upload_file in files:
        with NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            while chunk := await upload_file.read(1024 * 1024):
                tmp.write(chunk)
            temp_path = Path(tmp.name)
        try:
            report, created = register_pdf(session, temp_path, root, batch_id=batch_id)
        finally:
            temp_path.unlink(missing_ok=True)
        reports.append(str(report.id))
        if created:
            enqueue(session, queue, "ingest_document", {"report_id": str(report.id)}, f"ingest:{report.id}")
    return {"batch_id": str(batch_id), "report_ids": reports, "count": len(reports)}


@app.post("/api/ingest/paste")
def paste(body: dict = Body(...), session: Session = Depends(get_session),
          queue: PostgresJobQueue = Depends(get_queue), user: User = Depends(write_user)) -> dict:
    text = (body.get("text") or "").strip()
    if not text:
        raise AcqError(ErrorCode.INVALID_INPUT, "text is required")
    batch = dbm.Batch(id=uuid4(), name=body.get("batch_name") or "paste",
                      file_count=1, total_count=1, status="uploaded")
    session.add(batch)
    report, created = ingest_paste(session, text, settings.document_root, batch_id=batch.id)
    if created:
        enqueue(session, queue, "ingest_document", {"report_id": str(report.id)}, f"ingest:{report.id}")
    return {"batch_id": str(batch.id), "report_ids": [str(report.id)], "count": 1}


app.include_router(properties_router)
app.include_router(portfolio_router)

# SPA serving: web/dist is served when the frontend has been built; API and
# health routes are registered first and always win.
_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if (_DIST / "index.html").exists():
    if (_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="spa-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/") or full_path == "healthz":
            return JSONResponse(status_code=404, content=_envelope("not_found", "not found"))
        candidate = _DIST / full_path
        if full_path and candidate.is_file() and candidate.resolve().is_relative_to(_DIST):
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
