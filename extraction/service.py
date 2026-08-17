"""Extraction orchestration: budget gate, cost accounting, persistence, re-extraction.

Every DB interaction goes through an injected SQLAlchemy ``Session`` so tests
run offline (SQLite in memory or a fake). The budget gate calls
``ops.reserve_budget`` unchanged; a batch that hits its cap is paused, not
failed (spec §19).
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from common.errors import AcqError, ErrorCode
from common.storage import get_document_storage
from db.models import ExtractedFact, ExtractionUnit
from ops import reserve_budget

from .client import ExtractionResult, ProviderClient
from .flatten import flatten_payload
from .prompts import load_prompt, prompt_version
from .validation import GauntletOutcome, run_gauntlet

BudgetReserve = Callable[[Session, UUID, Decimal], bool]

# Rough pre-flight estimate: output tokens ≈ input / 4, frontier input pricing
# as the conservative bound. Refined by classification's token estimates.
_ESTIMATED_INPUT_PRICE_PER_TOKEN = Decimal("0.0000025")
_ESTIMATED_OUTPUT_PRICE_PER_TOKEN = Decimal("0.00001")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UnitInput:
    """Everything extraction needs for one unit; pipeline builds these from
    ``extraction_units`` rows plus the report header."""

    id: UUID
    report_id: UUID
    unit_type: str
    text: str
    page_start: int
    page_end: int
    property_id: UUID | None = None
    batch_id: UUID | None = None
    token_estimate: int = 0
    subject_address: str | None = None
    subject_apn: str | None = None


def estimate_cost(unit: UnitInput) -> Decimal:
    tokens = unit.token_estimate or len(unit.text) // 4
    estimated = tokens * _ESTIMATED_INPUT_PRICE_PER_TOKEN + (tokens // 4) * _ESTIMATED_OUTPUT_PRICE_PER_TOKEN
    return max(Decimal("0.000001"), estimated).quantize(Decimal("0.000001"))


def _default_page_text(unit: UnitInput) -> dict[int, str]:
    return {page: unit.text for page in range(unit.page_start, unit.page_end + 1)}


def _subject_line(unit: UnitInput) -> str:
    return (
        f"Subject: {unit.subject_address or 'unknown'} | APN: {unit.subject_apn or 'unknown'} "
        f"| Doc: {unit.unit_type} | Pages {unit.page_start}-{unit.page_end}"
    )


def persist_facts(
    session: Session,
    outcome: GauntletOutcome,
    *,
    property_id: UUID | None = None,
) -> dict[tuple[str, str], UUID]:
    """Insert active + inactive facts; returns (entity_local_id, field_path) → id."""
    ids: dict[tuple[str, str], UUID] = {}
    for fact, is_active in [(f, True) for f in outcome.active] + [(f, False) for f in outcome.inactive]:
        fact_id = uuid4()
        ids[(fact.entity_local_id, fact.field_path)] = fact_id
        session.add(ExtractedFact(
            id=fact_id,
            property_id=property_id,
            report_id=fact.report_id,
            extraction_unit_id=fact.extraction_unit_id,
            entity_type=fact.entity_type.value,
            entity_local_id=fact.entity_local_id,
            field_path=fact.field_path,
            value_raw=fact.value_raw,
            value_parsed=fact.value_parsed,
            value_text=fact.value_text,
            value_date=fact.value_date,
            value_bool=fact.value_bool,
            unit=fact.unit,
            as_of_date=fact.as_of_date,
            page_number=fact.page_number,
            snippet=fact.snippet,
            extraction_confidence=Decimal(str(fact.extraction_confidence)),
            null_reason=fact.null_reason.value if fact.null_reason else None,
            source_kind=fact.source_kind.value,
            is_active=is_active,
        ))
    session.flush()
    return ids


def record_unit_outcome(
    session: Session,
    unit_id: UUID,
    *,
    model: str | None,
    cost_usd: Decimal | None,
    status: str,
) -> None:
    session.execute(
        update(ExtractionUnit)
        .where(ExtractionUnit.id == unit_id)
        .values(model=model, prompt_version=prompt_version(), cost_usd=cost_usd, status=status)
    )


class ExtractionService:
    def __init__(self, provider: ProviderClient, *, reserve: BudgetReserve = reserve_budget):
        self.provider = provider
        self.reserve = reserve

    def _extract_drafts(
        self,
        unit: UnitInput,
        page_text_by_number: dict[int, str] | None,
    ) -> tuple[GauntletOutcome, ExtractionResult]:
        """Provider call + gauntlet, without persistence. Returns the gauntlet
        outcome and the public result; persistence is the caller's choice."""
        model = self.provider.model_for(unit.unit_type)
        context = {
            "batch_id": unit.batch_id,
            "report_id": unit.report_id,
            "unit_id": unit.id,
            "event": "extraction_request",
            "stage": "extraction_request",
            "model": model,
            "provider_host": urlparse(self.provider.base_url).hostname,
            "unit_type": unit.unit_type,
            "token_estimate": unit.token_estimate,
        }
        log.info("provider extraction request started", extra={**context, "success": True})
        try:
            response = self.provider.complete(
                unit.unit_type, unit.text, subject=_subject_line(unit), system_prompt=load_prompt()
            )
        except Exception as exc:
            details = exc.details if isinstance(exc, AcqError) else {}
            log.exception("provider extraction response failed", extra={
                **context,
                "event": "extraction_response",
                "success": False,
                "provider_status_code": details.get("provider_status_code"),
                "retry_count": details.get("retry_count", 0),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            })
            raise
        log.info("provider extraction response completed", extra={
            **context,
            "event": "extraction_response",
            "success": True,
            "model_used": response.model,
            "retry_count": max(0, response.attempts - 1),
            "provider_status_code": 200,
        })
        try:
            drafts, dropped = flatten_payload(
                unit.unit_type, response.payload,
                report_id=unit.report_id, extraction_unit_id=unit.id,
            )
            outcome = run_gauntlet(
                drafts, page_text_by_number or _default_page_text(unit), dropped=dropped,
            )
        except Exception as exc:
            log.exception("normalization and validation failed", extra={
                "event": "analysis_stage_failed",
                "stage": "normalization_validation",
                "success": False,
                "batch_id": unit.batch_id,
                "report_id": unit.report_id,
                "unit_id": unit.id,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            })
            raise
        log.info("normalization and validation completed", extra={
            "event": "analysis_stage_completed",
            "stage": "normalization_validation",
            "success": True,
            "batch_id": unit.batch_id,
            "report_id": unit.report_id,
            "unit_id": unit.id,
            "fact_count": len(outcome.active),
            "inactive_fact_count": len(outcome.inactive),
        })
        result = ExtractionResult(
            outcome.active, outcome.dropped, prompt_version(),
            inactive=outcome.inactive, counters=outcome.counters,
            model=response.model, cost_usd=response.cost_usd, usage=response.usage,
        )
        return outcome, result

    def _check_budget(self, session: Session, unit: UnitInput) -> None:
        # Budget gate first: a paused batch never reaches the provider (spec §19).
        if unit.batch_id is not None and not self.reserve(session, unit.batch_id, estimate_cost(unit)):
            raise AcqError(ErrorCode.BUDGET_PAUSED, f"batch {unit.batch_id} is over budget; paused")

    def extract_unit(
        self,
        unit: UnitInput,
        *,
        session: Session | None = None,
        page_text_by_number: dict[int, str] | None = None,
    ) -> ExtractionResult:
        if session is not None:
            self._check_budget(session, unit)
        outcome, result = self._extract_drafts(unit, page_text_by_number)
        if session is not None:
            persist_facts(session, outcome, property_id=unit.property_id)
            record_unit_outcome(session, unit.id, model=result.model, cost_usd=result.cost_usd, status="extracted")
        return result

    def reextract(
        self,
        session: Session,
        report_ids: Iterable[UUID],
        *,
        page_text_by_report: dict[UUID, dict[int, str]] | None = None,
        batch_id: UUID | None = None,
    ) -> list[ExtractionResult]:
        """Re-run extraction for reports from their stored page text (spec §19.5).

        New facts get the current ``prompt_version``; old facts are superseded
        (``is_active=False``, ``superseded_by`` set to the replacement fact when
        one exists) — never deleted, so versions stay comparable.
        """
        report_ids = list(report_ids)
        units = session.scalars(
            select(ExtractionUnit).where(ExtractionUnit.report_id.in_(report_ids)).order_by(ExtractionUnit.created_at)
        ).all()
        old_facts = session.scalars(
            select(ExtractedFact).where(ExtractedFact.report_id.in_(report_ids), ExtractedFact.is_active.is_(True))
        ).all()
        results: list[ExtractionResult] = []
        new_fact_ids: dict[tuple[UUID | None, str, str], UUID] = {}
        for row in units:
            if not row.text_path:
                continue
            text = get_document_storage().read_text(row.text_path)
            unit = UnitInput(
                id=row.id, report_id=row.report_id, unit_type=row.unit_type, text=text,
                page_start=row.page_start, page_end=row.page_end,
                batch_id=batch_id, token_estimate=row.token_estimate,
            )
            self._check_budget(session, unit)
            pages = (page_text_by_report or {}).get(row.report_id)
            outcome, result = self._extract_drafts(unit, pages)
            ids = persist_facts(session, outcome)
            record_unit_outcome(session, unit.id, model=result.model, cost_usd=result.cost_usd, status="extracted")
            results.append(result)
            for (entity_local_id, field_path), fact_id in ids.items():
                new_fact_ids[(row.report_id, entity_local_id, field_path)] = fact_id
        for old in old_facts:
            old.is_active = False
            old.superseded_by = new_fact_ids.get((old.report_id, old.entity_local_id, old.field_path))
        session.flush()
        return results
