"""WP-14 AI analyst: deterministic comparison/explanation and comp-set support.

The comparison engine diffs ``ScoreSet.components`` (whose stable key names
are a contract owned by ``scoring``) and ranks the drivers of an A-vs-B gap;
an optional phraser may reword the result but is validated to introduce no
number absent from the payload, with a templated fallback. ``build_comp_set``
produces the spec §11.5 compare-view table for 2–4 properties.
"""
from .comparison import (
                         ComponentDelta,
                         ScoreComparison,
                         allowed_numbers,
                         compare_scores,
                         explain_comparison,
                         extract_numbers,
                         phrasing_payload,
                         template_explanation,
                         validate_phrasing,
                         why_above,
)
from .compset import CompSetEntry, CompSetRow, CompSetTable, build_comp_set

__all__ = [
    "CompSetEntry",
    "CompSetRow",
    "CompSetTable",
    "ComponentDelta",
    "ScoreComparison",
    "allowed_numbers",
    "build_comp_set",
    "compare_scores",
    "explain_comparison",
    "extract_numbers",
    "phrasing_payload",
    "template_explanation",
    "validate_phrasing",
    "why_above",
]
