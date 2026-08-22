"""Retry-safe report-level orchestration for the whole-PDF pipeline."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy.orm import Session

from common.errors import ErrorCode
from common.storage import DocumentStorage, get_document_storage
from db import models as dbm
from identity.owners import persist_owner_profile
from identity.service import attach_report, persist_property_owners
from ops.db_budget import reserve_budget
from pipeline.orchestrator import Computation, Pipeline

from .classification import classify_pdf
from .normalizer import (
    canonical_to_normalized,
    identity_address,
    validate_and_normalize,
)
from .provider import (
    PermanentProviderError,
    ProviderAnalysis,
    ProviderError,
    ProviderTimeout,
    WholePdfProviderClient,
)
from .schemas import (
    OWNER_SCHEMA_VERSION,
    SCHEMA_VERSION,
    OwnerProfileExtraction,
    PropertyReportExtraction,
)

log = logging.getLogger(__name__)

TERMINAL_FAILURES = {
    "failed_provider", "failed_validation", "failed_computation", "unresolved_identity", "paused_budget",
}


class ReportAnalysisFailure(RuntimeError):
    """Permanent report-level failure after useful state has been persisted."""


def _session_factory(factory=None):
    if factory is not None:
        return factory
    from common.db import db_session
    return db_session


def _context(report: dbm.Report, *, job_id: UUID | None = None) -> dict:
    return {
        "batch_id": report.batch_id,
        "report_id": report.id,
        "property_id": report.property_id,
        "job_id": job_id,
        "document_path": report.file_path,
    }


def _get_or_create_extraction(session: Session, report_id: UUID) -> dbm.ReportExtraction:
    row = session.query(dbm.ReportExtraction).filter(
        dbm.ReportExtraction.report_id == report_id,
    ).first()
    if row is None:
        row = dbm.ReportExtraction(
            id=uuid4(), report_id=report_id, schema_version=SCHEMA_VERSION,
            status="analyzing", validation_issues=[],
        )
        session.add(row)
        session.flush()
    return row


def _reuse_duplicate_extraction(
    session: Session, report: dbm.Report, row: dbm.ReportExtraction,
    *, job_id: UUID | None = None,
) -> tuple[bool, UUID | None]:
    """Copy a completed immutable extraction onto a new batch-owned report reference."""
    if report.duplicate_of is None or row.raw_json is not None:
        return False, None
    source = session.query(dbm.ReportExtraction).filter(
        dbm.ReportExtraction.report_id == report.duplicate_of,
        dbm.ReportExtraction.status.in_(["complete", "unresolved_identity"]),
    ).first()
    if source is None:
        return False, None
    source_report = session.get(dbm.Report, report.duplicate_of)
    row.property_id = source.property_id
    row.schema_version = source.schema_version
    row.model = source.model
    row.raw_json = source.raw_json
    row.normalized_json = source.normalized_json
    row.validation_issues = source.validation_issues or []
    row.status = source.status
    row.input_tokens = 0
    row.output_tokens = 0
    row.cost_usd = Decimal(0)
    row.duration_ms = 0
    row.retry_count = 0
    report.property_id = source.property_id
    if source_report is not None:
        report.doc_kind = source_report.doc_kind
        report.classification_confidence = source_report.classification_confidence
    report.status = source.status
    report.failure_reason = (
        ErrorCode.IDENTITY_UNRESOLVED.value
        if source.status == "unresolved_identity" else None
    )
    batch = _refresh_batch(session, report.batch_id)
    log.info("immutable duplicate extraction reused", extra={
        **_context(report, job_id=job_id),
        "event": "analysis_persisted",
        "stage": "duplicate_reuse",
        "success": True,
        "source_report_id": report.duplicate_of,
        "batch_status_after": batch.status if batch else None,
        "report_status_after": report.status,
    })
    return True, source.property_id


def _refresh_batch(session: Session, batch_id: UUID | None) -> dbm.Batch | None:
    if batch_id is None:
        return None
    batch = session.get(dbm.Batch, batch_id)
    if batch is None:
        return None
    reports = session.query(dbm.Report).filter(dbm.Report.batch_id == batch_id).all()
    report_ids = [report.id for report in reports]
    extractions = session.query(dbm.ReportExtraction).filter(
        dbm.ReportExtraction.report_id.in_(report_ids),
    ).all() if report_ids else []
    statuses = {row.report_id: row.status for row in extractions}
    completed = sum(statuses.get(report.id) == "complete" for report in reports)
    failed = sum(statuses.get(report.id) in TERMINAL_FAILURES for report in reports)
    outstanding = max(0, len(reports) - completed - failed)
    status_before = batch.status
    batch.total_count = len(reports)
    batch.completed_count = completed
    batch.failed_count = failed
    batch.actual_cost_usd = sum(
        (row.cost_usd or Decimal(0) for row in extractions), Decimal(0),
    )
    if outstanding:
        batch.status = "computing" if any(
            statuses.get(report.id) == "computing" for report in reports
        ) else "analyzing"
    elif completed:
        batch.status = "complete"
    elif reports and all(statuses.get(report.id) == "unresolved_identity" for report in reports):
        batch.status = "unresolved_identity"
    elif reports:
        failure_status = next(
            (statuses.get(report.id) for report in reports if statuses.get(report.id) in TERMINAL_FAILURES),
            "failed",
        )
        batch.status = failure_status or "failed"
    session.flush()
    log.info("whole PDF batch status refreshed", extra={
        "event": "batch_status_changed" if status_before != batch.status else "batch_status_refreshed",
        "stage": "batch_refresh",
        "batch_id": batch_id,
        "batch_status_before": status_before,
        "batch_status_after": batch.status,
        "total_count": batch.total_count,
        "completed_count": batch.completed_count,
        "failed_count": batch.failed_count,
        "report_count": len(reports),
        "report_statuses": {
            status: sum(value == status for value in statuses.values())
            for status in sorted(set(statuses.values()))
        },
    })
    return batch


def _mark_failure(
    report_id: UUID, status: str, reason: str, *, raw_json: dict | None = None,
    issues: list[dict] | None = None, provider: ProviderAnalysis | None = None,
    factory=None,
) -> None:
    with _session_factory(factory)() as session:
        report = session.get(dbm.Report, report_id)
        if report is None:
            return
        extraction = _get_or_create_extraction(session, report_id)
        extraction.status = status
        extraction.raw_json = raw_json if raw_json is not None else extraction.raw_json
        extraction.validation_issues = issues or extraction.validation_issues or []
        if provider is not None:
            _set_provider_metadata(extraction, provider)
        report.status = status
        report.failure_reason = reason[:60]
        _refresh_batch(session, report.batch_id)
        batch = session.get(dbm.Batch, report.batch_id) if report.batch_id else None
        log.error("whole PDF report analysis failed", extra={
            **_context(report),
            "event": status,
            "stage": status.removeprefix("failed_"),
            "success": False,
            "report_status_after": report.status,
            "batch_status_after": batch.status if batch else None,
            "error_message": reason,
        })


def _set_provider_metadata(row: dbm.ReportExtraction, result: ProviderAnalysis) -> None:
    row.model = result.model
    row.input_tokens = result.input_tokens
    row.output_tokens = result.output_tokens
    row.cost_usd = result.cost_usd
    row.duration_ms = result.duration_ms
    row.retry_count = result.attempts - 1


def _update_property_fields(property_row: dbm.Property, extraction: PropertyReportExtraction) -> None:
    identity = extraction.property_identity
    details = extraction.property_details
    for name, value in (
        ("city", identity.city), ("state", identity.state), ("zip5", identity.zip5),
        ("fips_county", identity.fips), ("apn", identity.apn),
        ("property_type", details.property_type), ("beds", details.beds),
        ("baths", details.baths), ("sqft", details.sq_ft),
        ("lot_sqft", details.lot_sq_ft), ("year_built", details.year_built),
        ("units", details.units),
    ):
        if value is not None and getattr(property_row, name) is None:
            setattr(property_row, name, value)


def _page_for(path: str, extraction: PropertyReportExtraction, row: dict | None = None) -> tuple[int, float, str] | None:
    if row and row.get("source_page"):
        return int(row["source_page"]), float(row.get("confidence") or 0.75), "Canonical extraction"
    for reference in extraction.source_references:
        if reference.field_path == path and reference.source_page:
            return (
                reference.source_page,
                float(reference.confidence or 0.75),
                (reference.evidence or "Canonical extraction")[:200],
            )
    return None


def _replace_evidence(
    session: Session, report: dbm.Report, extraction: PropertyReportExtraction,
) -> int:
    """Optional provenance rows; canonical JSON remains the processing source."""
    session.query(dbm.ExtractedFact).filter(
        dbm.ExtractedFact.report_id == report.id,
        dbm.ExtractedFact.extraction_unit_id.is_(None),
        dbm.ExtractedFact.source_kind == "report",
    ).delete(synchronize_session=False)
    payload = extraction.model_dump(mode="json")
    inserted = 0

    def visit(value: Any, path: str, entity_type: str, local_id: str, row: dict | None = None) -> None:
        nonlocal inserted
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"source_page", "confidence"}:
                    continue
                visit(child, f"{path}.{key}" if path else key, entity_type, local_id, value)
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}.{index}", entity_type, f"{local_id}:{index}", child if isinstance(child, dict) else row)
            return
        if value is None:
            return
        source = _page_for(path, extraction, row)
        if source is None:
            return
        page_number, confidence, snippet = source
        value_parsed = Decimal(str(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        value_date = None
        if isinstance(value, str):
            try:
                value_date = date.fromisoformat(value)
            except ValueError:
                pass
        session.add(dbm.ExtractedFact(
            id=uuid4(), property_id=report.property_id, report_id=report.id,
            extraction_unit_id=None, entity_type=entity_type,
            entity_local_id=local_id, field_path=path, value_raw=str(value),
            value_parsed=value_parsed,
            value_text=value if isinstance(value, str) and value_date is None else None,
            value_date=value_date,
            value_bool=value if isinstance(value, bool) else None,
            page_number=page_number, snippet=snippet,
            extraction_confidence=Decimal(str(confidence)), source_kind="report",
            is_active=True,
        ))
        inserted += 1

    entity_by_block = {
        "property_identity": "property", "property_details": "property",
        "ownership": "property", "valuation": "valuation", "tax": "tax",
        "loans": "mortgage", "liens": "lien", "foreclosure": "foreclosure",
        "transaction_history": "property", "listing_history": "listing",
        "rental": "rental", "additional_facts": "property",
    }
    for block, entity_type in entity_by_block.items():
        visit(payload[block], block, entity_type, block)
    session.flush()
    return inserted


def _source_payload(row: dbm.ReportExtraction) -> dict | None:
    normalized = row.normalized_json or {}
    source = normalized.get("source") if isinstance(normalized, dict) else None
    return source if isinstance(source, dict) else None


def analyze_report(
    report_id: UUID, *, batch_id: UUID | None = None, job_id: UUID | None = None,
    provider: WholePdfProviderClient | None = None,
    storage: DocumentStorage | None = None,
    session_factory=None,
    compute: Callable[..., Computation] | None = None,
    identity_resolver: Callable[..., dbm.Property] = attach_report,
) -> UUID | None:
    """Analyze one report. Every persisted step is safe to resume after a retry."""
    factory = _session_factory(session_factory)
    storage = storage or get_document_storage()
    provider = provider or WholePdfProviderClient()

    with factory() as session:
        report = session.get(dbm.Report, report_id)
        if report is None:
            raise ReportAnalysisFailure(f"report {report_id} not found")
        if batch_id is not None and report.batch_id != batch_id:
            report.batch_id = batch_id
        extraction_row = _get_or_create_extraction(session, report_id)
        context = _context(report, job_id=job_id)
        reused, reused_property_id = _reuse_duplicate_extraction(
            session, report, extraction_row, job_id=job_id,
        )
        if reused:
            return reused_property_id
        if extraction_row.status == "complete" and extraction_row.property_id is not None:
            report.property_id = extraction_row.property_id
            report.status = "complete"
            report.failure_reason = None
            _refresh_batch(session, report.batch_id)
            log.info("completed extraction reused", extra={
                **context,
                "property_id": extraction_row.property_id,
                "event": "analysis_persisted",
                "stage": "duplicate_reuse",
                "success": True,
            })
            return extraction_row.property_id
        if (extraction_row.status == "complete" and report.doc_kind == "owner_profile"
                and isinstance(extraction_row.normalized_json, dict)):
            return None
        if extraction_row.status == "unresolved_identity" and _source_payload(extraction_row):
            report.status = "unresolved_identity"
            report.failure_reason = ErrorCode.IDENTITY_UNRESOLVED.value
            _refresh_batch(session, report.batch_id)
            return None
        report.status = "analyzing"
        report.failure_reason = None
        extraction_row.status = "analyzing"
        _refresh_batch(session, report.batch_id)
        source_payload = _source_payload(extraction_row)
        file_path = report.file_path
        log.info("whole PDF analysis job started", extra={
            **context,
            "event": "analysis_job_claimed",
            "stage": "analysis",
            "success": True,
        })

    provider_result: ProviderAnalysis | None = None
    if source_payload is None:
        # Reserve a conservative per-document estimate before making the
        # provider call. The reservation is atomic at the batch row and keeps
        # whole-PDF retries within the configured spend ceiling.
        if report.batch_id is not None:
            estimate = max(Decimal("0.01"), Decimal(str(report.page_count or 1)) * Decimal("0.01"))
            with factory() as budget_session:
                if not reserve_budget(budget_session, report.batch_id, estimate):
                    _mark_failure(report_id, "paused_budget", ErrorCode.BUDGET_PAUSED.value, factory=factory)
                    raise ReportAnalysisFailure("analysis paused by batch budget")
        try:
            with storage.materialize(file_path) as pdf_path:
                doc_kind, classification_confidence = classify_pdf(pdf_path)
                with factory() as classification_session:
                    classified_report = classification_session.get(dbm.Report, report_id)
                    if classified_report is not None:
                        classified_report.doc_kind = doc_kind
                        classified_report.classification_confidence = Decimal(str(classification_confidence))
                log.info("original PDF materialized", extra={
                    **context,
                    "event": "document_materialized",
                    "stage": "storage_read",
                    "success": True,
                    "storage_backend": "s3" if file_path.startswith("s3://") else "filesystem",
                })
                if "doc_kind" in inspect.signature(provider.analyze_pdf).parameters:
                    provider_result = provider.analyze_pdf(
                        pdf_path, doc_kind=doc_kind, log_context=context,
                    )
                else:
                    provider_result = provider.analyze_pdf(pdf_path, log_context=context)
            source_payload = provider_result.payload
        except PermanentProviderError as exc:
            _mark_failure(report_id, "failed_provider", "provider_rejected", factory=factory)
            raise ReportAnalysisFailure(str(exc)) from exc
        except (ProviderTimeout, ProviderError) as exc:
            _mark_failure(report_id, "failed_provider", "provider_failed", factory=factory)
            raise ReportAnalysisFailure(str(exc)) from exc

    with factory() as kind_session:
        kind_report = kind_session.get(dbm.Report, report_id)
        doc_kind = kind_report.doc_kind if kind_report is not None else "property_profile"

    if doc_kind == "owner_profile":
        try:
            owner_extraction = OwnerProfileExtraction.model_validate(source_payload)
        except ValidationError as exc:
            issues = [{
                "code": "schema_validation_failed",
                "path": ".".join(str(part) for part in error["loc"]),
                "message": error["msg"],
            } for error in exc.errors()]
            _mark_failure(report_id, "failed_validation", "schema_validation_failed",
                          raw_json=source_payload, issues=issues,
                          provider=provider_result, factory=factory)
            raise ReportAnalysisFailure("owner extraction failed schema validation") from exc
        with factory() as owner_session:
            owner_report = owner_session.get(dbm.Report, report_id)
            if owner_report is None:
                raise ReportAnalysisFailure(f"report {report_id} not found")
            owner_row, candidates = persist_owner_profile(
                owner_session, owner_extraction, report=owner_report,
            )
            extraction_row = _get_or_create_extraction(owner_session, report_id)
            extraction_row.schema_version = OWNER_SCHEMA_VERSION
            extraction_row.raw_json = source_payload
            extraction_row.normalized_json = {
                "owner_id": str(owner_row.id),
                "link_candidates": [{
                    "owner_id": str(candidate.owner_id),
                    "confidence": candidate.confidence,
                    "reasons": candidate.reasons,
                    "property_ids": [str(value) for value in candidate.property_ids],
                } for candidate in candidates],
            }
            extraction_row.status = "complete"
            if provider_result is not None:
                _set_provider_metadata(extraction_row, provider_result)
            owner_report.status = "complete"
            owner_report.failure_reason = None
            _refresh_batch(owner_session, owner_report.batch_id)
        return None

    try:
        validated = validate_and_normalize(source_payload)
    except ValidationError as exc:
        issues = [{
            "code": "schema_validation_failed",
            "path": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
        } for error in exc.errors()]
        _mark_failure(
            report_id, "failed_validation", "schema_validation_failed",
            raw_json=source_payload, issues=issues, provider=provider_result, factory=factory,
        )
        raise ReportAnalysisFailure("canonical extraction failed schema validation") from exc

    address = identity_address(validated.extraction)
    if address is None:
        _mark_failure(
            report_id, "unresolved_identity", ErrorCode.IDENTITY_UNRESOLVED.value,
            raw_json=source_payload, issues=validated.issues,
            provider=provider_result, factory=factory,
        )
        log.warning("property identity unresolved", extra={
            **context,
            "event": "identity_unresolved",
            "stage": "identity",
            "success": False,
            "address": validated.extraction.property_identity.full_address,
            "apn": validated.extraction.property_identity.apn,
        })
        return None

    with factory() as session:
        report = session.get(dbm.Report, report_id)
        if report is None:
            raise ReportAnalysisFailure(f"report {report_id} not found")
        extraction_row = _get_or_create_extraction(session, report_id)
        identity = validated.extraction.property_identity
        log.info("property identity evidence found", extra={
            **_context(report, job_id=job_id),
            "event": "property_identity_evidence_found",
            "stage": "identity",
            "address": address,
            "apn": identity.apn,
        })
        property_row = identity_resolver(
            session, report, address, apn=identity.apn, fips=identity.fips, zip5=identity.zip5,
        )
        created = bool(getattr(property_row, "identity_created", False))
        _update_property_fields(property_row, validated.extraction)
        normalized_record = canonical_to_normalized(
            validated.extraction, property_row.id, report_date=report.generated_date,
        )
        persist_property_owners(
            session,
            property_row.id,
            validated.extraction.ownership.owner_names,
            mailing_address=validated.extraction.ownership.mailing_address,
            is_absentee=normalized_record.ownership.is_absentee,
            ownership_start_date=normalized_record.ownership.ownership_start_date,
        )
        extraction_row.property_id = property_row.id
        extraction_row.raw_json = source_payload
        extraction_row.normalized_json = {
            "source": validated.normalized_source,
            "property": normalized_record.model_dump(mode="json"),
        }
        extraction_row.validation_issues = validated.issues
        extraction_row.status = "computing"
        if provider_result is not None:
            _set_provider_metadata(extraction_row, provider_result)
        report.status = "computing"
        report.failure_reason = None
        fact_count = _replace_evidence(session, report, validated.extraction)
        batch = _refresh_batch(session, report.batch_id)
        log.info("canonical extraction validated", extra={
            **_context(report, job_id=job_id),
            "event": "extraction_validated",
            "stage": "validation",
            "success": True,
            "validation_issue_count": len(validated.issues),
        })
        log.info("property resolved and report attached", extra={
            **_context(report, job_id=job_id),
            "event": "property_resolved",
            "stage": "identity",
            "success": True,
            "property_created": created,
            "fact_count": fact_count,
            "batch_status_after": batch.status if batch else None,
        })

    compute = compute or Pipeline().compute_normalized
    try:
        log.info("deterministic calculation started", extra={
            **context,
            "property_id": normalized_record.property_id,
            "event": "calculation_started",
            "stage": "calculation",
        })
        computation = compute(
            normalized_record,
            reason="whole_pdf_analysis",
            trace_context={**context, "property_id": normalized_record.property_id},
        )
    except Exception as exc:
        _mark_failure(
            report_id, "failed_computation", "calculation_failed",
            raw_json=source_payload, issues=validated.issues,
            provider=provider_result, factory=factory,
        )
        log.exception("deterministic calculation failed", extra={
            **context,
            "property_id": normalized_record.property_id,
            "event": "calculation_failed",
            "stage": "calculation",
            "success": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        })
        raise ReportAnalysisFailure("deterministic property calculation failed") from exc

    with factory() as session:
        report = session.get(dbm.Report, report_id)
        extraction_row = _get_or_create_extraction(session, report_id)
        extraction_row.status = "complete"
        report.status = "complete"
        report.failure_reason = None
        batch = _refresh_batch(session, report.batch_id)
        log.info("deterministic calculation completed", extra={
            **_context(report, job_id=job_id),
            "event": "calculation_completed",
            "stage": "calculation",
            "success": True,
            "underwriting_status": computation.underwriting.status,
        })
        log.info("whole PDF analysis persisted", extra={
            **_context(report, job_id=job_id),
            "event": "analysis_persisted",
            "stage": "persistence",
            "success": True,
            "batch_status_after": batch.status if batch else None,
            "report_status_after": report.status,
        })
        if batch is not None and batch.status == "complete":
            property_ids = [str(row[0]) for row in session.query(dbm.Report.property_id).filter(
                dbm.Report.batch_id == batch.id, dbm.Report.property_id.isnot(None),
            ).distinct().all()]
            log.info("batch completed with property analysis", extra={
                **_context(report, job_id=job_id),
                "event": "batch_completed",
                "stage": "final_state",
                "success": True,
                "final_status": batch.status,
                "property_ids": property_ids,
                "total_count": batch.total_count,
                "completed_count": batch.completed_count,
                "failed_count": batch.failed_count,
            })
    return normalized_record.property_id
