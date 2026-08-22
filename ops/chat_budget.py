"""Persistent spend, token, and retrieval-cache controls for property chat."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from .budget import Budget


def chat_session_key(user_id: str, session_id: UUID) -> str:
    user_key = hashlib.sha256(user_id.encode()).hexdigest()[:16]
    return f"chat_session:{user_key}:{session_id}"


def _decode(value: object) -> dict:
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _ensure_and_lock(session: Session, key: str, initial: dict) -> dict:
    dialect = session.get_bind().dialect.name
    encoded = json.dumps(initial)
    if dialect == "postgresql":
        session.execute(text(
            "INSERT INTO settings(key, value) VALUES (:key, CAST(:value AS jsonb)) "
            "ON CONFLICT (key) DO NOTHING",
        ), {"key": key, "value": encoded})
        value = session.execute(text(
            "SELECT value FROM settings WHERE key = :key FOR UPDATE",
        ), {"key": key}).scalar()
    else:
        value = session.execute(text(
            "SELECT value FROM settings WHERE key = :key",
        ), {"key": key}).scalar()
        if value is None:
            session.execute(text(
                "INSERT INTO settings(key, value) VALUES (:key, :value)",
            ), {"key": key, "value": encoded})
            value = initial
    return _decode(value)


def _write(session: Session, key: str, value: dict) -> None:
    encoded = json.dumps(value)
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text(
            "UPDATE settings SET value = CAST(:value AS jsonb) WHERE key = :key",
        ), {"key": key, "value": encoded})
    else:
        session.execute(text(
            "UPDATE settings SET value = :value WHERE key = :key",
        ), {"key": key, "value": encoded})


def reserve_daily_chat_budget(session: Session, estimate: Decimal, limit: Decimal) -> bool:
    key = f"chat_spend:{datetime.now(UTC).date().isoformat()}"
    state = _ensure_and_lock(session, key, {"reserved": "0"})
    budget = Budget(limit)
    budget.reserved = Decimal(str(state.get("reserved", 0)))
    decision = budget.check_and_reserve(max(Decimal(0), estimate))
    if not decision.allowed:
        return False
    _write(session, key, {"reserved": str(budget.reserved)})
    return True


def reconcile_daily_chat_budget(
    session: Session, reserved: Decimal, actual: Decimal,
) -> None:
    key = f"chat_spend:{datetime.now(UTC).date().isoformat()}"
    state = _ensure_and_lock(session, key, {"reserved": "0"})
    spent = Decimal(str(state.get("reserved", 0)))
    reconciled = max(Decimal(0), spent - max(Decimal(0), reserved) + max(Decimal(0), actual))
    _write(session, key, {"reserved": str(reconciled)})


def reserve_chat_session_tokens(
    session: Session, key: str, estimated_tokens: int, token_cap: int,
) -> bool:
    state = _ensure_and_lock(session, key, {"tokens": 0, "document_cache": {}})
    used = int(state.get("tokens") or 0)
    estimate = max(0, estimated_tokens)
    if used + estimate > max(0, token_cap):
        return False
    state["tokens"] = used + estimate
    _write(session, key, state)
    return True


def reconcile_chat_session_tokens(
    session: Session, key: str, reserved_tokens: int, actual_tokens: int,
) -> None:
    state = _ensure_and_lock(session, key, {"tokens": 0, "document_cache": {}})
    used = int(state.get("tokens") or 0)
    state["tokens"] = max(0, used - max(0, reserved_tokens) + max(0, actual_tokens))
    _write(session, key, state)


def cached_document_text(session: Session, key: str, cache_key: str) -> dict | None:
    state = _ensure_and_lock(session, key, {"tokens": 0, "document_cache": {}})
    cache = state.get("document_cache")
    if not isinstance(cache, dict):
        return None
    value = cache.get(cache_key)
    return value if isinstance(value, dict) else None


def cache_document_text(session: Session, key: str, cache_key: str, value: dict) -> None:
    state = _ensure_and_lock(session, key, {"tokens": 0, "document_cache": {}})
    cache = state.get("document_cache")
    if not isinstance(cache, dict):
        cache = {}
    cache[cache_key] = value
    while len(cache) > 20:
        cache.pop(next(iter(cache)))
    state["document_cache"] = cache
    _write(session, key, state)
