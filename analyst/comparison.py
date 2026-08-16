"""Deterministic "why is A above B" comparison (spec §13, WP-14).

The diff is pure code: two ``ScoreSet.components`` dicts are diffed term by
term, ranked by absolute point delta, and the top terms plus their raw driver
values are handed to an optional phraser (LLM) *for phrasing only*. The
phraser's output is validated to contain no number absent from the input
payload; on any failure the templated fallback is emitted, so the numbers in
the explanation are always deterministic.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from contracts import ScoreSet

ZERO = Decimal(0)

# Pillar prefix -> (pillar label, direction of effect on OVERALL).
# Risk points subtract from overall, so a higher risk term for A is reported
# as a negative driver of the A-minus-B gap.
PILLARS: dict[str, tuple[str, Decimal]] = {
    "fos": ("Financial Opportunity", Decimal(1)),
    "distress": ("Distress", Decimal(1)),
    "dcs": ("Data Confidence", Decimal(1)),
    "risk": ("Risk", Decimal(-1)),
}

# Point term -> (raw driver component key, human label, raw format).
# Raw drivers live alongside the point terms in ScoreSet.components
# (scoring/engine.py emits them as a contract for this package).
TERM_LABELS: dict[str, tuple[str, str | None, str | None, str]] = {
    "fos_profit": ("Financial Opportunity: expected profit", "profit", "money", "expected profit"),
    "fos_roi": ("Financial Opportunity: ROI", "roi", "percent", "ROI"),
    "fos_equity_pct": ("Financial Opportunity: equity %", "equity_pct", "percent", "equity share"),
    "fos_discount_to_value": ("Financial Opportunity: discount to value", "discount_to_value", "percent", "discount to value"),
    "fos_margin_of_safety": ("Financial Opportunity: margin of safety", "margin_of_safety", "percent", "margin of safety"),
    "distress_foreclosure": ("Distress: active foreclosure", None, None, ""),
    "distress_prior_foreclosure_activity": ("Distress: prior foreclosure activity", None, None, ""),
    "distress_bankruptcy_active": ("Distress: active bankruptcy", None, None, ""),
    "distress_bankruptcy_prior": ("Distress: prior bankruptcy", None, None, ""),
    "distress_repeat_filings": ("Distress: repeat filings", None, None, ""),
    "distress_tax_lien_property": ("Distress: property tax lien", None, None, ""),
    "distress_tax_lien_owner": ("Distress: owner-only tax lien", None, None, ""),
    "distress_other_liens": ("Distress: other involuntary liens", None, None, ""),
    "distress_taxes_delinquent": ("Distress: delinquent taxes", None, None, ""),
    "distress_absentee": ("Distress: absentee owner", None, None, ""),
    "distress_long_ownership": ("Distress: long ownership", None, None, ""),
    "distress_listing_failures": ("Distress: failed listings", None, None, ""),
    "distress_high_equity_bonus": ("Distress: high-equity bonus", None, None, ""),
    "dcs_coverage": ("Data Confidence: field coverage", None, None, ""),
    "dcs_corroboration": ("Data Confidence: corroboration", None, None, ""),
    "dcs_recency": ("Data Confidence: report recency", None, None, ""),
    "dcs_conflict": ("Data Confidence: conflict-freeness", None, None, ""),
    "dcs_verification": ("Data Confidence: verification rate", None, None, ""),
    "dcs_extraction": ("Data Confidence: extraction quality", None, None, ""),
    "risk_lien_count": ("Risk: open lien count", None, None, ""),
    "risk_active_bankruptcy": ("Risk: active bankruptcy", None, None, ""),
    "risk_foreclosure_stage": ("Risk: near-sale foreclosure stage", None, None, ""),
    "risk_owner_only_liens": ("Risk: large owner-only liens", None, None, ""),
    "risk_title_flags": ("Risk: title flags", None, None, ""),
    "risk_owner_occupied": ("Risk: owner-occupied", None, None, ""),
    "risk_hoa_arrears": ("Risk: HOA arrears", None, None, ""),
    "risk_material_conflicts": ("Risk: material conflicts", None, None, ""),
    "risk_low_confidence": ("Risk: low data confidence", None, None, ""),
    "risk_federal_tax_lien": ("Risk: federal tax lien", None, None, ""),
}

RAW_DRIVER_LABELS: dict[str, tuple[str, str]] = {
    "profit": ("expected profit", "money"),
    "roi": ("ROI", "percent"),
    "equity_pct": ("equity share", "percent"),
    "discount_to_value": ("discount to value", "percent"),
    "margin_of_safety": ("margin of safety", "percent"),
}

POINT_QUANT = Decimal("0.1")


@dataclass(frozen=True)
class ComponentDelta:
    """One scored sub-term's contribution to the A-minus-B gap."""

    name: str            # stable ScoreSet.components key, e.g. "fos_profit"
    label: str           # human-readable, e.g. "Financial Opportunity: expected profit"
    pillar: str          # fos | distress | dcs | risk
    a_points: Decimal
    b_points: Decimal
    point_delta: Decimal  # signed in the direction of OVERALL (risk negated)
    a_raw: Decimal | None = None
    b_raw: Decimal | None = None
    raw_label: str | None = None
    raw_format: str | None = None  # "money" | "percent"


