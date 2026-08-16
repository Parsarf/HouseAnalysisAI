import json
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from common.errors import AcqError, ErrorCode
from db.models import Flag

RESOLUTIONS = ("approve", "reject", "replace", "dismiss")

# Injectable so tests stay offline: hook(session, property_id, reason) -> anything.
RecomputeHook = Callable[[Session, UUID, str], Any]

_ENTITY_TYPE_BY_PREFIX = {
    "property": "property", "mortgage": "mortgage", "lien": "lien",
    "foreclosure": "foreclosure", "bankruptcy": "bankruptcy", "valuation": "valuation",
    "listing": "listing", "comp": "comp", "tax": "tax", "rental": "rental",
    "condition": "condition",
}

_INSERT_FACT_SQL = text("""
INSERT INTO extracted_facts (id, property_id, entity_type, entity_local_id, field_path,
    value_raw, value_parsed, value_text, value_date, value_bool,
    page_number, snippet, extraction_confidence, null_reason, source_kind)
VALUES (:id, :property_id, :entity_type, :entity_local_id, :field_path,
    :value_raw, :value_parsed, :value_text, :value_date, :value_bool,
    :page_number, :snippet, :extraction_confidence, :null_reason, :source_kind)
""")

def _history_sql(session: Session):
    def value(column: str) -> str:
        return f"CAST(:{column} AS jsonb)" if session.bind.dialect.name == "postgresql" else f":{column}"
    return text(f"""
INSERT INTO history (id, entity_type, entity_id, action, before, after, at)
VALUES (:id, :entity_type, :entity_id, :action, {value("before")}, {value("after")}, :at)
""")


def _default_recompute_hook(session: Session, property_id: UUID, reason: str) -> UUID:
    """Enqueue recompute_property (WP-10 owns execution). Lazy import: flags must not
    depend on pipeline/jobs at module load."""
    from jobs.postgres import PostgresJobQueue

    payload = json.dumps({"property_id": str(property_id), "reason": reason})
    return PostgresJobQueue().enqueue(session, "recompute_property", payload,
                                      dedupe_key=f"recompute_property:{property_id}")


def _bindable(value: Any) -> Any:
    """Raw text() params must bind on both psycopg and sqlite3: stringify UUID/Decimal/date."""
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _write_history(session: Session, entity_id: UUID, action: str, before: dict, after: dict) -> None:
    session.execute(_history_sql(session), {
        "id": str(uuid4()), "entity_type": "flag", "entity_id": str(entity_id), "action": action,
        "before": json.dumps(before, default=str), "after": json.dumps(after, default=str),
        "at": datetime.now(timezone.utc).isoformat()})


def _fact_columns(field_path: str, value: Any, note: str | None) -> dict:
    prefix = field_path.split(".", 1)[0].split("[", 1)[0].rstrip("s")
    columns: dict[str, Any] = {
        "id": uuid4(), "entity_type": _ENTITY_TYPE_BY_PREFIX.get(prefix, "property"),
        "entity_local_id": prefix, "field_path": field_path,
        "value_raw": None, "value_parsed": None, "value_text": None, "value_date": None,
        "value_bool": None, "page_number": 1, "snippet": (note or "human override")[:200],
        # Precedence 1.0: a human fact outranks every other source kind (spec §12).
        "extraction_confidence": 1.0, "null_reason": None, "source_kind": "human",
    }
    if value is None:
        columns["null_reason"] = "not_present"
    elif isinstance(value, bool):
        columns["value_bool"] = value
        columns["value_raw"] = str(value)
    elif isinstance(value, (Decimal, int)):
        columns["value_parsed"] = Decimal(str(value))
        columns["value_raw"] = str(value)
    elif isinstance(value, date):
        columns["value_date"] = value
        columns["value_raw"] = value.isoformat()
    else:
        columns["value_text"] = str(value)
        columns["value_raw"] = str(value)
    return columns


