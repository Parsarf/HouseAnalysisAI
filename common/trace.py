"""Calculation provenance recorder.

Engines accept an optional ``TraceRecorder`` and record the exact inputs,
formulas and substitutions of the calculation they are already performing.
The recorder never changes arithmetic: with ``None`` (the default) engines
behave exactly as before, and recorded values are read back from the same
variables that produced the persisted result. This keeps explanations
formula-drift-free by construction: a trace can only be produced during the
real computation, never by an independent reimplementation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


def show(value: Any) -> str:
    """Human display for a recorded value (money-friendly, no exponent notation)."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Decimal):
        value = value.normalize()
        text = format(value, "f")
        return text
    if isinstance(value, float):
        return repr(value)
    return str(value)


@dataclass
class TraceRecorder:
    engine: str = ""
    engine_version: str = ""
    entries: list[dict[str, Any]] = field(default_factory=list)

    # -- recording API used by engines -----------------------------------------
    def step(self, label: str, formula: str | None = None, inputs: dict[str, Any] | None = None,
             result: Any = None, substitution: str | None = None) -> None:
        self.entries.append({
            "kind": "step", "label": label,
            "formula": formula,
            "inputs": {name: _plain(value) for name, value in (inputs or {}).items()},
            "substitution": substitution or _substitution(inputs),
            "result": _plain(result), "display_result": show(result) if result is not None else None,
        })

    def input(self, name: str, value: Any, note: str | None = None,
              source_fact_id: Any = None) -> None:
        self.entries.append({
            "kind": "input", "label": name, "value": _plain(value),
            "display_value": show(value) if value is not None else None,
            "note": note, "source_fact_id": source_fact_id,
        })

    def assumption(self, name: str, value: Any, note: str | None = None,
                   assumption_set_id: Any = None) -> None:
        self.entries.append({
            "kind": "assumption", "label": name, "value": _plain(value),
            "display_value": show(value) if value is not None else None,
            "note": note, "assumption_set_id": assumption_set_id,
        })

    def warning(self, message: str) -> None:
        self.entries.append({"kind": "warning", "message": message})

    def unresolved(self, name: str) -> None:
        self.entries.append({"kind": "unresolved", "message": name})

    def conflict(self, description: str, magnitude: Any = None) -> None:
        self.entries.append({"kind": "conflict", "description": description,
                             "magnitude": _plain(magnitude)})

    def candidate(self, label: str, value: Any, confidence: Any = None,
                  origin: str | None = None, is_winner: bool = False,
                  reason: str | None = None, derivation_inputs: dict[str, Any] | None = None,
                  source_fact_id: Any = None) -> None:
        self.entries.append({
            "kind": "candidate", "label": label, "value": _plain(value),
            "display_value": show(value) if value is not None else None,
            "confidence": None if confidence is None else Decimal(str(confidence)),
            "origin": origin, "is_winner": is_winner, "reason": reason,
            "derivation_inputs": {k: _plain(v) for k, v in (derivation_inputs or {}).items()},
            "source_fact_id": source_fact_id,
        })

    def resolution(self, method: str, winner_description: str, reason: str) -> None:
        self.entries.append({"kind": "resolution", "method": method,
                             "winner_description": winner_description, "reason": reason})

    def sensitivity(self, question: str, effect: str, delta: Any = None) -> None:
        self.entries.append({"kind": "sensitivity", "question": question, "effect": effect,
                             "delta": _plain(delta)})

    # -- reading -----------------------------------------------------------------
    def by_kind(self, kind: str) -> list[dict[str, Any]]:
        return [entry for entry in self.entries if entry["kind"] == kind]

    @property
    def steps(self) -> list[dict[str, Any]]:
        return self.by_kind("step")

    @property
    def warnings(self) -> list[str]:
        return [entry["message"] for entry in self.by_kind("warning")]


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return value
    if hasattr(value, "value") and isinstance(getattr(value, "value", None), Decimal):
        return value.value  # TrackedValue-like passthrough
    return value


def _substitution(inputs: dict[str, Any] | None) -> str | None:
    if not inputs:
        return None
    return ", ".join(f"{name}={show(value)}" for name, value in inputs.items())
