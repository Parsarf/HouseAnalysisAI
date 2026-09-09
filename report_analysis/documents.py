"""Resumable, page-accounted, multi-entity PDF analysis.

Each provider request is checkpointed separately. Old published entities stay active
until their replacements have validated and computed successfully.
"""
from __future__ import annotations

import hashlib
import inspect
import logging
import tempfile
from contextlib import contextmanager, nullcontext
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, text

from common.settings import settings
from common.storage import get_document_storage
from db import models as dbm
from identity.owners import persist_owner_profile
from identity.service import (
    confirm_owner_link,
    normalize_address,
    normalize_apn,
    persist_property_owners,
)
from ops.db_budget import reserve_budget

from .document_identity import resolve_identity
from .document_schemas import DISCOVERY_PROMPT, VERSION, Discovery
from .normalizer import canonical_to_normalized, identity_address, validate_and_normalize
from .provider import (
    OWNER_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    PermanentProviderError,
    ProviderError,
    ProviderIncompleteError,
    WholePdfProviderClient,
)
from .read_model import load_record, merge_sources, source_report_id
from .schemas import OwnerProfileExtraction, canonical_schema, owner_schema
from .service import _replace_evidence, _update_property_fields

log = logging.getLogger(__name__)
TERMINAL = {"complete", "partial", "needs_review", "failed", "paused_budget", "no_properties"}


class BudgetPaused(RuntimeError):
    pass


def _factory(factory):
    if factory is None:
        from common.db import db_session
        return db_session
    return factory


@contextmanager
def _run_lock(factory, report_id):
    # Transaction-scoped advisory lock survives the short checkpoint transactions;
    # process crashes release it. The caller can safely replay the same job.
    with factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            key = int.from_bytes(hashlib.sha256(str(report_id).encode()).digest()[:8], "big", signed=True)
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        yield


def _heartbeat(session, job_id):
    if job_id is not None:
        job = session.get(dbm.Job, job_id)
        if job is not None:
            job.locked_at = dbm.now()


def refresh_batches(session, report_id):
    reports = session.query(dbm.Report).filter(
        (dbm.Report.id == report_id) | (dbm.Report.duplicate_of == report_id),
    ).all()
    for batch_id in {report.batch_id for report in reports if report.batch_id}:
        batch = session.get(dbm.Batch, batch_id)
        rows = session.query(dbm.Report).filter(dbm.Report.batch_id == batch_id).all()
        states = [row.status for row in rows]
        batch.total_count = len(rows)
        batch.completed_count = sum(status in {"complete", "no_properties"} for status in states)
        batch.failed_count = sum(status in {"partial", "needs_review", "failed", "paused_budget"}
                                 or status.startswith("failed_") for status in states)
        if "paused_budget" in states:
            batch.status = "paused_budget"
        elif any(status not in TERMINAL and not status.startswith("failed_") for status in states):
            batch.status = "analyzing"
        elif batch.failed_count:
            batch.status = "partial" if batch.completed_count or "partial" in states else "needs_review"
        else:
            batch.status = "complete"
        canonical_ids = {source_report_id(session, row) for row in rows}
        # Charges belong to the batch that owned the source request, not duplicate references.
        owned = {row.id for row in rows if row.id in canonical_ids}
        batch.actual_cost_usd = session.query(func.coalesce(func.sum(dbm.DocumentAnalysisRun.cost_usd), 0)).filter(
            dbm.DocumentAnalysisRun.report_id.in_(owned),
        ).scalar()


def _set_status(factory, run_id, status, issues=None):
    with factory() as session:
        run = session.get(dbm.DocumentAnalysisRun, run_id)
        run.status = status
        if issues is not None:
            run.issues = issues
        report = session.get(dbm.Report, run.report_id)
        for row in session.query(dbm.Report).filter(
            (dbm.Report.id == report.id) | (dbm.Report.duplicate_of == report.id),
        ).all():
            row.status = status
            row.failure_reason = None if status in {"complete", "no_properties"} else status
        session.flush()
        refresh_batches(session, report.id)


