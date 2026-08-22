"""Explanation builders for strategies, offer simulator, and scoring."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from common.trace import TraceRecorder, show
from contracts import (
    ExplanationCandidate,
    ExplanationInput,
    ExplanationResolution,
    ExplanationSensitivity,
    ExplanationTrace,
    Scenario,
)

from .builder import (
    ExplainContext,
    finish,
    scenario_label,
    scoring_version,
    strategies_version,
    unavailable_trace,
)

ZERO = Decimal(0)


# --------------------------------------------------------------------------- strategy keys

_STRATEGY_DESCRIPTIONS = {
    "cash": "Buy with cash and resell at the as-is value. Profit = value after resale costs minus everything paid.",
    "flip": "Renovate and resell at the after-repair value using hard-money financing.",
    "wholesale": "Contract the property and assign the contract to another investor for a fee.",
    "rental": "Buy and hold as a rental; profitability is measured by cash flow, cap rate, and DSCR.",
    "subject_to": "Take over the existing loan payments subject to the lender's due-on-sale rights. "
                  "Detection only — always requires legal review.",
    "foreclosure": "Buy at the foreclosure auction; profit is the spread between the low value and all obligations.",
}
_STRATEGY_FORMULAS = {
    "cash": "profit = value × (1 − resale%) − (purchase + acquisition + repairs + holding)",
    "flip": "profit = ARV − (purchase + repairs + holding + financing + acquisition + resale incl. staging)",
    "wholesale": "profit (spread) = ARV × investor% − repairs − contract price",
    "rental": "profit (annual cash flow) = NOI − debt service",
    "subject_to": "detection only; no profit is computed",
    "foreclosure": "profit (spread) = V low − total obligations − repairs − auction holding",
}


def build_strategy(session: Session, ctx: ExplainContext, strategy: str, scenario_name: str) -> ExplanationTrace:
    from strategies import cash as cash_fn
    from strategies import flip as flip_fn
    from strategies import foreclosure as foreclosure_fn
    from strategies import rental as rental_fn
    from strategies import subject_to as subject_to_fn
    from strategies import wholesale as wholesale_fn

    try:
        scenario = Scenario(scenario_name)
    except ValueError:
        scenario = Scenario.EXPECTED
    record = ctx.normalized
    uw, _uw_recorder = ctx.underwriting()
    if record is None or uw is None or ctx.assumptions is None:
        return unavailable_trace(f"strategy.{strategy}.{scenario.value}", f"{strategy} strategy", uw)
    price = ctx.purchase_price()
    recorder = TraceRecorder(engine="strategies", engine_version=strategies_version())
    functions = {
        "cash": lambda: cash_fn(record, uw, ctx.assumptions, price, scenario, trace=recorder),
        "flip": lambda: flip_fn(record, uw, ctx.assumptions, price, scenario, trace=recorder),
        "wholesale": lambda: wholesale_fn(uw, ctx.assumptions, price, scenario, trace=recorder),
        "rental": lambda: rental_fn(record, uw, ctx.assumptions, price, scenario),
        "subject_to": lambda: subject_to_fn(record, uw, ctx.assumptions, price, scenario),
        "foreclosure": lambda: foreclosure_fn(record, uw, ctx.assumptions, price, scenario),
    }
    fn = functions.get(strategy)
    if fn is None:
        raise KeyError(f"unknown strategy '{strategy}'")
    result = fn()
    trace = finish(recorder, key=f"strategy.{strategy}.{scenario.value}",
                   title=f"{strategy.replace('_', ' ').capitalize()} strategy ({scenario.value})",
                   description=_STRATEGY_DESCRIPTIONS[strategy],
                   value=result.profit if result.status != "unavailable" else None,
                   value_kind="calculated", formula=_STRATEGY_FORMULAS[strategy],
                   assumption_set_id=ctx.assumptions.id, computed_at=datetime.now(UTC))
    trace.inputs.append(ExplanationInput(
        name="purchase price used", value=result.inputs_echo.get("purchase_price", price),
        note="from the latest saved offer or recompute; the reported ownership purchase price is the fallback"))
    for metric_name, display in (("mao", "Maximum Allowable Offer"), ("all_in_basis", "All-in basis"),
                                 ("roi", "ROI"), ("margin_of_safety", "Margin of safety")):
        metric_value = getattr(result, metric_name, None)
        trace.children.append(ExplanationTrace(
            key=f"strategy.{strategy}.{scenario.value}.{metric_name}", title=display,
            description="", value=metric_value,
            display_value=show(metric_value) if metric_value is not None else None,
            value_kind="calculated", engine=recorder.engine, engine_version=strategies_version()))
    for metric_name, metric_value in (result.metrics or {}).items():
        trace.children.append(ExplanationTrace(
            key=f"strategy.{strategy}.{scenario.value}.metric.{metric_name}",
            title=metric_name.replace("_", " "), description="", value=metric_value,
            display_value=show(metric_value) if metric_value is not None else None,
            value_kind="calculated", engine=recorder.engine, engine_version=strategies_version()))
    trace.warnings.extend(result.notices)
    if result.unavailable_reason:
        trace.unresolved_dependencies.append(f"Strategy unavailable: {result.unavailable_reason}")
    elif result.status == "not_viable":
        recorder.warning("Not viable at this purchase price: the computed profit is negative.")
        trace.warnings.append("Not viable at this purchase price: the computed profit is negative.")
    if result.mao is not None:
        recorder.sensitivity("What if the value were 10% lower?",
                             f"Profit falls by roughly {show((uw.value.v_expected or ZERO) * Decimal('0.10'))}.")
        repair_cost = (uw.costs.get(scenario).repairs if uw.costs.get(scenario) else None) or ZERO
        recorder.sensitivity("What if repairs ran 25% over budget?",
                             f"Profit falls by about {show(repair_cost * Decimal('0.25'))}.")
        trace.sensitivity = [ExplanationSensitivity(question=e["question"], effect=e["effect"], delta=e.get("delta"))
                             for e in recorder.by_kind("sensitivity")]
    dscr_value = result.metrics.get("dscr")
    if dscr_value is not None and Decimal(str(dscr_value)) < 1:
        trace.warnings.append("DSCR below 1.0 means the rent does not cover the debt service.")
    return trace


def build_offer_simulator(ctx: ExplainContext, scenario: Scenario) -> ExplanationTrace:
    from strategies import ENGINE_VERSION
    from strategies import offer_point as offer_point_fn

    uw, _ = ctx.underwriting()
    if uw is None or uw.status != "ok" or ctx.assumptions is None:
        return unavailable_trace("offers.simulator", "Offer simulator", uw)
    v_exp = uw.value.v_expected
    children = []
    offers = [(v_exp or ZERO) * (Decimal("0.60") + Decimal(index) * Decimal("0.05")) for index in range(9)]
    for offer in offers:
        point_recorder = TraceRecorder(engine="strategies", engine_version=ENGINE_VERSION)
        point = offer_point_fn(uw, ctx.assumptions, offer.quantize(Decimal("0.01")), scenario,
                               trace=point_recorder)
        child = finish(point_recorder, key=f"offer.point.{point.offer_price}",
                       title=f"Offer {show(point.offer_price)}",
                       description="Authoritative server-computed grid point.",
                       value=point.profit, value_kind="calculated",
                       formula="profit = value × (1 − resale%) − buyer basis")
        child.inputs.insert(0, ExplanationInput(name="offer price", value=point.offer_price))
        child.children.append(ExplanationTrace(key=f"offer.point.{point.offer_price}.roi", title="ROI",
                                               description="", value=point.roi,
                                               display_value=show(point.roi) if point.roi is not None else None,
                                               value_kind="calculated"))
        children.append(child)
    return ExplanationTrace(
        key="offers.simulator", title="Offer simulator",
        description=("These are the authoritative server-generated points for the "
                     f"{scenario_label(scenario)} scenario. Values between two points are linearly "
                     "interpolated by the UI and marked as interpolated there; every output is linear in "
                     "the offer price, so interpolation is exact."),
        value=None, value_kind="calculated", engine="strategies", engine_version=ENGINE_VERSION,
        children=children)


# --------------------------------------------------------------------------- score keys


def build_score_overall(ctx: ExplainContext) -> ExplanationTrace:
    scores, config, persisted = ctx.scores()
    if scores is None:
        return unavailable_trace("score.overall", "Overall score", None)
    weights = ((config or {}).get("weights") or {}).get("overall") or {}
    fos_w = weights.get("fos", Decimal("0.50"))
    distress_w = weights.get("distress", Decimal("0.20"))
    dcs_w = weights.get("dcs", Decimal("0.20"))
    risk_w = weights.get("risk", Decimal("0.25"))
    recorder = TraceRecorder(engine="scoring", engine_version=scoring_version())
    recorder.step(label="Overall score",
                  formula="FoS×w_fos + distress×w_distress + DCS×w_dcs − risk×w_risk, clamped to 0-100",
                  inputs={"FoS": scores.fos, "distress": scores.distress,
                          "data confidence": scores.data_confidence, "risk": scores.risk},
                  substitution=f"{show(scores.fos)}×{show(fos_w)} + {show(scores.distress)}×{show(distress_w)}"
                              f" + {show(scores.data_confidence)}×{show(dcs_w)} − {show(scores.risk)}×{show(risk_w)}",
                  result=scores.overall)
    trace = finish(recorder, key="score.overall", title="Overall score",
                   description="Combines financial opportunity, distress motivation, data confidence, and "
                               "risk into one 0-100 ranking number using the active scoring configuration.",
                   value=scores.overall, value_kind="calculated",
                   formula="FoS×w + distress×w + DCS×w − risk×w (active config weights)",
                   scoring_config_id=scores.scoring_config_id,
                   computed_at=persisted.computed_at if persisted else None)
    from .builders_scores_terms import build_score_component

    for component in ("fos", "distress", "data_confidence", "risk"):
        trace.children.append(build_score_component(ctx, component))
    if scores.gates_applied:
        trace.warnings.extend(f"Gate applied: {gate}" for gate in scores.gates_applied)
        for gate, plain in (("insufficient_data", "Underwriting lacks the data to compute a full analysis."),
                            ("dcs_below_40", "Data confidence is too low, so the overall score is capped."),
                            ("foreclosure_cap", "Active foreclosure with low data confidence caps the score."),
                            ("open_gating_flag", "An open gating flag blocks ranking until resolved.")):
            if gate in scores.gates_applied:
                trace.warnings.append(plain)
    return trace


def build_recommendation(ctx: ExplainContext) -> ExplanationTrace:
    scores, _, persisted = ctx.scores()
    strategies = ctx.strategies()
    viable = [item for item in strategies
              if item.scenario == Scenario.EXPECTED and item.status == "viable"
              and item.strategy.value != "subject_to"]
    ranked = sorted(viable, key=lambda item: item.profit or ZERO, reverse=True)
    winner = scores.recommended_strategy if scores is not None else None
    recorder = TraceRecorder(engine="scoring", engine_version=scoring_version())
    if winner is not None:
        recorder.step(label="Recommended strategy selection",
                      formula="highest normalized expected profit among viable strategies; near-ties within "
                              "the configured band become alternatives; ties break by fixed priority",
                      inputs={item.strategy.value: item.profit for item in ranked},
                      substitution=", ".join(f"{item.strategy.value}={show(item.profit)}" for item in ranked),
                      result=winner.value)
    trace = finish(recorder, key="recommendation.strategy", title="Recommended strategy",
                   description="ACQ recommends the viable strategy with the highest normalized expected "
                               "profit at the current purchase-price basis; near-ties become alternatives.",
                   value=winner.value if winner is not None else None,
                   value_kind="derived" if winner is not None else "estimated",
                   computed_at=persisted.computed_at if persisted else None)
    trace.display_value = str(winner.value).replace("_", " ") if winner is not None else None
    leader_profit = ranked[0].profit if ranked else ZERO
    for item in ranked[:4]:
        is_winner = winner is not None and item.strategy == winner
        trace.candidates.append(ExplanationCandidate(
            value=item.profit, display_value=f"{item.strategy.value}: expected profit {show(item.profit)}",
            origin="derived", is_winner=is_winner,
            reason=("recommended: highest expected profit among viable strategies" if is_winner
                    else ("near-tie alternative" if scores is not None and item.strategy in scores.recommended_alternatives
                          else f"trails the leader by {show(leader_profit - (item.profit or ZERO))} of profit"))))
    if winner is None:
        trace.unresolved_dependencies.append(
            "No viable strategy at the current purchase price — nothing can be recommended yet.")
        trace.resolution = ExplanationResolution(method="none-viable",
                                                 winner_description="no viable strategies",
                                                 reason="Every strategy was gated or unprofitable at this basis.")
    return trace


def build_rank(ctx: ExplainContext) -> ExplanationTrace:
    """Why this property ranks where it does: rank position plus component
    differences against its nearest ranked neighbors."""
    from db import models as dbm

    session, property_id = ctx.session, ctx.property_id
    row = (session.query(dbm.Ranking)
           .filter(dbm.Ranking.scope_type == "portfolio", dbm.Ranking.property_id == property_id)
           .order_by(dbm.Ranking.ranked_at.desc())
           .first())
    trace = ExplanationTrace(key="rank", title="Portfolio rank",
                             description="Where this property sits in the portfolio ordering, driven by the "
                                         "overall score and compared against its nearest neighbor.",
                             value=row.rank if row is not None else None,
                             display_value=f"#{row.rank}" if row is not None else None,
                             value_kind="derived")
    if row is None:
        trace.unresolved_dependencies.append(
            "No portfolio ranking snapshot exists yet — run a recompute/rank first.")
        return trace
    neighbors = (session.query(dbm.Ranking)
                 .filter(dbm.Ranking.scope_type == "portfolio",
                         dbm.Ranking.ranked_at == row.ranked_at,
                         dbm.Ranking.rank.in_([int(row.rank or 0) - 1, int(row.rank or 0) + 1]))
                 .all())
    own_score = _latest_overall(session, property_id)
    for neighbor in neighbors:
        other = neighbor.property_id
        if other == property_id:
            continue
        other_score = _latest_overall(session, other)
        rank_delta = (neighbor.rank or 0) - (row.rank or 0)
        direction = "above" if rank_delta > 0 else "below"
        detail = ""
        if own_score is not None and other_score is not None:
            detail = (f"This property ranks #{row.rank}, one place "
                      f"{'ahead of' if direction == 'above' else 'behind'} #{neighbor.rank}: "
                      f"overall score {show(own_score)} vs {show(other_score)}.")
            differences = _component_differences(_components(session, property_id),
                                                 _components(session, other))
            if differences:
                detail += " Largest component differences: " + ", ".join(differences[:3]) + "."
        trace.children.append(ExplanationTrace(
            key=f"rank.vs.{neighbor.rank}", title=f"vs rank #{neighbor.rank}",
            description=detail or f"The #{neighbor.rank} property compared against this one.",
            value=None, value_kind="derived"))
    if not trace.children:
        trace.warnings.append("No neighboring ranked property was found for comparison.")
    return trace


_DIFFERENCE_COMPONENTS = ("profit", "roi", "fos_profit_norm", "fos_roi_norm",
                          "distress_nts", "risk_liens", "dcs_field_coverage")


def _component_differences(own: dict[str, Decimal], other: dict[str, Decimal]) -> list[str]:
    differences = []
    for name in _DIFFERENCE_COMPONENTS:
        if name in own or name in other:
            delta = own.get(name, ZERO) - other.get(name, ZERO)
            if abs(delta) > ZERO:
                differences.append(f"{name.replace('_', ' ')} {'+' if delta > 0 else ''}{show(delta)}")
    return differences


def _latest_score_row(session: Session, property_id):
    from db import models as dbm

    return (session.query(dbm.Score)
            .filter(dbm.Score.property_id == property_id)
            .order_by(dbm.Score.computed_at.desc())
            .first())


def _latest_overall(session: Session, property_id) -> Any:
    row = _latest_score_row(session, property_id)
    return row.overall if row is not None else None


def _components(session: Session, property_id) -> dict[str, Decimal]:
    row = _latest_score_row(session, property_id)
    components = (row.components if row is not None else None) or {}
    return {name: Decimal(str(value)) for name, value in components.items()
            if isinstance(value, (int, float, str))}
