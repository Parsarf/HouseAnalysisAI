"""Key registry and dispatcher for explanation traces."""
from __future__ import annotations

from collections.abc import Callable
from functools import partial
from uuid import UUID

from sqlalchemy.orm import Session

from contracts import ExplanationTrace, Scenario

from .builder import (
    ExplainContext,
    build_arv,
    build_cost,
    build_equity,
    build_liabilities,
    build_repairs,
    build_value,
)
from .builders_scores import (
    build_offer_simulator,
    build_rank,
    build_recommendation,
    build_score_overall,
    build_strategy,
)

SCENARIOS = (Scenario.CONSERVATIVE, Scenario.EXPECTED, Scenario.OPTIMISTIC)

CORE_KEYS = [
    "value.expected", "value.low", "value.high", "value.arv",
    "liabilities.confirmed", "liabilities.potential", "liabilities.maximum",
    "equity.conservative", "equity.expected", "equity.optimistic",
    "repairs.conservative", "repairs.expected", "repairs.optimistic",
    "costs.acquisition.expected", "costs.holding.expected", "costs.resale.expected",
    "offers.simulator",
    "score.overall", "score.fos", "score.distress", "score.data_confidence", "score.risk",
    "recommendation.strategy", "rank",
]
STRATEGIES = ("cash", "flip", "wholesale", "rental", "subject_to", "foreclosure")


def available_keys() -> list[str]:
    keys = list(CORE_KEYS)
    for strategy in STRATEGIES:
        for scenario in SCENARIOS:
            keys.append(f"strategy.{strategy}.{scenario.value}")
        keys.append(f"strategy.{strategy}.expected.mao")
        keys.append(f"strategy.{strategy}.expected.profit")
    return keys


def build_trace(session: Session, property_id: UUID, key: str) -> ExplanationTrace:
    """Build one trace; metric-suffixed strategy keys return the strategy trace
    narrowed to that child (same recorded execution, no recomputation)."""
    ctx = ExplainContext(session, property_id)
    if ctx.normalized is None and key != "rank":
        raise KeyError(f"no normalized analysis exists for property {property_id}")
    parts = key.split(".")
    if parts[0] == "strategy":
        base_key = ".".join(parts[:3]) if len(parts) >= 3 else f"strategy.{parts[1]}.expected"
        scenario_name = parts[2] if len(parts) > 2 else "expected"
        trace = build_strategy(session, ctx, parts[1], scenario_name)
        if len(parts) > 3:
            metric_suffix = ".".join(parts[3:])
            child = next((child for child in trace.children
                          if child.key == f"{base_key}.{metric_suffix}"), None)
            if child is not None:
                return child.model_copy(update={"key": key})
            if metric_suffix == "profit":
                return trace.model_copy(update={"key": key})
        return trace
    builders: dict[str, Callable[[], ExplanationTrace]] = {}

    def add(name: str, fn: Callable[[], ExplanationTrace]) -> None:
        builders[name] = fn

    add("value.expected", partial(build_value, ctx, "expected"))
    add("value.low", partial(build_value, ctx, "low"))
    add("value.high", partial(build_value, ctx, "high"))
    add("value.arv", partial(build_arv, ctx))
    add("liabilities.confirmed", partial(build_liabilities, ctx, "confirmed"))
    add("liabilities.potential", partial(build_liabilities, ctx, "potential"))
    add("liabilities.maximum", partial(build_liabilities, ctx, "maximum"))
    for scenario in SCENARIOS:
        add(f"equity.{scenario.value}", partial(build_equity, ctx, scenario))
        add(f"repairs.{scenario.value}", partial(build_repairs, ctx, scenario))
        for which in ("acquisition", "holding", "resale"):
            add(f"costs.{which}.{scenario.value}", partial(build_cost, ctx, which, scenario))
    add("offers.simulator", partial(build_offer_simulator, ctx, Scenario.EXPECTED))
    add("score.overall", partial(build_score_overall, ctx))
    for component in ("fos", "distress", "data_confidence", "risk"):
        add(f"score.{component}", partial(_component, ctx, component))
    add("recommendation.strategy", partial(build_recommendation, ctx))
    add("rank", partial(build_rank, ctx))

    builder = builders.get(key)
    if builder is None:
        raise KeyError(f"unknown explanation key '{key}'")
    return builder()


def _component(ctx: ExplainContext, component: str) -> ExplanationTrace:
    from .builders_scores_terms import build_score_component

    return build_score_component(ctx, component)
