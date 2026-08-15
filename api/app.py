from decimal import Decimal
from uuid import UUID

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from auth.dependencies import User, current_user
from common.errors import ErrorCode
from common.serializers import json_safe
from contracts import FilterClause

app = FastAPI(title="ACQ", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(Exception)
async def error_handler(request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": {"code": ErrorCode.INTERNAL.value, "message": str(exc), "details": {}}})


@app.get("/api/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"id": user.id, "read_only": user.read_only}


@app.post("/api/filter/validate")
def validate_filter(filters: list[FilterClause], user: User = Depends(current_user)) -> dict:
    return {"filters": [item.model_dump(mode="json") for item in filters]}


@app.get("/api/properties/{property_id}/analysis")
def analysis(property_id: UUID, scenario: str = "expected", user: User = Depends(current_user)) -> dict:
    # The endpoint shape is stable while persistence-backed analysis is wired in.
    return {"property_id": str(property_id), "scenario": scenario, "normalized": None, "underwriting": None, "strategies": [], "offers": None, "scores": None, "flags": [], "timeline": []}