@dataclass(frozen=True)
class ScoreComparison:
    """Full deterministic comparison result. ``terms`` is ranked by absolute
    point delta; ``pillar_deltas`` holds the four component-level deltas in
    the direction of OVERALL (risk negated)."""

    a_label: str
    b_label: str
    a_overall: Decimal
    b_overall: Decimal
    overall_delta: Decimal
    pillar_deltas: dict[str, Decimal]
    terms: list[ComponentDelta]
    all_terms: list[ComponentDelta] = field(default_factory=list)


def _pillar_of(name: str) -> str | None:
    for prefix in PILLARS:
        if name.startswith(f"{prefix}_"):
            return prefix
    return None


def _format_raw(value: Decimal, fmt: str) -> str:
    if fmt == "money":
        return f"${value:,.0f}"
    if fmt == "percent":
        return f"{(value * 100).quantize(POINT_QUANT)}%"
    return f"{value}"


def compare_scores(a: ScoreSet, b: ScoreSet, *, a_label: str = "A", b_label: str = "B",
                   top_n: int = 5) -> ScoreComparison:
    """Diff two ScoreSets term by term and rank the drivers of the gap.

    Every shared point sub-term (``fos_*``, ``distress_*``, ``dcs_*``,
    ``risk_*``) is compared; terms missing from one side count as zero.
    Raw driver values (``profit``, ``roi``, ...) ride along as context.
    """
    all_terms: list[ComponentDelta] = []
    for name in sorted(set(a.components) | set(b.components)):
        pillar = _pillar_of(name)
        if pillar is None:
            continue  # raw drivers are context, not ranked terms
        direction = PILLARS[pillar][1]
        a_points = a.components.get(name, ZERO)
        b_points = b.components.get(name, ZERO)
        delta = (a_points - b_points) * direction
        label, raw_key, raw_format, raw_label = TERM_LABELS.get(name, (name, None, None, ""))
        all_terms.append(ComponentDelta(
            name=name, label=label, pillar=pillar,
            a_points=a_points, b_points=b_points, point_delta=delta,
            a_raw=a.components.get(raw_key) if raw_key else None,
            b_raw=b.components.get(raw_key) if raw_key else None,
            raw_label=raw_label or None, raw_format=raw_format,
        ))
    ranked = sorted(all_terms, key=lambda term: (-abs(term.point_delta), term.name))
    drivers = [term for term in ranked if term.point_delta != ZERO]
    pillar_deltas = {
        "fos": a.fos - b.fos,
        "distress": a.distress - b.distress,
        "dcs": a.data_confidence - b.data_confidence,
        "risk": -(a.risk - b.risk),
    }
    return ScoreComparison(
        a_label=a_label, b_label=b_label,
        a_overall=a.overall, b_overall=b.overall,
        overall_delta=a.overall - b.overall,
        pillar_deltas=pillar_deltas,
        terms=drivers[:top_n],
        all_terms=drivers,
    )


# --- Phrasing layer: the model may only reword, never invent numbers --------

_NUMBER_RE = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?%?")


def _parse_number(token: str) -> Decimal | None:
    cleaned = token.replace("$", "").replace(",", "").rstrip("%")
    try:
        return Decimal(cleaned)
    except ArithmeticError:
        return None


def extract_numbers(text: str) -> list[Decimal]:
    """Every number literal in ``text`` (``25%`` reads as 25 — payload ratios
    are allowed in both 0.25 and 25 form, see ``allowed_numbers``)."""
    numbers = []
    for token in _NUMBER_RE.findall(text):
        value = _parse_number(token)
        if value is not None:
            numbers.append(value)
    return numbers


def _collect_payload_numbers(payload: Any, into: set[Decimal]) -> None:
    if isinstance(payload, bool):
        return
    if isinstance(payload, (int, float, Decimal)):
        value = Decimal(str(payload))
        into.add(value)
        into.add(value * 100)
        for exponent in ("1", "0.1", "0.01"):
            into.add(value.quantize(Decimal(exponent)))
        return
    if isinstance(payload, str):
        into.update(extract_numbers(payload))
        return
    if isinstance(payload, Mapping):
        for value in payload.values():
            _collect_payload_numbers(value, into)
        return
    if isinstance(payload, (list, tuple)):
        for value in payload:
            _collect_payload_numbers(value, into)


