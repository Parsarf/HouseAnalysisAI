"""Source-evidence lookups: extracted facts, field resolutions, report pages."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from contracts import ExplanationCandidate, ExplanationSource
from db import models as dbm


def _source_from_fact(fact: dbm.ExtractedFact, report: dict[UUID, dbm.Report] | None = None,
                      is_winner: bool = False, property_id: UUID | None = None) -> ExplanationSource:
    report_row = report.get(fact.report_id) if (report and fact.report_id) else None
    return ExplanationSource(
        fact_id=fact.id,
        report_id=fact.report_id,
        report_name=(f"{report_row.vendor or ''} {report_row.report_type or 'report'}".strip()
                     if report_row is not None else None) or None,
        vendor=report_row.vendor if report_row is not None else None,
        report_type=report_row.report_type if report_row is not None else None,
        source_kind=fact.source_kind,
        page_number=fact.page_number,
        snippet=fact.snippet or None,
        value_raw=fact.value_raw,
        value_parsed=str(fact.value_parsed) if fact.value_parsed is not None else None,
        extraction_confidence=float(fact.extraction_confidence),
        extraction_unit_id=fact.extraction_unit_id,
        ocr_applied=bool(report_row.ocr_applied) if report_row is not None else False,
        is_active=bool(fact.is_active),
        is_superseded=fact.superseded_by is not None,
        is_winner=is_winner,
        field_path=fact.field_path,
        source_url=_source_url(property_id, fact),
    )


def _source_url(property_id: UUID | None, fact: dbm.ExtractedFact) -> str | None:
    if fact.report_id is None:
        return None
    url = f"/api/reports/{fact.report_id}/source?page={max(1, fact.page_number)}"
    if property_id is not None:
        url += f"&property_id={property_id}&fact_id={fact.id}"
    return url


def _reports_by_id(session: Session, ids: set[UUID]) -> dict[UUID, dbm.Report]:
    if not ids:
        return {}
    return {row.id: row for row in session.query(dbm.Report).filter(dbm.Report.id.in_(ids)).all()}


def sources_for_property(session: Session, property_id: UUID,
                         entity_types: set[str], path_fragments: tuple[str, ...] = (),
                         limit: int = 60) -> list[ExplanationSource]:
    """All active extracted facts of the given entity types that back this area.

    This is the honest "which document text supports these numbers" list: the
    facts returned are the rows the normalization/engines consumed.
    """
    query = (session.query(dbm.ExtractedFact)
             .filter(dbm.ExtractedFact.property_id == property_id,
                     dbm.ExtractedFact.is_active.is_(True)))
    if entity_types:
        query = query.filter(dbm.ExtractedFact.entity_type.in_(entity_types))
    rows = query.order_by(dbm.ExtractedFact.field_path, dbm.ExtractedFact.page_number).limit(limit).all()
    if path_fragments:
        rows = [row for row in rows
                if any(fragment in (row.field_path or "") for fragment in path_fragments)]
    reports = _reports_by_id(session, {row.report_id for row in rows if row.report_id})
    winners = _winning_fact_ids(session, property_id)
    return [_source_from_fact(row, reports, is_winner=row.id in winners, property_id=property_id)
            for row in rows]


def sources_for_fact_ids(session: Session, property_id: UUID,
                         fact_ids: list[UUID]) -> list[ExplanationSource]:
    rows = session.query(dbm.ExtractedFact).filter(dbm.ExtractedFact.id.in_(fact_ids)).all() if fact_ids else []
    reports = _reports_by_id(session, {row.report_id for row in rows if row.report_id})
    winners = _winning_fact_ids(session, property_id)
    order = {fid: index for index, fid in enumerate(fact_ids)}
    rows.sort(key=lambda row: order.get(row.id, 10**9))
    return [_source_from_fact(row, reports, is_winner=row.id in winners, property_id=property_id)
            for row in rows]


def candidates_for_field(session: Session, property_id: UUID, field_path: str) -> tuple[list[ExplanationCandidate], str | None, str | None]:
    """Competing values for one normalized field path, winner flagged."""
    resolution = (session.query(dbm.FieldResolution)
                  .filter(dbm.FieldResolution.property_id == property_id,
                          dbm.FieldResolution.field_path == field_path)
                  .first())
    facts = (session.query(dbm.ExtractedFact)
             .filter(dbm.ExtractedFact.property_id == property_id,
                     dbm.ExtractedFact.is_active.is_(True))
             .all())
    matching = [fact for fact in facts
                if (fact.field_path or "").endswith(field_path.split(".")[-1])
                and fact.value_parsed is not None]
    if not matching:
        return [], None, None
    matching.sort(key=lambda fact: (fact.extraction_confidence), reverse=True)
    reports = _reports_by_id(session, {fact.report_id for fact in matching if fact.report_id})
    candidates = []
    for fact in matching[:6]:
        is_winner = resolution is not None and resolution.winning_fact_id == fact.id
        candidates.append(ExplanationCandidate(
            value=fact.value_parsed,
            display_value=str(fact.value_parsed),
            confidence=fact.extraction_confidence,
            source_kind=fact.source_kind,
            origin="extracted",
            is_winner=is_winner,
            reason="winning fact per normalization precedence" if is_winner else
                   "competing extraction from another page/report; lower precedence score",
            source=_source_from_fact(fact, reports, is_winner=is_winner, property_id=property_id),
        ))
    method = resolution.method if resolution is not None else None
    reason = ("Higher-precedence source: newer evidence with higher extraction confidence won."
              if method else None)
    return candidates, method, reason


def _winning_fact_ids(session: Session, property_id: UUID) -> set[UUID]:
    rows = (session.query(dbm.FieldResolution.winning_fact_id)
            .filter(dbm.FieldResolution.property_id == property_id,
                    dbm.FieldResolution.winning_fact_id.is_not(None))
            .all())
    return {row[0] for row in rows}


def load_report_page(session: Session, report_id: UUID, page: int) -> dict:
    """Page text for the View-source viewer (mirrors the chat grounding path)."""
    import fitz

    from common.storage import get_document_storage

    report = session.get(dbm.Report, report_id)
    if report is None:
        raise LookupError("report not found")
    storage = get_document_storage()
    with storage.materialize(report.file_path) as path, fitz.open(path) as document:
        page_count = len(document)
        number = max(1, min(page, page_count))
        text = document[number - 1].get_text()
    return {
        "report_id": str(report_id),
        "page": number,
        "page_count": page_count,
        "text": text,
        "vendor": report.vendor,
        "report_type": report.report_type,
    }
