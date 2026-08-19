"""Flatten a schema-shaped provider payload into ExtractedFactDrafts.

The provider returns one JSON object per unit schema (spec §18), e.g.
``{"liens": [{...}, ...]}``. Each scalar leaf becomes one fact whose
``field_path`` is ``<array>[<index>].<field>``; ``<name>_raw``/``<name>_parsed``
pairs merge into a single ``<name>`` fact. Validation is *not* done here —
drafts go through the gauntlet in ``validation.py``.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from contracts import EntityType, ExtractedFactDraft, NullReason

from .schemas import canonical_unit_type, schema_for, top_level_key

UNIT_ENTITY_TYPES: dict[str, EntityType] = {
    "property_core": EntityType.PROPERTY,
    "ownership": EntityType.PROPERTY,
    "mortgages": EntityType.MORTGAGE,
    "foreclosure": EntityType.FORECLOSURE,
    "bankruptcy": EntityType.BANKRUPTCY,
    "valuation": EntityType.VALUATION,
    "comparables": EntityType.COMP,
    "listings": EntityType.LISTING,
    "tax": EntityType.TAX,
    "rental": EntityType.RENTAL,
    "condition_signals": EntityType.CONDITION,
    "liens": EntityType.LIEN,
}

_PROVENANCE_KEYS = {"page_number", "snippet", "extraction_confidence", "null_reason"}


def _safe_additional_fact_leaf(label: str, category: str | None) -> str:
    descriptor = "_".join(part for part in (category, label) if part)
    slug = re.sub(r"[^a-z0-9]+", "_", descriptor.casefold()).strip("_")
    return f"detail_{slug or 'unmapped'}"


def _date_fields(unit_type: str) -> set[str]:
    schema = schema_for(unit_type)
    item = next(iter(schema["properties"].values()))["items"]
    return {name for name, spec in item["properties"].items() if spec.get("format") == "date"}


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _to_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def flatten_payload(
    unit_type: str,
    payload: dict,
    *,
    report_id: UUID,
    extraction_unit_id: UUID,
) -> tuple[list[ExtractedFactDraft], int]:
    """Returns ``(drafts, dropped)``; drafts that fail Pydantic schema
    validation (e.g. snippet over 200 chars) are counted as dropped."""
    canonical = canonical_unit_type(unit_type)
    entity_type = UNIT_ENTITY_TYPES[canonical]
    date_fields = _date_fields(unit_type)
    key = top_level_key(unit_type)
    items = payload.get(key) or []
    drafts: list[ExtractedFactDraft] = []
    dropped = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            dropped += 1
            continue
        if not item.get("snippet") or not item.get("page_number"):
            # Schema-required provenance missing: an empty snippet would pass
            # grounding trivially, so drop the whole item at the schema stage.
            dropped += 1
            continue
        entity_local_id = f"{key}[{index}]"
        try:
            null_reason = NullReason(item["null_reason"]) if item.get("null_reason") else None
        except ValueError:
            dropped += 1
            continue
        page_number = int(item.get("page_number") or 1)
        snippet = str(item.get("snippet") or "")
        confidence = item.get("extraction_confidence")
        extraction_confidence = float(confidence) if confidence is not None else 0.0
        # Pair <name>_raw / <name>_parsed into a single <name> fact; a lone
        # ``<name>_raw`` (e.g. creditor_raw) is text, stored as value_text.
        bases: list[str] = []
        for name in item:
            if name in _PROVENANCE_KEYS or name.endswith("_parsed"):
                continue
            if name.endswith("_raw") and f"{name[:-4]}_parsed" in item:
                base = name[:-4]
            else:
                base = name
            if base not in bases:
                bases.append(base)
        for base in bases:
            paired = f"{base}_parsed" in item
            field_path = f"{entity_local_id}.{base}"
            value_raw = item.get(f"{base}_raw") if paired else None
            value_parsed = _to_decimal(item.get(f"{base}_parsed"))
            value_text, value_date, value_bool = None, None, None
            if not paired:
                raw_value = item.get(base)
                if base in date_fields:
                    value_date = _to_date(raw_value)
                elif isinstance(raw_value, bool):
                    value_bool = raw_value
                elif isinstance(raw_value, (int, float)):
                    value_parsed = _to_decimal(raw_value)
                elif isinstance(raw_value, list):
                    value_text = "; ".join(str(v) for v in raw_value) if raw_value else None
                elif raw_value is not None:
                    value_text = str(raw_value)
            try:
                drafts.append(ExtractedFactDraft(
                    report_id=report_id,
                    extraction_unit_id=extraction_unit_id,
                    entity_type=entity_type,
                    entity_local_id=entity_local_id,
                    page_number=page_number,
                    snippet=snippet,
                    extraction_confidence=extraction_confidence,
                    null_reason=null_reason,
                    field_path=field_path,
                    value_raw=value_raw,
                    value_parsed=value_parsed,
                    value_text=value_text,
                    value_date=value_date,
                    value_bool=value_bool,
                ))
            except Exception:
                # Schema-invalid draft (e.g. snippet > 200 chars): dropped by the
                # schema stage of the gauntlet.
                dropped += 1
                continue

    # Catch-all facts still become ordinary ExtractedFactDrafts and therefore
    # pass through the same grounding and validation gauntlet as predefined
    # fields. The ``detail_`` prefix prevents an unmapped fact from silently
    # masquerading as a normalized/calculated property field downstream.
    for index, item in enumerate(payload.get("additional_facts") or []):
        if not isinstance(item, dict):
            dropped += 1
            continue
        label = item.get("label")
        value = item.get("value")
        page_number_raw = item.get("source_page")
        snippet_raw = item.get("snippet")
        if not label or value is None or not isinstance(page_number_raw, int) or not isinstance(snippet_raw, str) or not snippet_raw:
            dropped += 1
            continue
        page_number = page_number_raw
        snippet = snippet_raw
        category = item.get("category")
        confidence = item.get("confidence")
        entity_local_id = f"additional_facts[{index}]"
        try:
            drafts.append(ExtractedFactDraft(
                report_id=report_id,
                extraction_unit_id=extraction_unit_id,
                entity_type=EntityType.PROPERTY,
                entity_local_id=entity_local_id,
                page_number=int(page_number),
                snippet=str(snippet),
                extraction_confidence=float(confidence) if confidence is not None else 0.0,
                field_path=(
                    f"{entity_local_id}."
                    f"{_safe_additional_fact_leaf(str(label), str(category) if category else None)}"
                ),
                value_text=str(value),
            ))
        except Exception:
            dropped += 1
    return drafts, dropped
