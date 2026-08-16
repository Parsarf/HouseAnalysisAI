"""Injectable dependencies for the API.

``get_session`` and ``get_queue`` are the two seams tests override with fakes;
every DB-touching endpoint takes the session via ``Depends`` instead of opening
its own, so the whole surface runs offline under FastAPI's TestClient.
"""
import json
from collections.abc import Iterator
from uuid import UUID

from sqlalchemy.orm import Session

from common.db import SessionLocal
from jobs.postgres import PostgresJobQueue


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_queue() -> PostgresJobQueue:
    return PostgresJobQueue()


def enqueue(session: Session, queue: PostgresJobQueue, name: str, payload: dict, dedupe_key: str) -> UUID:
    return queue.enqueue(session, name, json.dumps(payload), dedupe_key)
