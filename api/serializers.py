"""Row → contract serialization for the API.

All responses are dumped with ``model_dump(mode="json")`` so ``Decimal`` leaves
the process as a string — money is never serialized as a float (WP-11 AC #6).
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from contracts import (
    ContractModel,
    FlagRecord,
    FlagType,
    MoneyResponse,
    NullReason,
    OfferPoint,
    PropertySummary,
    RankingEntry,
    Scenario,
    ScoreSet,
    SourceKind,
    StrategyResult,
    StrategyType,
    TimelineEvent,
    TrackedValue,
)
from db import models as dbm


def dump(model: ContractModel | list[ContractModel] | dict) -> Any:
    if isinstance(model, list):
        return [item.model_dump(mode="json") for item in model]
    if isinstance(model, ContractModel):
        return model.model_dump(mode="json")
    return model


def money_envelope(tracked: TrackedValue | None) -> MoneyResponse | None:
    if tracked is None:
        return None
    return MoneyResponse(value=tracked.value, confidence=tracked.confidence,
                         source_kind=tracked.source_kind, is_estimated=tracked.is_estimated,
                         null_reason=tracked.null_reason)


def _decimal_map(values: dict | None) -> dict[str, Decimal]:
    return {key: Decimal(str(value)) for key, value in (values or {}).items() if value is not None}


def property_summary(row: dbm.Property, score: Decimal | None = None, rank: int | None = None,
                     open_flags: int = 0) -> PropertySummary:
    return PropertySummary(id=row.id, apn=row.apn, address_line1=row.address_line1, city=row.city,
                           state=row.state, zip5=row.zip5, pipeline_status=row.pipeline_status or "new",
                           tags=list(row.tags or []), next_action=row.next_action,
                           next_action_date=row.next_action_date, gut_rating=row.gut_rating,
                           is_watchlisted=bool(row.is_watchlisted), overall_score=score, rank=rank,
                           open_flags=open_flags)


def strategy_result(row: dbm.DealScenario) -> StrategyResult:
    metrics = {key: getattr(row, key) for key in ("cap_rate", "cash_flow", "coc", "arv")}
    metrics.update({key: getattr(row, key) for key in ("purchase_price", "repairs", "holding", "financing", "resale")})
    return StrategyResult(strategy=StrategyType(row.strategy), scenario=Scenario(row.scenario),
                          status=row.status or "viable", unavailable_reason=row.unavailable_reason,
                          mao=row.mao, all_in_basis=row.all_in_basis, profit=row.profit, roi=row.roi,
                          margin_of_safety=row.margin_of_safety,
                          metrics={key: value for key, value in metrics.items() if value is not None})


def offer_point(row: dbm.OfferScenario) -> OfferPoint:
    return OfferPoint(offer_price=row.offer_price, scenario=Scenario(row.scenario),
                      confirmed_payoffs=row.confirmed_payoffs, potential_payoffs=row.potential_payoffs,
                      closing_costs=row.closing_costs, proceeds_low=row.proceeds_low,
                      proceeds_expected=row.proceeds_expected, proceeds_high=row.proceeds_high,
                      buyer_basis=row.buyer_basis, profit=row.profit, roi=row.roi,
                      is_short_sale=bool(row.is_short_sale))


def score_set(row: dbm.Score) -> ScoreSet:
    return ScoreSet(property_id=row.property_id,
                    scoring_config_id=row.scoring_config_id or UUID(int=0),
                    fos=row.fos, distress=row.distress, data_confidence=row.data_confidence,
                    risk=row.risk, overall=row.overall, components=_decimal_map(row.components),
                    gates_applied=list(row.gates_applied or []), is_rankable="open_gating_flag" not in (row.gates_applied or []),
                    recommended_strategy=None)


def flag_record(row: dbm.Flag) -> FlagRecord:
    return FlagRecord(id=row.id, property_id=row.property_id, flag_type=FlagType(row.flag_type),
                      payload=dict(row.payload or {}), financial_impact_usd=row.financial_impact_usd,
                      status=row.status or "open", resolution=row.resolution,
                      resolved_value=row.resolved_value, note=row.note, dedupe_key=row.dedupe_key or "",
                      resolved_at=row.resolved_at)


def ranking_entry(row: dbm.Ranking) -> RankingEntry:
    return RankingEntry(property_id=row.property_id, rank=row.rank, prev_rank=row.prev_rank, score=row.score)


def tracked_money(value: Decimal | None, confidence: float = 1.0,
                  source_kind: SourceKind = SourceKind.REPORT) -> TrackedValue:
    if value is None:
        return TrackedValue(value=None, confidence=confidence, source_kind=source_kind,
                            is_estimated=False, null_reason=NullReason.NOT_PRESENT)
    return TrackedValue(value=value, confidence=confidence, source_kind=source_kind, is_estimated=False)


def timeline_event(event_type: str, when: date | datetime | None, label: str,
                   details: dict | None = None) -> TimelineEvent:
    if isinstance(when, datetime):
        when = when.date()
    return TimelineEvent(event_type=event_type, event_date=when, label=label, details=details or {})
