"""Explainability package: structured audit traces for every material figure.

Traces are built by re-running the same deterministic engine functions that
produce (and produced) the persisted results, with a ``TraceRecorder``
attached. The explanation layer never implements its own formulas.
"""
from .dispatch import available_keys, build_trace

__all__ = ["available_keys", "build_trace"]
