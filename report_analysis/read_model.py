"""Shared multi-document property evidence and report associations."""
from __future__ import annotations

import copy
import json
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import or_

from db import models as dbm


def source_report_id(session, report):
    seen = set()
    while report.duplicate_of and report.id not in seen:
        seen.add(report.id)
        parent = session.get(dbm.Report, report.duplicate_of)
        if parent is None:
            break
        report = parent
    return report.id


def merge_sources(sources: list[dict]) -> tuple[dict, list[dict]]:
    """Sources are ordered by precedence; missing values never erase evidence."""
    issues = []

    def row_key(item):
        if not isinstance(item, dict):
            return json.dumps(item, sort_keys=True)
        for field in ("document_number", "case_number", "recording_doc_number"):
            if item.get(field):
                return (field, str(item[field]).strip().casefold())
        return json.dumps({k: v for k, v in item.items()
                           if k not in {"source_page", "confidence", "evidence"}}, sort_keys=True)

    def merge(left, right, path):
        if left is None:
            return copy.deepcopy(right)
        if right is None:
            return left
        if isinstance(left, dict) and isinstance(right, dict):
            for key, value in right.items():
                left[key] = merge(left.get(key), value, f"{path}.{key}".strip("."))
        elif isinstance(left, list) and isinstance(right, list):
            keys = {row_key(item): index for index, item in enumerate(left)}
            for item in right:
                key = row_key(item)
                if key not in keys:
                    keys[key] = len(left)
                    left.append(copy.deepcopy(item))
                elif isinstance(item, dict):
                    index = keys[key]
                    left[index] = merge(left[index], item, path)
        elif left != right and path not in {"source_page", "confidence"}:
            issues.append({"code": "conflicting_evidence", "path": path,
                           "selected": left, "alternative": right})
        return left

    result: dict[str, Any] = {}
    for source in sources:
        result = merge(result, source, "")
    return result, issues


def active_entities(session, property_id):
    property_ids = {property_id}
    pending = [property_id]
    while pending:
        children = session.query(dbm.Property).filter(dbm.Property.merged_into_id.in_(pending)).all()
        pending = [row.id for row in children if row.id not in property_ids]
        property_ids.update(pending)
    return session.query(dbm.ReportEntityExtraction).filter(
        dbm.ReportEntityExtraction.property_id.in_(property_ids),
        dbm.ReportEntityExtraction.active.is_(True),
        dbm.ReportEntityExtraction.status == "complete",
    ).all()


def load_record(session, property_id: UUID, *, pending=None):
    from .normalizer import canonical_to_normalized, validate_and_normalize

    entities = active_entities(session, property_id)
    if pending is not None:
        entities = [entity for entity in entities if entity.report_id != pending.report_id]
        entities.append(pending)
    if not entities:
        return None
    reports = {row.id: row for row in session.query(dbm.Report).filter(
        dbm.Report.id.in_({entity.report_id for entity in entities}),
    ).all()}
    # Source date and evidence confidence precede arrival order. Stable ID breaks ties.
    def precedence(entity):
        report = reports[entity.report_id]
        references = (entity.raw_json or {}).get("source_references", [])
        confidence = max((ref.get("confidence") or 0 for ref in references), default=0)
        priority = {"subject": 3, "listing": 2, "reference": 1, "comparable": 0}.get(entity.role, 0)
        return (priority, report.generated_date or date.min, confidence, str(entity.id))
    entities.sort(key=precedence, reverse=True)
    sources = [entity.raw_json for entity in entities if entity.raw_json]
    if not sources:
        return None
    source, _issues = merge_sources(sources)
    # Explicit analyst facts retain priority over provider facts.
    overrides = session.query(dbm.ExtractedFact).filter(
        dbm.ExtractedFact.property_id == property_id,
        dbm.ExtractedFact.is_active.is_(True), dbm.ExtractedFact.source_kind == "human",
    ).order_by(dbm.ExtractedFact.created_at).all()
    for fact in overrides:
        parts = fact.field_path.split(".")
        target: Any = source
        for part in parts[:-1]:
            target = target.get(part) if isinstance(target, dict) else None
        if isinstance(target, dict) and parts[-1] in target:
            value = next((value for value in (fact.value_bool, fact.value_date,
                         fact.value_parsed, fact.value_text) if value is not None), None)
            target[parts[-1]] = str(value) if isinstance(value, date) else value
    validated = validate_and_normalize(source)
    return canonical_to_normalized(validated.extraction, property_id,
                                   report_date=reports[entities[0].report_id].generated_date)


def reports_for_property(session, property_id):
    entities = active_entities(session, property_id)
    entity_reports = [entity.report_id for entity in entities]
    return session.query(dbm.Report).filter(or_(
        dbm.Report.id.in_(entity_reports), dbm.Report.duplicate_of.in_(entity_reports),
        dbm.Report.property_id == property_id,
    )).all()