def _request(factory, run_id, key, pages, pdf, provider, *, schema, instruction,
             validate, job_id=None):
    with factory() as session:
        run = session.get(dbm.DocumentAnalysisRun, run_id)
        report = session.get(dbm.Report, run.report_id)
        row = session.query(dbm.DocumentAnalysisChunk).filter_by(run_id=run_id, key=key).first()
        if row and row.status == "complete":
            return validate(row.payload)
        if row is None:
            row = dbm.DocumentAnalysisChunk(run_id=run_id, key=key, pages=pages)
            session.add(row)
            session.flush()
        row_id = row.id
        estimate = Decimal("0.02") * len(pages)
        budget_batch_id = run.budget_batch_id or report.batch_id
        if budget_batch_id and not reserve_budget(session, budget_batch_id, estimate):
            raise BudgetPaused("Document analysis budget exhausted")
        row.status = "running"
        row.attempts += 1
        batch_id = budget_batch_id
        _heartbeat(session, job_id)
    result = None
    succeeded = False
    try:
        result = provider.analyze_pdf(pdf, schema=schema, instruction=instruction,
                                      log_context={"run_id": str(run_id), "chunk_key": key})
        # Save provider output and cost even when validation fails; it remains auditable.
        value = validate(result.payload)
        succeeded = True
    except (ProviderError, PermanentProviderError, ValidationError, ValueError) as exc:
        with factory() as session:
            row = session.get(dbm.DocumentAnalysisChunk, row_id)
            row.status = "failed"
            row.error = str(exc)[:2000]
        raise
    else:
        return value
    finally:
        with factory() as session:
            row = session.get(dbm.DocumentAnalysisChunk, row_id)
            if result is not None:
                row.payload = result.payload
                row.cost_usd += result.cost_usd
                run = session.get(dbm.DocumentAnalysisRun, run_id)
                run.cost_usd += result.cost_usd
            if succeeded:
                row.status = "complete"
                row.error = None
            # Unknown transport outcomes keep the reservation; known responses settle it.
            if batch_id and result is not None:
                batch = session.get(dbm.Batch, batch_id)
                batch.spent_usd = max(Decimal(0), batch.spent_usd + result.cost_usd - estimate)
            _heartbeat(session, job_id)


def _validate_discovery(payload, page_count):
    result = Discovery.model_validate(payload)
    if sorted(page.page for page in result.pages) != list(range(1, page_count + 1)):
        raise ValueError("Discovery did not account for every page exactly once")
    keys = {entity.key for entity in result.entities}
    if len(keys) != len(result.entities):
        raise ValueError("Discovery returned duplicate entity keys")
    pages_by_number = {page.page: page for page in result.pages}
    entities_by_key = {entity.key: entity for entity in result.entities}
    for entity in result.entities:
        if not entity.pages or any(page < 1 or page > page_count for page in entity.pages):
            raise ValueError("Entity has invalid source pages")
        if any(entity.key not in pages_by_number[page].entity_keys for page in entity.pages):
            raise ValueError("Entity and page links are inconsistent")
    for page in result.pages:
        if not set(page.entity_keys) <= keys:
            raise ValueError("Page references an unknown entity")
        if any(page.page not in entities_by_key[key].pages for key in page.entity_keys):
            raise ValueError("Page and entity links are inconsistent")
        if page.status == "entities" and not page.entity_keys:
            raise ValueError("Property-bearing page has no entities")
    return result


def _entity_key(entity, chunk_key):
    if entity.kind == "owner":
        # Names alone cannot establish shared owner identity. Keep profiles independent.
        identity = f"owner:{chunk_key}:{entity.key}"
    elif entity.address and (entity.zip5 or (entity.city and entity.state)):
        identity = normalize_address(entity.address, entity.zip5).address_key
        if not entity.zip5:
            identity += f"|{entity.city or ''}|{entity.state or ''}".upper()
    elif entity.apn and entity.jurisdiction:
        identity = f"parcel:{entity.jurisdiction}:{normalize_apn(entity.apn)}"
    else:
        identity = f"unresolved:{chunk_key}:{entity.key}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _remap_pages(payload, pages):
    if isinstance(payload, dict):
        result = {}
        for key, value in payload.items():
            if key == "source_page" and value is not None:
                if not isinstance(value, int) or not 1 <= value <= len(pages):
                    raise ValueError("Extracted evidence references a page outside the attachment")
                result[key] = pages[value - 1]
            else:
                result[key] = _remap_pages(value, pages)
        return result
    if isinstance(payload, list):
        return [_remap_pages(value, pages) for value in payload]
    return payload


