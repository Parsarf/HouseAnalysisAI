"""Filter-grammar → SQLAlchemy translation (WP-11; the grammar is a contract
shared with WP-12/14/15).

``translate_filters`` validates a list of ``FilterClause`` against a closed
allowlist of filterable fields and coerces values to the column's Python type.
``apply_filters`` turns the result into real query criteria — nothing is
echoed back untranslated, and anything outside the allowlist is a 422.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, or_

from common.errors import AcqError, ErrorCode
from contracts import FilterClause
from db.models import Property

# field name -> (column, coercion, is_array)
FIELD_MAP: dict[str, tuple[Any, Any, bool]] = {
    "apn": (Property.apn, str, False),
    "address": (Property.address_line1, str, False),
    "city": (Property.city, str, False),
    "state": (Property.state, str, False),
    "zip5": (Property.zip5, str, False),
    "pipeline_status": (Property.pipeline_status, str, False),
    "status": (Property.pipeline_status, str, False),
    "tags": (Property.tags, str, True),
    "next_action": (Property.next_action, str, False),
    "next_action_date": (Property.next_action_date, date, False),
    "gut_rating": (Property.gut_rating, int, False),
    "is_watchlisted": (Property.is_watchlisted, bool, False),
    "lat": (Property.lat, Decimal, False),
    "lng": (Property.lng, Decimal, False),
    "created_at": (Property.created_at, datetime, False),
    "updated_at": (Property.updated_at, datetime, False),
}

# Sorting shares the filter allowlist (minus array columns) plus id.
SORT_MAP: dict[str, Any] = {name: column for name, (column, _coerce, is_array) in FIELD_MAP.items() if not is_array}
SORT_MAP["id"] = Property.id

_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "in", "between", "contains", "is_null"}


def _invalid(message: str, details: dict | None = None) -> AcqError:
    return AcqError(ErrorCode.INVALID_INPUT, message, details or {})


def _coerce(coerce: Any, value: Any, field: str) -> Any:
    if value is None:
        return None
    try:
        if coerce is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "false"):
                return value.lower() == "true"
            raise ValueError(value)
        if coerce is date:
            return date.fromisoformat(str(value))
        if coerce is datetime:
            return datetime.fromisoformat(str(value))
        if coerce is Decimal:
            return Decimal(str(value))
        return coerce(value)
    except (ValueError, TypeError, InvalidOperation):
        raise _invalid(f"invalid value for filter field '{field}'", {"field": field, "value": value})


def translate_filters(clauses: list[FilterClause]) -> list[Any]:
    """Validate + translate filter clauses into SQLAlchemy criteria."""
    criteria = []
    for clause in clauses:
        if clause.field not in FIELD_MAP:
            raise _invalid(f"unknown filter field '{clause.field}'", {"field": clause.field, "allowed": sorted(FIELD_MAP)})
        if clause.op not in _OPS:
            raise _invalid(f"unknown filter operator '{clause.op}'", {"op": clause.op, "allowed": sorted(_OPS)})
        column, coerce, is_array = FIELD_MAP[clause.field]
        value = clause.value
        if clause.op == "is_null":
            criteria.append(column.is_(None) if value in (None, True) else column.is_not(None))
        elif clause.op == "in":
            if not isinstance(value, list) or not value:
                raise _invalid("'in' requires a non-empty list value", {"field": clause.field})
            criteria.append(column.in_([_coerce(coerce, item, clause.field) for item in value]))
        elif clause.op == "between":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise _invalid("'between' requires a [low, high] value", {"field": clause.field})
            low, high = (_coerce(coerce, item, clause.field) for item in value)
            criteria.append(and_(column >= low, column <= high))
        elif clause.op == "contains":
            if is_array:
                criteria.append(column.contains([str(value)]))
            else:
                criteria.append(column.ilike(f"%{value}%"))
        else:
            if is_array:
                raise _invalid(f"operator '{clause.op}' is not supported for array field '{clause.field}'")
            operand = _coerce(coerce, value, clause.field)
            if coerce is bool and clause.op in ("eq", "neq"):
                criteria.append(column.is_(operand) if clause.op == "eq" else column.is_not(operand))
                continue
            criteria.append({
                "eq": column == operand,
                "neq": column != operand,
                "gt": column > operand,
                "gte": column >= operand,
                "lt": column < operand,
                "lte": column <= operand,
            }[clause.op])
    return criteria


def parse_sort(sort: str | None) -> tuple[str, Any, bool]:
    """Parse `?sort=-created_at` into (field name, column, descending). Defaults to newest first."""
    key = (sort or "-created_at").strip()
    descending = key.startswith("-")
    name = key.lstrip("+-")
    if name not in SORT_MAP:
        raise _invalid(f"unknown sort field '{name}'", {"field": name, "allowed": sorted(SORT_MAP)})
    return name, SORT_MAP[name], descending


def cursor_condition(column: Any, descending: bool, sort_value: Any, row_id: Any) -> Any:
    """Keyset condition for rows strictly after (sort_value, row_id) in sort order."""
    if descending:
        after_value, tiebreak = column < sort_value, Property.id < row_id
    else:
        after_value, tiebreak = column > sort_value, Property.id > row_id
    if sort_value is None:
        return and_(column.is_(None), tiebreak)
    return or_(after_value, and_(column == sort_value, tiebreak))