def apply_override(property_id: UUID, field_path: str, value: Any, note: str | None, user_id: str | None,
                   *, session: Session | None = None, recompute_hook: RecomputeHook | None = None) -> UUID:
    """Write a source_kind='human' fact (WP-9's public override function) and trigger recompute."""
    if session is not None:
        return _apply_override(session, property_id, field_path, value, note, user_id, recompute_hook)
    from common.db import db_session  # lazy: production session factory needs a live DB

    with db_session() as owned:
        return _apply_override(owned, property_id, field_path, value, note, user_id, recompute_hook)


def _apply_override(session: Session, property_id: UUID, field_path: str, value: Any,
                    note: str | None, user_id: str | None, recompute_hook: RecomputeHook | None) -> UUID:
    columns = {key: _bindable(item) for key, item in _fact_columns(field_path, value, note).items()}
    columns["property_id"] = str(property_id)
    session.execute(_INSERT_FACT_SQL, columns)
    fact_id = UUID(columns["id"])
    _write_history(session, fact_id, "apply_override",
                   before={}, after={"property_id": str(property_id), "field_path": field_path,
                                     "value": str(value), "note": note, "user_id": user_id,
                                     "source_kind": "human"})
    hook = recompute_hook or _default_recompute_hook
    hook(session, property_id, f"apply_override:{field_path}")
    session.flush()
    return fact_id


def resolve_flag(session: Session, flag_id: UUID, action: str, *,
                 resolved_value: dict | None = None, note: str | None = None,
                 user_id: str | None = None, recompute_hook: RecomputeHook | None = None) -> dict:
    """Resolve an open flag: approve / reject / replace / dismiss (spec §12).

    Every resolution writes a history row and triggers recompute_property; the return
    carries the hook result so the API can show the score/rank delta.
    """
    if action not in RESOLUTIONS:
        raise AcqError(ErrorCode.INVALID_INPUT, f"unknown resolution {action!r}", {"allowed": list(RESOLUTIONS)})
    flag = session.get(Flag, flag_id)
    if flag is None:
        raise AcqError(ErrorCode.NOT_FOUND, f"flag {flag_id} not found")
    if flag.status != "open":
        raise AcqError(ErrorCode.CONFLICT, f"flag {flag_id} already {flag.status}",
                       {"status": flag.status, "resolution": flag.resolution})

    fact_ids = flag.payload.get("fact_ids") if isinstance(flag.payload, dict) else None
    if action == "approve" and fact_ids:
        session.execute(text(
            "UPDATE field_resolutions SET verification_state='human_verified' "
            "WHERE winning_fact_id IN :ids").bindparams(bindparam("ids", expanding=True)),
            {"ids": fact_ids})
    elif action == "reject" and fact_ids:
        session.execute(text(
            "UPDATE extracted_facts SET is_active=false WHERE id IN :ids").bindparams(
            bindparam("ids", expanding=True)), {"ids": fact_ids})
    elif action == "replace":
        if not resolved_value or "field_path" not in resolved_value or "value" not in resolved_value:
            raise AcqError(ErrorCode.INVALID_INPUT,
                           "replace requires resolved_value with field_path and value")
        fact_id = apply_override(flag.property_id, resolved_value["field_path"],
                                 resolved_value["value"], note, user_id,
                                 session=session, recompute_hook=lambda *_: None)
        resolved_value = {**resolved_value, "fact_id": str(fact_id)}

    before = {"status": flag.status, "resolution": flag.resolution}
    flag.status = "resolved"
    flag.resolution = action
    flag.resolved_value = resolved_value
    flag.note = note
    flag.resolved_at = datetime.now(timezone.utc)
    _write_history(session, flag.id, f"flag_{action}", before=before,
                   after={"status": "resolved", "resolution": action, "note": note, "user_id": user_id})
    hook = recompute_hook or _default_recompute_hook
    recompute_result = hook(session, flag.property_id, f"flag_resolved:{flag.flag_type}")
    session.flush()
    return {"flag_id": str(flag.id), "property_id": str(flag.property_id), "resolution": action,
            "recompute": recompute_result}
