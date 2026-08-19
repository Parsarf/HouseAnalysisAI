"""Predicted-vs-actual analyses for the calibration page (spec §22, WP-17).

Four analyses plus score-band hit rates:

- repairs per condition level -> correction factor and suggested $/sqft;
- sale price per valuation candidate type -> suggested candidate reweighting;
- holding period -> bias and suggested market-days default;
- gut rating vs Overall Score -> Spearman correlation;
- hit rate (closed profitably) per Overall Score band.

Every suggestion is a proposal gated on a minimum sample (default 5 per
group); below that the analysis reports data and suggests nothing.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from contracts import AssumptionSet

from .models import RealizedDeal

ZERO = Decimal(0)
ONE = Decimal(1)
CENT = Decimal("0.01")
WEIGHT_QUANT = Decimal("0.0001")
# Inverse-error weights blow up at zero error; floor the divisor instead.
MIN_ABS_ERROR_PCT = Decimal("0.0001")

DEFAULT_MIN_SAMPLE = 5
DEFAULT_SCORE_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal(0), Decimal(45)),
    (Decimal(45), Decimal(60)),
    (Decimal(60), Decimal(70)),
    (Decimal(70), Decimal(85)),
    (Decimal(85), Decimal(101)),
)


@dataclass(frozen=True)
class RepairCostAnalysis:
    condition: str
    sample_size: int
    predicted_total: Decimal
    actual_total: Decimal
    correction_factor: Decimal | None      # actual / predicted
    current_psf: Decimal | None
    suggested_psf: Decimal | None          # None below min_sample or without a current default


@dataclass(frozen=True)
class ValuationCandidateStats:
    valuation_type: str
    sample_size: int
    mean_signed_error_pct: Decimal         # (predicted - actual) / actual, signed
    mean_abs_error_pct: Decimal


@dataclass(frozen=True)
class ValuationAnalysis:
    stats: list[ValuationCandidateStats]
    suggested_weights: dict[str, Decimal] | None  # normalized to sum 1; None below min_sample


@dataclass(frozen=True)
class HoldingPeriodAnalysis:
    sample_size: int
    mean_predicted_days: Decimal | None
    mean_actual_days: Decimal | None
    mean_bias_days: Decimal | None         # actual - predicted
    suggested_market_days: int | None      # None below min_sample


@dataclass(frozen=True)
class GutCorrelation:
    sample_size: int
    coefficient: Decimal | None            # Spearman rho; None when undefined/degenerate


@dataclass(frozen=True)
class ScoreBandStats:
    band: tuple[Decimal, Decimal]
    sample_size: int
    hits: int
    hit_rate: Decimal | None
    mean_realized_profit: Decimal | None


@dataclass(frozen=True)
class PillarSeparation:
    pillar: str
    hits_mean: Decimal
    misses_mean: Decimal
    separation: Decimal                    # direction-adjusted: positive = higher for hits


@dataclass(frozen=True)
class ScoringWeightAnalysis:
    sample_size: int
    separations: list[PillarSeparation]
    suggested_overall_weights: dict[str, Decimal] | None
    rationale: str | None


def _mean(values: list[Decimal]) -> Decimal | None:
    return sum(values, ZERO) / Decimal(len(values)) if values else None


def repair_cost_analysis(deals: Iterable[RealizedDeal], assumptions: AssumptionSet,
                         min_sample: int = DEFAULT_MIN_SAMPLE) -> list[RepairCostAnalysis]:
    """Predicted vs. actual repairs per condition level. The correction factor
    is the ratio of sums, so with consistent sqft data the suggested $/sqft
    converges on the true per-sqft cost."""
    grouped: dict[str, list[tuple[Decimal, Decimal]]] = {}
    for deal in deals:
        condition = deal.snapshot.condition
        predicted = deal.snapshot.predicted_repairs
        if condition is None or predicted is None or deal.actual_repairs is None:
            continue
        grouped.setdefault(condition, []).append((predicted, deal.actual_repairs))

    analyses = []
    for condition in sorted(grouped):
        pairs = grouped[condition]
        predicted_total = sum((p for p, _ in pairs), ZERO)
        actual_total = sum((a for _, a in pairs), ZERO)
        factor = (actual_total / predicted_total) if predicted_total > ZERO else None
        current_psf = assumptions.repairs.psf_by_condition.get(condition)
        suggested = None
        if len(pairs) >= min_sample and factor is not None and current_psf is not None:
            suggested = (current_psf * factor).quantize(CENT)
        analyses.append(RepairCostAnalysis(
            condition=condition, sample_size=len(pairs),
            predicted_total=predicted_total, actual_total=actual_total,
            correction_factor=factor, current_psf=current_psf, suggested_psf=suggested))
    return analyses


def valuation_analysis(deals: Iterable[RealizedDeal],
                       min_sample: int = DEFAULT_MIN_SAMPLE) -> ValuationAnalysis:
    """Predicted vs. actual sale price per valuation candidate type.

    Suggested weights are proportional to inverse mean absolute percentage
    error across the candidate types with enough samples — the candidate that
    has been closest to reality gets the most weight."""
    errors: dict[str, list[Decimal]] = {}
    for deal in deals:
        actual = deal.sale_price
        if actual is None or actual <= ZERO:
            continue
        for valuation_type, predicted in deal.snapshot.valuation_predictions.items():
            if predicted is None:
                continue
            errors.setdefault(valuation_type, []).append((predicted - actual) / actual)

    stats = []
    for valuation_type in sorted(errors):
        signed = errors[valuation_type]
        stats.append(ValuationCandidateStats(
            valuation_type=valuation_type, sample_size=len(signed),
            mean_signed_error_pct=_mean(signed) or ZERO,
            mean_abs_error_pct=_mean([abs(value) for value in signed]) or ZERO))

    qualifying = [stat for stat in stats if stat.sample_size >= min_sample]
    suggested = None
    if len(qualifying) >= 2:
        raw = {stat.valuation_type: ONE / max(stat.mean_abs_error_pct, MIN_ABS_ERROR_PCT)
               for stat in qualifying}
        total = sum(raw.values(), ZERO)
        weights = {key: (value / total).quantize(WEIGHT_QUANT) for key, value in raw.items()}
        # Keep the weights summing to exactly 1 after quantization.
        largest = max(weights, key=lambda key: weights[key])
        weights[largest] += ONE - sum(weights.values(), ZERO)
        suggested = weights
    return ValuationAnalysis(stats=stats, suggested_weights=suggested)


def holding_period_analysis(deals: Iterable[RealizedDeal], assumptions: AssumptionSet,
                            min_sample: int = DEFAULT_MIN_SAMPLE) -> HoldingPeriodAnalysis:
    """Predicted vs. actual holding period; the bias shifts the market-days
    default (repair/acquisition months are condition-driven and reviewed
    separately)."""
    pairs = [(Decimal(deal.snapshot.predicted_holding_days), Decimal(deal.actual_holding_days))
             for deal in deals
             if deal.snapshot.predicted_holding_days is not None and deal.actual_holding_days is not None]
    predicted = [p for p, _ in pairs]
    actual = [a for _, a in pairs]
    bias = _mean([a - p for p, a in pairs])
    suggested = None
    if len(pairs) >= min_sample and bias is not None:
        suggested = max(0, int(assumptions.holding.market_days_default + bias))
    return HoldingPeriodAnalysis(
        sample_size=len(pairs), mean_predicted_days=_mean(predicted), mean_actual_days=_mean(actual),
        mean_bias_days=bias, suggested_market_days=suggested)


def _ranks(values: list[Decimal]) -> list[Decimal]:
    """Average ranks (1-based) with ties sharing the mean rank."""
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [ZERO] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        mean_rank = Decimal(index + end + 2) / Decimal(2)
        for position in range(index, end + 1):
            ranks[order[position]] = mean_rank
        index = end + 1
    return ranks


def _pearson(xs: list[Decimal], ys: list[Decimal]) -> Decimal | None:
    mean_x, mean_y = _mean(xs), _mean(ys)
    if mean_x is None or mean_y is None:
        return None
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    denom_x = sum((d * d for d in dx), ZERO)
    denom_y = sum((d * d for d in dy), ZERO)
    if denom_x == ZERO or denom_y == ZERO:
        return None  # one side is constant: correlation undefined
    return sum((a * b for a, b in zip(dx, dy)), ZERO) / (denom_x * denom_y).sqrt()


def gut_rating_correlation(deals: Iterable[RealizedDeal],
                           min_sample: int = DEFAULT_MIN_SAMPLE) -> GutCorrelation:
    """Spearman correlation between the 1–5 triage gut rating and the Overall
    Score — whether the model captures what you actually care about (§22)."""
    pairs = [(Decimal(deal.snapshot.gut_rating), deal.snapshot.overall_score)
             for deal in deals
             if deal.snapshot.gut_rating is not None and deal.snapshot.overall_score is not None]
    coefficient = None
    if len(pairs) >= min_sample:
        gut_ranks = _ranks([g for g, _ in pairs])
        score_ranks = _ranks([s for _, s in pairs])
        coefficient = _pearson(gut_ranks, score_ranks)
    return GutCorrelation(sample_size=len(pairs), coefficient=coefficient)


def score_band_analysis(deals: Iterable[RealizedDeal],
                        bands: tuple[tuple[Decimal, Decimal], ...] = DEFAULT_SCORE_BANDS) -> list[ScoreBandStats]:
    """Hit rate (closed with profit > 0) per Overall Score band — a monotone
    rising hit rate means the ranking orders reality correctly."""
    scored = [deal for deal in deals if deal.snapshot.overall_score is not None]
    result = []
    for low, high in bands:
        members = [deal for deal in scored if deal.snapshot.overall_score is not None and low <= deal.snapshot.overall_score < high]
        hits = sum(1 for deal in members if deal.is_hit)
        profits = [profit for deal in members if (profit := deal.realized_profit) is not None]
        result.append(ScoreBandStats(
            band=(low, high), sample_size=len(members), hits=hits,
            hit_rate=(Decimal(hits) / Decimal(len(members))) if members else None,
            mean_realized_profit=_mean(profits)))
    return result


# --- scoring_configs adjustment proposals -----------------------------------

PILLAR_KEYS = ("fos", "distress", "dcs", "risk")
# Pillars where a higher value is better; risk is direction-flipped.
POSITIVE_PILLARS = frozenset({"fos", "distress", "dcs"})
WEIGHT_SHIFT_STEP = Decimal("0.05")


def scoring_weight_analysis(deals: Iterable[RealizedDeal],
                            current_weights: dict[str, Decimal],
                            min_sample: int = DEFAULT_MIN_SAMPLE,
                            step: Decimal = WEIGHT_SHIFT_STEP) -> ScoringWeightAnalysis:
    """Which OVERALL pillars actually separate hits from misses, and a
    proposed ``scoring_configs`` weights adjustment when one pillar fails to.

    Separation is the direction-adjusted mean difference between hits and
    misses. When the worst pillar does not discriminate (separation <= 0) and
    the best does, ``step`` of weight is proposed to move from worst to best.
    Requires ``min_sample`` hits *and* misses.
    """
    hits: list[dict[str, Decimal]] = []
    misses: list[dict[str, Decimal]] = []
    for deal in deals:
        scores = deal.snapshot.pillar_scores
        if not scores:
            continue
        profit = deal.realized_profit
        if profit is None:
            continue
        (hits if profit > ZERO else misses).append(scores)

    separations = []
    for pillar in PILLAR_KEYS:
        hit_values = [scores[pillar] for scores in hits if pillar in scores]
        miss_values = [scores[pillar] for scores in misses if pillar in scores]
        if not hit_values or not miss_values:
            continue
        hits_mean = _mean(hit_values) or ZERO
        misses_mean = _mean(miss_values) or ZERO
        raw = hits_mean - misses_mean
        separation = raw if pillar in POSITIVE_PILLARS else -raw
        separations.append(PillarSeparation(
            pillar=pillar, hits_mean=hits_mean, misses_mean=misses_mean, separation=separation))

    sample_size = min(len(hits), len(misses))
    suggested = None
    rationale = None
    if sample_size >= min_sample and len(separations) >= 2:
        best = max(separations, key=lambda item: item.separation)
        worst = min(separations, key=lambda item: item.separation)
        if worst.pillar in current_weights and best.pillar in current_weights:
            shift = min(step, current_weights[worst.pillar])
            if shift > ZERO and best.separation > ZERO and worst.separation <= ZERO and worst.pillar != best.pillar:
                suggested = dict(current_weights)
                suggested[worst.pillar] -= shift
                suggested[best.pillar] += shift
                rationale = (
                    f"'{worst.pillar}' does not separate hits from misses "
                    f"(separation {worst.separation.quantize(WEIGHT_QUANT)}); "
                    f"'{best.pillar}' does ({best.separation.quantize(WEIGHT_QUANT)}). "
                    f"Shift {shift} of overall weight from {worst.pillar} to {best.pillar}."
                )
    return ScoringWeightAnalysis(
        sample_size=sample_size, separations=separations,
        suggested_overall_weights=suggested, rationale=rationale)