def allowed_numbers(payload: Any) -> set[Decimal]:
    """The closed set of numbers a phraser may mention: every numeric leaf of
    the payload, plus rounded and percent variants (phrasing may round)."""
    numbers: set[Decimal] = set()
    _collect_payload_numbers(payload, numbers)
    return numbers


def validate_phrasing(text: str, payload: Any) -> bool:
    """True iff every number in ``text`` appears in the payload (spec §13:
    validation rejects any response containing a number not in the input)."""
    allowed = allowed_numbers(payload)
    return all(number in allowed for number in extract_numbers(text))


def phrasing_payload(comparison: ScoreComparison) -> dict[str, Any]:
    """The exact payload handed to the phraser — the only numbers it may use."""
    return {
        "a_label": comparison.a_label,
        "b_label": comparison.b_label,
        "a_overall": comparison.a_overall,
        "b_overall": comparison.b_overall,
        "overall_delta": comparison.overall_delta,
        "pillar_deltas": comparison.pillar_deltas,
        "terms": [
            {
                "name": term.name,
                "label": term.label,
                "pillar": term.pillar,
                "a_points": term.a_points,
                "b_points": term.b_points,
                "point_delta": term.point_delta,
                "a_raw": term.a_raw,
                "b_raw": term.b_raw,
                "raw_label": term.raw_label,
            }
            for term in comparison.terms
        ],
    }


def _points(value: Decimal) -> str:
    return f"{value.quantize(POINT_QUANT)}"


def _signed_points(value: Decimal) -> str:
    return f"+{_points(value)}" if value > ZERO else _points(value)


def template_explanation(comparison: ScoreComparison) -> str:
    """Deterministic fallback phrasing built only from payload numbers."""
    a, b = comparison.a_label, comparison.b_label
    delta = comparison.overall_delta
    if delta == ZERO:
        headline = f"{a} and {b} are tied at {_points(comparison.a_overall)} overall."
    elif delta > ZERO:
        headline = f"{a} is {_points(delta)} points above {b} overall ({_points(comparison.a_overall)} vs {_points(comparison.b_overall)})."
    else:
        headline = f"{a} is {_points(-delta)} points below {b} overall ({_points(comparison.a_overall)} vs {_points(comparison.b_overall)})."
    if not comparison.terms:
        return f"{headline} No scoring sub-term differs."

    drivers = [term for term in comparison.terms if term.point_delta * delta > ZERO] if delta != ZERO else []
    offsets = [term for term in comparison.terms if term not in drivers]
    parts = [f"{term.label} {_signed_points(term.point_delta)} pts" + _raw_clause(term) for term in drivers]
    if delta == ZERO:
        parts = [f"{term.label} {_signed_points(term.point_delta)} pts" + _raw_clause(term) for term in comparison.terms]
    sentence = " Biggest drivers: " + "; ".join(parts) + "." if parts else ""
    if offsets and delta != ZERO:
        sentence += " Partly offset by " + "; ".join(
            f"{term.label} {_signed_points(term.point_delta)} pts" + _raw_clause(term) for term in offsets
        ) + "."
    return headline + sentence


def _raw_clause(term: ComponentDelta) -> str:
    if term.a_raw is None or term.b_raw is None or not term.raw_label or not term.raw_format:
        return ""
    return (
        f" ({_format_raw(term.a_raw, term.raw_format)} vs "
        f"{_format_raw(term.b_raw, term.raw_format)} {term.raw_label})"
    )


def explain_comparison(comparison: ScoreComparison,
                       phraser: Callable[[dict[str, Any]], str] | None = None) -> str:
    """Phrase the comparison. With no phraser — or when the phraser fails or
    introduces any number absent from the payload — the template is emitted,
    so the reported numbers are deterministic regardless of the model."""
    payload = phrasing_payload(comparison)
    if phraser is not None:
        try:
            text = phraser(payload)
        except Exception:  # noqa: BLE001 - a phraser failure must never break the fallback
            text = None
        if text and validate_phrasing(text, payload):
            return text
    return template_explanation(comparison)


def why_above(a: ScoreSet, b: ScoreSet, *, a_label: str = "A", b_label: str = "B",
              top_n: int = 5, phraser: Callable[[dict[str, Any]], str] | None = None) -> tuple[ScoreComparison, str]:
    """One-call entry point: compare two ScoreSets and explain the gap."""
    comparison = compare_scores(a, b, a_label=a_label, b_label=b_label, top_n=top_n)
    return comparison, explain_comparison(comparison, phraser)