def _pdf_subset(document, pages, root, key, region=None):
    import fitz
    path = root / f"{key}.pdf"
    with fitz.open() as subset:
        for page in pages:
            if region is None:
                subset.insert_pdf(document, from_page=page - 1, to_page=page - 1)
            else:
                source = document[page - 1]
                rect = source.rect
                clip = fitz.Rect(rect.x0, rect.y0 + rect.height * region[0],
                                 rect.x1, rect.y0 + rect.height * region[1])
                pixmap = source.get_pixmap(clip=clip, dpi=200)
                output = subset.new_page(width=clip.width, height=clip.height)
                output.insert_image(output.rect, pixmap=pixmap)
        subset.save(path, garbage=4, deflate=True)
    return path


def _discover(document, pages, root, factory, run_id, provider, job_id, region=None, depth=0):
    key = "discover:" + "-".join(map(str, pages))
    if region:
        key += f":region:{region[0]}:{region[1]}"
    path = _pdf_subset(document, pages, root, hashlib.sha256(key.encode()).hexdigest(), region)
    failure: Exception
    try:
        if path.stat().st_size > settings.pdf_chunk_max_bytes:
            raise ValueError("PDF chunk exceeds configured request size")
        discovery = _request(factory, run_id, key, pages, path, provider,
                             schema=Discovery.model_json_schema(), instruction=DISCOVERY_PROMPT,
                             validate=lambda payload: _validate_discovery(payload, len(pages)),
                             job_id=job_id)
        if any(page.status == "unreadable" for page in discovery.pages):
            raise ValueError("Retry unreadable pages with smaller visual context")
        return [(key, pages, path, discovery)], []
    except BudgetPaused:
        raise
    except (PermanentProviderError, ProviderError) as exc:
        if not isinstance(exc, ProviderIncompleteError):
            raise
        failure = exc
    except (ValidationError, ValueError) as exc:
        failure = exc
    if len(pages) > 1:
        midpoint = len(pages) // 2
        left, left_issues = _discover(document, pages[:midpoint], root, factory, run_id, provider, job_id)
        right, right_issues = _discover(document, pages[midpoint:], root, factory, run_id, provider, job_id)
        return left + right, left_issues + right_issues
    if depth < 2:
        top, bottom = region or (0.0, 1.0)
        middle = (top + bottom) / 2
        overlap = (bottom - top) * 0.05
        left, left_issues = _discover(document, pages, root, factory, run_id, provider, job_id,
                                      (top, middle + overlap), depth + 1)
        right, right_issues = _discover(document, pages, root, factory, run_id, provider, job_id,
                                        (middle - overlap, bottom), depth + 1)
        return left + right, left_issues + right_issues
    return [], [{"code": "unreadable_page", "pages": pages, "message": str(failure)[:500]}]


def _extract_fragment(discovered, pages, path, factory, run_id, chunk_key, provider, job_id):
    owner = discovered.kind == "owner"
    instruction = (OWNER_SYSTEM_PROMPT if owner else SYSTEM_PROMPT) + "\n" + (
        "Extract ONLY this entity; other properties/owners belong to their own records. "
        "Never copy a comparable's price, a different parcel's debt, or owner-only liens into this entity. "
        "Read all its supporting information in this attachment, including tables and scanned pages. "
        "Use local attachment page numbers starting at 1. Identity and target evidence:\n"
        + discovered.model_dump_json()
    )
    validate = OwnerProfileExtraction.model_validate if owner else lambda payload: validate_and_normalize(payload).extraction
    extracted = _request(factory, run_id, f"extract:{chunk_key}:{discovered.key}"[:255], pages,
                         path, provider, schema=owner_schema() if owner else canonical_schema(),
                         instruction=instruction, validate=validate, job_id=job_id)
    payload = _remap_pages(extracted.model_dump(mode="json"), pages)
    if not owner:
        identity = extracted.property_identity
        if discovered.apn and identity.apn and normalize_apn(discovered.apn) != normalize_apn(identity.apn):
            raise ValueError("Extracted parcel does not match the discovered target")
        if discovered.address and identity.address_line1:
            left = normalize_address(discovered.address, discovered.zip5).address_key
            right = normalize_address(identity.address_line1, identity.zip5 or discovered.zip5).address_key
            if left != right:
                raise ValueError("Extracted address/unit does not match the discovered target")
    return payload


