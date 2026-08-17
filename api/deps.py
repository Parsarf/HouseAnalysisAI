"""Injectable dependencies for the API.

``get_session`` and ``get_queue`` are the two seams tests override with fakes;
every DB-touching endpoint takes the session via ``Depends`` instead of opening
its own, so the whole surface runs offline under FastAPI's TestClient.
"""
import json
import logging
from collections.abc import Iterator
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from common.db import SessionLocal
from jobs.postgres import PostgresJobQueue

log = logging.getLogger(__name__)


def get_session(request: Request) -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
        context = getattr(session, "info", {}).pop("transaction_log_context", {})
        log.info("database transaction committed", extra={
            "event": "database_transaction_committed",
            "request_method": request.method,
            "request_path": request.url.path,
            "transaction_status": "committed",
            **context,
        })
    except Exception:
        session.rollback()
        log.exception("database transaction rolled back", extra={
            "event": "database_transaction_rolled_back",
            "stage": "database_transaction",
            "success": False,
            "request_method": request.method,
            "request_path": request.url.path,
            "transaction_status": "rolled_back",
        })
        raise
    finally:
        session.close()


def get_queue() -> PostgresJobQueue:
    return PostgresJobQueue()


def enqueue(session: Session, queue: PostgresJobQueue, name: str, payload: dict, dedupe_key: str) -> UUID:
    return queue.enqueue(session, name, json.dumps(payload), dedupe_key)
