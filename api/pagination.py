"""Cursor pagination on (sort_key, id) — offset pagination on a 100k-row,
sorted table is a performance trap (WP-11 notes)."""
import base64
import json
from datetime import date, datetime
from typing import Any
from uuid import UUID

from common.errors import AcqError, ErrorCode

from .filters import FIELD_MAP, _coerce


def _serialize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def encode_cursor(sort_value: Any, row_id: UUID) -> str:
    payload = json.dumps({"v": _serialize(sort_value), "id": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, sort_field: str) -> tuple[Any, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        raw_value, raw_id = payload["v"], payload["id"]
        row_id = UUID(str(raw_id))
    except Exception:
        raise AcqError(ErrorCode.INVALID_INPUT, "malformed cursor", {"cursor": cursor})
    if raw_value is None:
        return None, row_id
    if sort_field == "id":
        return UUID(str(raw_value)), row_id
    coerce = FIELD_MAP.get(sort_field, (None, str, False))[1]
    return _coerce(coerce, raw_value, sort_field), row_id
