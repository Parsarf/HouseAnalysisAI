"""Explainability endpoints: structured audit traces + source-document pages."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth.dependencies import current_user
from common.errors import AcqError, ErrorCode
from db import models as dbm
from explanation import available_keys, build_trace
from explanation.sources import load_report_page

from .deps import get_session
from .serializers import dump

router = APIRouter(prefix="/api", tags=["explain"])

_KEY_NOT_FOUND = ErrorCode.NOT_FOUND


@router.get("/explain/keys")
def keys(user: object = Depends(current_user)) -> dict:
    return {"keys": available_keys()}


@router.get("/properties/{property_id}/explain")
def explain_catalog(property_id: UUID, session: Session = Depends(get_session),
                    user: object = Depends(current_user)) -> dict:
    _get_property(session, property_id)
    return {"property_id": str(property_id), "keys": available_keys()}


@router.get("/properties/{property_id}/explain/{key:path}")
def explain(property_id: UUID, key: str, session: Session = Depends(get_session),
            user: object = Depends(current_user)) -> dict:
    _get_property(session, property_id)
    try:
        trace = build_trace(session, property_id, key)
    except KeyError as exc:
        raise AcqError(_KEY_NOT_FOUND, str(exc.args[0] if exc.args else key)) from exc
    return dump(trace)


class ExplainBatchRequest(BaseModel):
    keys: list[str]


@router.post("/properties/{property_id}/explain/batch")
def explain_batch(property_id: UUID, body: ExplainBatchRequest,
                  session: Session = Depends(get_session), user: object = Depends(current_user)) -> dict:
    _get_property(session, property_id)
    items = []
    missing = []
    for key in body.keys[:60]:
        try:
            items.append(dump(build_trace(session, property_id, key)))
        except KeyError:
            missing.append(key)
        except Exception:  # a broken single key must not sink the batch
            missing.append(key)
    return {"property_id": str(property_id), "traces": items, "missing_keys": missing}


@router.get("/reports/{report_id}/source")
def report_source(report_id: UUID, page: int = Query(default=1, ge=1),
                  property_id: UUID | None = None, fact_id: UUID | None = None,
                  session: Session = Depends(get_session),
                  user: object = Depends(current_user)) -> dict:
    """Text of one page of the stored source document (View-source viewer)."""
    try:
        payload = load_report_page(session, report_id, page)
    except LookupError:
        raise AcqError(ErrorCode.NOT_FOUND, "report not found") from None
    if fact_id is not None:
        fact = session.get(dbm.ExtractedFact, fact_id)
        if fact is not None and fact.snippet:
            payload["snippet"] = fact.snippet
            payload["page"] = max(1, int(fact.page_number or payload["page"]))
    payload["requested_page"] = page
    return payload


def _get_property(session: Session, property_id: UUID) -> dbm.Property:
    row = session.get(dbm.Property, property_id)
    if row is None:
        raise AcqError(ErrorCode.NOT_FOUND, "property not found")
    return row