def analyze_document(report_id: UUID, *, batch_id=None, job_id=None, run_id=None,
                     provider=None, storage=None, session_factory=None, compute=None,
                     identity_resolver=None):
    factory = _factory(session_factory)
    provider = provider or WholePdfProviderClient()
    storage = storage or get_document_storage()
    with factory() as session:
        report = session.get(dbm.Report, report_id)
        if report is None:
            raise ValueError("Report not found")
        canonical_id = source_report_id(session, report)
    with _run_lock(factory, canonical_id):
        with factory() as session:
            report = session.get(dbm.Report, canonical_id)
            run = session.get(dbm.DocumentAnalysisRun, run_id) if run_id else session.query(
                dbm.DocumentAnalysisRun,
            ).filter_by(report_id=canonical_id, version=VERSION).order_by(
                dbm.DocumentAnalysisRun.generation.desc(),
            ).first()
            if run is not None and run.report_id != canonical_id:
                raise ValueError("Analysis run does not belong to this report")
            if run is None:
                generation = session.query(func.max(dbm.DocumentAnalysisRun.generation)).filter_by(report_id=canonical_id).scalar() or 0
                run = dbm.DocumentAnalysisRun(id=uuid4(), report_id=canonical_id,
                                             generation=generation + 1, version=VERSION)
                session.add(run)
                session.flush()
            run_id = run.id
            if run.status in {"complete", "no_properties"}:
                status = run.status
                _set_status(factory, run_id, status)
                return run_id
            run.status = "analyzing"
            file_path = report.file_path
        try:
            with storage.materialize(file_path) as pdf_path, tempfile.TemporaryDirectory(prefix="acq-pdf-") as tmp:
                import fitz
                with fitz.open(pdf_path) as document:
                    if document.needs_pass:
                        raise ValueError("Password-protected PDF: upload an unlocked copy")
                    if len(document) == 0:
                        raise ValueError("PDF contains no pages")
                    with factory() as session:
                        session.get(dbm.DocumentAnalysisRun, run_id).page_count = len(document)
                        session.get(dbm.Report, canonical_id).page_count = len(document)
                    chunks, issues = [], []
                    size = max(2, settings.pdf_chunk_pages)
                    for start in range(1, len(document) + 1, size - 1):
                        pages = list(range(start, min(start + size, len(document) + 1)))
                        found, errors = _discover(document, pages, Path(tmp), factory, run_id, provider, job_id)
                        chunks.extend(found)
                        issues.extend(errors)
                        if pages[-1] == len(document):
                            break
                    coverage: dict[int, dict] = {}
                    grouped: dict[str, dict[str, Any]] = {}
                    coverage_priority = {
                        "unreadable": 0, "blank": 1, "irrelevant": 1,
                        "supporting": 2, "entities": 3,
                    }
                    role_priority = {"comparable": 0, "reference": 1, "listing": 2,
                                     "subject": 3, "owner": 3}
                    for chunk_key, pages, path, discovery in chunks:
                        for page in discovery.pages:
                            original = pages[page.page - 1]
                            item = {**page.model_dump(), "page": original}
                            previous = coverage.get(original)
                            if previous is None or coverage_priority[item["status"]] > coverage_priority[previous["status"]]:
                                coverage[original] = item
                        for entity in discovery.entities:
                            entity_key = _entity_key(entity, chunk_key)
                            group = grouped.setdefault(entity_key, {"entity": entity, "payloads": [], "pages": set(), "issues": [], "fragments": []})
                            if role_priority[entity.role] > role_priority[group["entity"].role]:
                                group["entity"] = entity
                            group["pages"].update(pages[p - 1] for p in entity.pages)
                            group["fragments"].append((entity, pages, path, chunk_key))
                    for number in range(1, len(document) + 1):
                        coverage.setdefault(number, {"page": number, "status": "unreadable", "entity_keys": [], "reason": "Analysis failed"})
                    with factory() as session:
                        run = session.get(dbm.DocumentAnalysisRun, run_id)
                        run.coverage = [coverage[number] for number in sorted(coverage)]
                        report = session.get(dbm.Report, canonical_id)
                        kinds = {group["entity"].kind for group in grouped.values()}
                        report.doc_kind = "mixed" if len(kinds) > 1 else "owner_profile" if "owner" in kinds else "property_profile"
                    for key, group in grouped.items():
                        for entity, pages, path, chunk_key in group["fragments"]:
                            try:
                                payload = _extract_fragment(entity, pages, path, factory, run_id, chunk_key, provider, job_id)
                                group["payloads"].append(payload)
                            except BudgetPaused:
                                group["issues"].append({"code": "paused_budget"})
                                _publish_entity(factory, canonical_id, run_id, key, group, compute, identity_resolver)
                                raise
                            except PermanentProviderError:
                                raise
                            except (ProviderError, ValidationError, ValueError) as exc:
                                group["issues"].append({"code": "entity_extraction_failed", "message": str(exc)[:500]})
                        _publish_entity(factory, canonical_id, run_id, key, group, compute, identity_resolver)
                    issues.extend({"code": "unreadable_page", "pages": [number]}
                                  for number, page in coverage.items() if page["status"] == "unreadable")
                    with factory() as session:
                        entities = session.query(dbm.ReportEntityExtraction).filter_by(run_id=run_id).all()
                        good = sum(entity.status == "complete" for entity in entities)
                        incomplete = bool(issues) or any(entity.status != "complete" or entity.issues for entity in entities)
                        status = ("partial" if good else "needs_review") if incomplete else ("complete" if entities else "no_properties")
                        # Do not silently detach old data when a new inventory disagrees.
                        current_ids = {entity.property_id for entity in entities if entity.property_id}
                        old = session.query(dbm.ReportEntityExtraction).filter(
                            dbm.ReportEntityExtraction.report_id == canonical_id,
                            dbm.ReportEntityExtraction.run_id != run_id,
                            dbm.ReportEntityExtraction.active.is_(True),
                        ).all()
                        if any(entity.property_id and entity.property_id not in current_ids for entity in old):
                            issues.append({"code": "previous_assignment_not_rediscovered"})
                            status = "partial" if good else "needs_review"
                    _set_status(factory, run_id, status, issues)
        except BudgetPaused as exc:
            _set_status(factory, run_id, "paused_budget", [{"code": "budget", "message": str(exc)}])
        except (OSError, RuntimeError, ValueError) as exc:
            log.exception("document analysis failed", extra={"run_id": str(run_id)})
            _set_status(factory, run_id, "failed", [{"code": "document_failed", "message": str(exc)[:500]}])
        return run_id


