"""Calibration suggestions: proposals, acceptance, and preview (spec §22, WP-17).

Suggestions are *proposals* — nothing auto-applies. Accepting one creates a
new ``AssumptionSet`` version (old version intact and rollback-able) and
returns a recompute request for the pipeline to enqueue. For scoring-config
suggestions, ``apply_scoring_suggestion`` builds the new config row payload.
``rank_deltas`` powers the before/after preview of accepting a suggestion.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from contracts import AssumptionSet

from .analysis import (
    DEFAULT_MIN_SAMPLE,
    holding_period_analysis,
    repair_cost_analysis,
    valuation_analysis,
)
from .models import RealizedDeal

KIND_REPAIR_PSF = "repair_psf"
KIND_VALUATION_WEIGHTS = "valuation_weights"
KIND_HOLDING_MARKET_DAYS = "holding_market_days"
KIND_SCORING_OVERALL_WEIGHTS = "scoring_overall_weights"


@dataclass(frozen=True)
class CalibrationSuggestion:
    """A proposed change. ``target`` is a dotted path into the AssumptionSet
    (or scoring config), e.g. ``repairs.psf_by_condition.moderate``."""

    kind: str
    target: str
    current: Any
    proposed: Any
    sample_size: int
    rationale: str


@dataclass(frozen=True)
class RecomputeRequest:
    """What the pipeline should enqueue when a suggestion is accepted."""

    assumption_set_id: UUID
    reason: str


@dataclass(frozen=True)
class RankChange:
    property_id: UUID
    prev_rank: int | None
    new_rank: int | None
    delta: int | None  # positive = moved up ("moved up 14")


def build_suggestions(assumptions: AssumptionSet, deals: Iterable[RealizedDeal],
                      min_sample: int = DEFAULT_MIN_SAMPLE) -> list[CalibrationSuggestion]:
    """All currently-warranted assumption proposals for the calibration page.

    Groups below ``min_sample`` produce no suggestion (the page shows the
    underlying analysis data instead)."""
    deals = list(deals)
    suggestions = []
    for analysis in repair_cost_analysis(deals, assumptions, min_sample):
        if analysis.suggested_psf is None or analysis.suggested_psf == analysis.current_psf:
            continue
        factor = analysis.correction_factor.quantize(Decimal("0.0001"))
        suggestions.append(CalibrationSuggestion(
            kind=KIND_REPAIR_PSF,
            target=f"repairs.psf_by_condition.{analysis.condition}",
            current=analysis.current_psf, proposed=analysis.suggested_psf,
            sample_size=analysis.sample_size,
            rationale=(f"Actual repairs run {factor}x the prediction across "
                       f"{analysis.sample_size} '{analysis.condition}' deals.")))

    valuation = valuation_analysis(deals, min_sample)
    if valuation.suggested_weights is not None and valuation.suggested_weights != assumptions.valuation_weights:
        summary = "; ".join(
            f"{stat.valuation_type} mean abs error {stat.mean_abs_error_pct.quantize(Decimal('0.0001'))}"
            for stat in valuation.stats)
        suggestions.append(CalibrationSuggestion(
            kind=KIND_VALUATION_WEIGHTS, target="valuation_weights",
            current=assumptions.valuation_weights, proposed=valuation.suggested_weights,
            sample_size=min(stat.sample_size for stat in valuation.stats),
            rationale=f"Reweight value candidates toward the most accurate ({summary})."))

    holding = holding_period_analysis(deals, assumptions, min_sample)
    if holding.suggested_market_days is not None and holding.suggested_market_days != assumptions.holding.market_days_default:
        suggestions.append(CalibrationSuggestion(
            kind=KIND_HOLDING_MARKET_DAYS, target="holding.market_days_default",
            current=assumptions.holding.market_days_default, proposed=holding.suggested_market_days,
            sample_size=holding.sample_size,
            rationale=(f"Deals hold {holding.mean_bias_days.quantize(Decimal('0.1'))} days "
                       f"{'longer' if holding.mean_bias_days > 0 else 'shorter'} than predicted "
                       f"on average.")))
    return suggestions


def _apply_to_assumptions(assumptions: AssumptionSet, suggestion: CalibrationSuggestion) -> AssumptionSet:
    updated = assumptions.model_copy(deep=True)
    parts = suggestion.target.split(".")
    if suggestion.kind == KIND_REPAIR_PSF and parts[:2] == ["repairs", "psf_by_condition"] and len(parts) == 3:
        updated.repairs.psf_by_condition[parts[2]] = suggestion.proposed
    elif suggestion.kind == KIND_VALUATION_WEIGHTS and suggestion.target == "valuation_weights":
        updated.valuation_weights = dict(suggestion.proposed)
    elif suggestion.kind == KIND_HOLDING_MARKET_DAYS and suggestion.target == "holding.market_days_default":
        updated.holding.market_days_default = int(suggestion.proposed)
    else:
        raise ValueError(f"unsupported suggestion target: {suggestion.target}")
    return updated


def accept_suggestion(assumptions: AssumptionSet,
                      suggestion: CalibrationSuggestion) -> tuple[AssumptionSet, RecomputeRequest]:
    """Create the new AssumptionSet version for an accepted suggestion.

    The input set is never mutated: the new version gets a fresh id and
    ``version + 1``; the old row stays intact and rollback-able. Returns the
    recompute request the pipeline should enqueue (bulk recompute, §17)."""
    new_set = _apply_to_assumptions(assumptions, suggestion)
    new_set.id = uuid4()
    new_set.version = assumptions.version + 1
    return new_set, RecomputeRequest(
        assumption_set_id=new_set.id, reason=f"calibration:{suggestion.kind}")


def apply_scoring_suggestion(config: Mapping[str, Any],
                             suggestion: CalibrationSuggestion) -> dict[str, Any]:
    """Build the new ``scoring_configs`` row payload for an accepted scoring
    weight suggestion. Pure dict in/out; activation of the new version is the
    caller's job (nothing auto-applies)."""
    if suggestion.kind != KIND_SCORING_OVERALL_WEIGHTS:
        raise ValueError(f"unsupported scoring suggestion kind: {suggestion.kind}")
    new_config = {key: (dict(value) if isinstance(value, Mapping) else value)
                  for key, value in config.items()}
    weights = {key: (dict(value) if isinstance(value, Mapping) else value)
               for key, value in (config.get("weights") or {}).items()}
    weights["overall"] = dict(suggestion.proposed)
    new_config["weights"] = weights
    new_config["version"] = int(config.get("version") or 0) + 1
    return new_config


def _ranks(scores: Mapping[UUID, Decimal]) -> dict[UUID, int]:
    ordered = sorted(scores, key=lambda pid: (-scores[pid], str(pid)))
    return {pid: index + 1 for index, pid in enumerate(ordered)}


def rank_deltas(before: Mapping[UUID, Decimal],
                after: Mapping[UUID, Decimal]) -> list[RankChange]:
    """Before/after rank changes between two score snapshots (the acceptance
    preview, WP-17). Rank 1 is the highest score; ties break by id."""
    prev_ranks = _ranks(before)
    new_ranks = _ranks(after)
    changes = []
    for pid in sorted(set(before) | set(after), key=str):
        prev, new = prev_ranks.get(pid), new_ranks.get(pid)
        changes.append(RankChange(
            property_id=pid, prev_rank=prev, new_rank=new,
            delta=(prev - new) if prev is not None and new is not None else None))
    return changes
