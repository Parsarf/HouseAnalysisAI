"""Comp-set support for the compare view (spec §11.5, WP-14).

Select 2–4 properties and get a row-per-metric table with the winning cell in
each row identified. Pure and deterministic: the UI renders what this returns
and highlights ``winner_indices``; no number is recomputed anywhere else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from contracts import Scenario, ScoreSet, StrategyResult, UnderwritingResult

MIN_ENTRIES = 2
MAX_ENTRIES = 4


@dataclass(frozen=True)
class CompSetEntry:
    """One property column: the already-computed analysis objects."""

    label: str
    underwriting: UnderwritingResult | None = None
    scores: ScoreSet | None = None
    strategies: list[StrategyResult] = field(default_factory=list)


@dataclass(frozen=True)
class CompSetRow:
    """One comparison row. ``values`` are raw (Decimal/str/None) in entry
    order; ``winner_indices`` marks the best cell(s) — empty for non-numeric
    rows or rows with fewer than two known values."""

    key: str
    label: str
    values: list[Any]
    higher_is_better: bool | None
    winner_indices: list[int]


@dataclass(frozen=True)
class CompSetTable:
    labels: list[str]
    rows: list[CompSetRow]


def _expected_equity(entry: CompSetEntry) -> Decimal | None:
    if entry.underwriting is None:
        return None
    block = entry.underwriting.equity.get(Scenario.EXPECTED)
    return block.adjusted if block else None


def _best_strategy(entry: CompSetEntry) -> StrategyResult | None:
    """The recommended strategy's expected-scenario result (MAO/profit/ROI source)."""
    recommended = entry.scores.recommended_strategy if entry.scores else None
    viable = [item for item in entry.strategies
              if item.status == "viable" and item.scenario == Scenario.EXPECTED]
    if recommended is not None:
        match = next((item for item in viable if item.strategy == recommended), None)
        if match is not None:
            return match
    return viable[0] if viable else None


def _winners(values: list[Decimal | None], higher_is_better: bool) -> list[int]:
    known = [(index, value) for index, value in enumerate(values) if value is not None]
    if len(known) < 2:
        return []
    target = max(v for _, v in known) if higher_is_better else min(v for _, v in known)
    return [index for index, value in known if value == target]


def _row(key: str, label: str, values: list[Any], higher_is_better: bool | None) -> CompSetRow:
    winners = _winners(values, higher_is_better) if higher_is_better is not None else []
    return CompSetRow(key=key, label=label, values=values, higher_is_better=higher_is_better,
                      winner_indices=winners)


def _risk_notes(entry: CompSetEntry) -> list[str]:
    if entry.scores is None:
        return []
    notes = list(entry.scores.gates_applied)
    risk_terms = sorted(
        ((name, value) for name, value in entry.scores.components.items()
         if name.startswith("risk_") and value > 0),
        key=lambda item: (-item[1], item[0]),
    )
    notes.extend(name.removeprefix("risk_") for name, _ in risk_terms[:3])
    return notes


def build_comp_set(entries: list[CompSetEntry]) -> CompSetTable:
    """Build the §11.5 comparison table for 2–4 properties.

    Rows: estimated value, confirmed debt, adjusted equity, the four scores,
    best strategy, MAO, profit, ROI, and key risks. Raises ValueError outside
    the 2–4 range — the spec's compare view is defined for exactly that.
    """
    if not MIN_ENTRIES <= len(entries) <= MAX_ENTRIES:
        raise ValueError(f"comp set needs {MIN_ENTRIES}-{MAX_ENTRIES} properties, got {len(entries)}")

    best = [_best_strategy(entry) for entry in entries]

    def value_of(fn) -> list[Any]:
        return [fn(entry) for entry in entries]

    rows = [
        _row("value", "Est. value",
             value_of(lambda e: e.underwriting.value.v_expected if e.underwriting else None), True),
        _row("confirmed_debt", "Confirmed debt",
             value_of(lambda e: e.underwriting.liabilities.confirmed if e.underwriting else None), False),
        _row("adjusted_equity", "Adjusted equity", value_of(_expected_equity), True),
        _row("overall", "Overall score", value_of(lambda e: e.scores.overall if e.scores else None), True),
        _row("fos", "Financial Opportunity", value_of(lambda e: e.scores.fos if e.scores else None), True),
        _row("distress", "Distress", value_of(lambda e: e.scores.distress if e.scores else None), True),
        _row("data_confidence", "Data Confidence",
             value_of(lambda e: e.scores.data_confidence if e.scores else None), True),
        _row("risk", "Risk", value_of(lambda e: e.scores.risk if e.scores else None), False),
        _row("best_strategy", "Best strategy",
             [item.strategy.value if item else None for item in best], None),
        _row("mao", "MAO", [item.mao if item else None for item in best], True),
        _row("profit", "Profit", [item.profit if item else None for item in best], True),
        _row("roi", "ROI", [item.roi if item else None for item in best], True),
        _row("key_risks", "Key risks", [_risk_notes(entry) for entry in entries], None),
    ]
    return CompSetTable(labels=[entry.label for entry in entries], rows=rows)