def _publish_entity(factory, report_id, run_id, key, group, compute, resolver):
    from pipeline.orchestrator import Pipeline
    discovered = group["entity"]
    source, conflicts = merge_sources(group["payloads"])
    identity_conflict = any(issue["path"].startswith("property_identity.") for issue in conflicts)
    with factory() as session:
        row = session.query(dbm.ReportEntityExtraction).filter_by(run_id=run_id, entity_key=key).first()
        if row and row.status == "complete":
            return
        if row is None:
            row = dbm.ReportEntityExtraction(id=uuid4(), run_id=run_id, report_id=report_id,
                                            entity_key=key, kind=discovered.kind, role=discovered.role)
            session.add(row)
        row.source_pages = sorted(group["pages"])
        row.raw_json = source or None
        row.issues = group["issues"] + conflicts
        row.status = "needs_review"
        session.flush()
        entity_id = row.id
    if not source or group["issues"] or identity_conflict:
        return
    try:
        with factory() as session:
            row = session.get(dbm.ReportEntityExtraction, entity_id)
            report = session.get(dbm.Report, report_id)
            if discovered.kind == "owner":
                extraction = OwnerProfileExtraction.model_validate(source)
                previous_profiles = session.query(dbm.ReportEntityExtraction).filter(
                    dbm.ReportEntityExtraction.report_id == report_id,
                    dbm.ReportEntityExtraction.kind == "owner",
                    dbm.ReportEntityExtraction.active.is_(True),
                ).all()
                previous_profile = next((item for item in previous_profiles
                                         if (item.raw_json or {}).get("person") == source.get("person")
                                         and source.get("person", {}).get("full_name")
                                         and source.get("person", {}).get("mailing_address")), None)
                if previous_profile and previous_profile.raw_json == source:
                    row.owner_id = previous_profile.owner_id
                    row.normalized_json = previous_profile.normalized_json
                    previous_profile.active = False
                    row.status, row.active = "complete", True
                    return
                owner, candidates = persist_owner_profile(session, extraction, report=report)
                if previous_profile and previous_profile.owner_id:
                    owner = confirm_owner_link(session, owner.id, previous_profile.owner_id)
                    previous_profile.active = False
                row.owner_id = owner.id
                row.normalized_json = {"owner_id": str(owner.id), "link_candidates": [
                    {"owner_id": str(candidate.owner_id), "confidence": candidate.confidence,
                     "reasons": candidate.reasons, "property_ids": [str(value) for value in candidate.property_ids]}
                    for candidate in candidates]}
                if previous_profile and (previous_profile.normalized_json or {}).get("linked"):
                    row.normalized_json = {**row.normalized_json, "linked": True}
                row.status, row.active = "complete", True
                return
            validated = validate_and_normalize(source)
            identity = validated.extraction.property_identity
            address = identity_address(validated.extraction)
            if address is None:
                if identity.apn and identity.fips:
                    prop = session.query(dbm.Property).filter_by(
                        apn_key=normalize_apn(identity.apn, identity.fips), merged_into_id=None,
                    ).one_or_none()
                else:
                    prop = None
                if prop is None:
                    raise ValueError("Property identity unresolved; no grounded street address")
            else:
                resolve = resolver or resolve_identity
                kwargs = {"apn": identity.apn, "fips": identity.fips, "zip5": identity.zip5}
                if "city" in inspect.signature(resolve).parameters:
                    kwargs.update(city=identity.city, state=identity.state)
                previous = report.property_id
                prop = resolve(session, report, address, **kwargs)
                report.property_id = previous
            row.property_id = prop.id
            same_run = session.query(dbm.ReportEntityExtraction).filter(
                dbm.ReportEntityExtraction.run_id == run_id,
                dbm.ReportEntityExtraction.property_id == prop.id,
                dbm.ReportEntityExtraction.active.is_(True),
                dbm.ReportEntityExtraction.id != row.id,
            ).all()
            if same_run:
                combined_source, combined_issues = merge_sources([source] + [item.raw_json for item in same_run if item.raw_json])
                validated = validate_and_normalize(combined_source)
                row.raw_json = combined_source
                row.issues = row.issues + combined_issues
                row.source_pages = sorted(set(row.source_pages).union(*(item.source_pages for item in same_run)))
            row.issues = row.issues + validated.issues
            row.normalized_json = {"source": validated.normalized_source,
                                   "property": canonical_to_normalized(validated.extraction, prop.id,
                                                                       report_date=report.generated_date).model_dump(mode="json")}
            # Stage, compute, then activate. Prior successful results remain readable.
            row.status = "computing"
            property_id = prop.id
            session.flush()
            staged = load_record(session, prop.id, pending=row)
        with factory() as session:
            row = session.get(dbm.ReportEntityExtraction, entity_id)
            from pipeline.store import SqlStore
            if session.get_bind().dialect.name == "postgresql":
                SqlStore(session).acquire_property_lock(property_id)
            prop = session.query(dbm.Property).filter_by(id=property_id).with_for_update().one()
            report = session.get(dbm.Report, report_id)
            # Calculation tables and active evidence switch in the SAME transaction.
            # Re-read under the property lock to include concurrent reports.
            staged = load_record(session, property_id, pending=row)
            if staged is None:
                raise ValueError("No validated property evidence available")
            calculate = compute or Pipeline(store_factory=lambda: nullcontext(SqlStore(session))).compute_normalized
            calculate(staged, reason="document_analysis",
                      trace_context={"report_id": report_id, "entity_id": entity_id})
            older = session.query(dbm.ReportEntityExtraction).filter(
                dbm.ReportEntityExtraction.report_id == report_id,
                dbm.ReportEntityExtraction.property_id == property_id,
                dbm.ReportEntityExtraction.id != entity_id,
                dbm.ReportEntityExtraction.active.is_(True),
            ).all()
            for previous in older:
                previous.active = False
                session.query(dbm.ExtractedFact).filter(
                    dbm.ExtractedFact.report_id == report_id,
                    dbm.ExtractedFact.property_id == property_id,
                    dbm.ExtractedFact.source_kind == "report",
                    dbm.ExtractedFact.entity_local_id.like(f"{previous.id}:%"),
                ).update({"is_active": False}, synchronize_session=False)
            row.status, row.active = "complete", True
            _update_property_fields(prop, validated.extraction)
            _replace_evidence(session, report, validated.extraction, property_id=property_id, entity_id=entity_id)
            persist_property_owners(session, property_id, validated.extraction.ownership.owner_names,
                                    mailing_address=validated.extraction.ownership.mailing_address,
                                    is_absentee=staged.ownership.is_absentee,
                                    ownership_start_date=staged.ownership.ownership_start_date)
            session.flush()
            combined = load_record(session, property_id)
            if combined is not None:
                row.normalized_json = {"source": validated.normalized_source,
                                       "property": combined.model_dump(mode="json")}
    except Exception as exc:
        log.exception("property entity processing failed", extra={"entity_id": str(entity_id)})
        with factory() as session:
            row = session.get(dbm.ReportEntityExtraction, entity_id)
            row.status = "needs_review" if isinstance(exc, (ValueError, ValidationError)) else "failed"
            row.issues = row.issues + [{"code": "entity_failed", "message": str(exc)[:500]}]
