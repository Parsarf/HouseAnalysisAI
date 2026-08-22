"""Explanation-trace builders: valuation, liabilities, equity, repairs, costs.

Every builder re-runs the exact engine function that produces the persisted
figure with a ``TraceRecorder`` attached, then assembles an
``ExplanationTrace`` from the recorded entries plus source-evidence lookups.
This layer contains no financial formulas of its own.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from common.trace import TraceRecorder, show
from contracts import (
    ExplanationAssumption,
    ExplanationCandidate,
    ExplanationConflict,
    ExplanationInput,
    ExplanationResolution,
    ExplanationSensitivity,
    ExplanationStep,
    ExplanationTrace,
    NormalizedProperty,
    Scenario,
    SourceKind,
    ValueKind,
)

from . import sources as source_store
from . import store

ZERO = Decimal(0)
ONE = Decimal(1)
SCENARIOS = (Scenario.CONSERVATIVE, Scenario.EXPECTED, Scenario.OPTIMISTIC)


def finance_version() -> str:
    from finance import ENGINE_VERSION
    return ENGINE_VERSION


def strategies_version() -> str:
    from strategies import ENGINE_VERSION
    return ENGINE_VERSION


def scoring_version() -> str:
    from scoring import ENGINE_VERSION
    return ENGINE_VERSION


# --------------------------------------------------------------------------- context


class ExplainContext:
    """Per-request bundle shared by all builders."""

    def __init__(self, session: Session, property_id: UUID):
        self.session = session
        self.property_id = property_id
        self.normalized: NormalizedProperty | None = store.load_normalized(session, property_id)
        self.assumptions = store.load_assumption_set(session)
        self._underwriting: tuple[Any, TraceRecorder] | None = None

    def underwriting(self) -> tuple[Any, TraceRecorder]:
        """Underwriting via the same canonical path the deal page uses."""
        if self._underwriting is not None:
            return self._underwriting
        recorder = TraceRecorder(engine="finance", engine_version=finance_version())
        result = None
        if self.normalized is not None and self.assumptions is not None:
            from report_analysis.normalizer import underwrite_canonical

            result = underwrite_canonical(self.normalized, self.assumptions, trace=recorder)
        self._underwriting = (result, recorder)
        return self._underwriting

    def purchase_price(self) -> Decimal:
        if self.normalized is None:
            return ZERO
        return store.load_purchase_price(self.session, self.property_id, self.normalized)

    def strategies(self):
        record, uw = self.normalized, self.underwriting()[0]
        if record is None or uw is None or self.assumptions is None:
            return []
        from scoring import data_confidence as scoring_dcs
        from strategies import all_strategies

        _, config = store.load_scoring_config(self.session)
        dcs = scoring_dcs(record, config=config)
        wholesale_min = Decimal(str(((config or {}).get("gates") or {}).get("wholesale_min", 60)))
        return all_strategies(record, uw, self.assumptions, self.purchase_price(),
                              data_confidence_value=dcs, wholesale_min=wholesale_min)

    def scores(self):
        """(recomputed ScoreSet via the engine, config, persisted row).

        The recency anchor is the persisted computation date when available,
        otherwise the record's newest report date — never wall-clock — so a
        historical result always explains itself with its own dates."""
        record, uw = self.normalized, self.underwriting()[0]
        if record is None or uw is None:
            return None, None, None
        from scoring import score as scoring_score

        config_id, config = store.load_scoring_config(self.session)
        persisted = store.load_persisted_score(self.session, self.property_id)
        as_of = (persisted.computed_at.date()
                 if persisted is not None and persisted.computed_at is not None
                 else record.data_quality.newest_report_date)
        result = scoring_score(record, uw, config_id, strategies=self.strategies(), config=config,
                               as_of=as_of, trace=None)
        return result, config, persisted


# --------------------------------------------------------------------------- assembly


def finish(recorder: TraceRecorder, *, key: str, title: str, description: str,
           value: Any, value_kind: ValueKind, formula: str | None = None,
           confidence: Decimal | None = None, data_confidence: Decimal | None = None,
           assumption_set_id=None, scoring_config_id=None, computed_at=None) -> ExplanationTrace:
    steps = [ExplanationStep(order=order, label=entry["label"], formula=entry.get("formula"),
                             substitution=entry.get("substitution"), result=entry.get("result"),
                             display_result=entry.get("display_result"))
             for order, entry in enumerate(recorder.steps, start=1)]
    inputs = [ExplanationInput(name=e["label"], value=e.get("value"), display_value=e.get("display_value"),
                               note=e.get("note"), source_fact_id=e.get("source_fact_id"))
              for e in recorder.by_kind("input")]
    assumptions = [ExplanationAssumption(name=e["label"], value=e.get("value"),
                                         display_value=e.get("display_value"), note=e.get("note"),
                                         assumption_set_id=e.get("assumption_set_id"))
                   for e in recorder.by_kind("assumption")]
    candidates = [ExplanationCandidate(
        value=e.get("value"), display_value=e.get("display_value") or show(e["value"]) if e.get("value") is not None else None,
        confidence=e.get("confidence"), origin=e.get("origin"), is_winner=bool(e.get("is_winner")),
        reason=e.get("reason")) for e in recorder.by_kind("candidate")]
    resolution_entries = recorder.by_kind("resolution")
    resolution = (ExplanationResolution(method=resolution_entries[0]["method"],
                                        winner_description=resolution_entries[0]["winner_description"],
                                        reason=resolution_entries[0]["reason"])
                  if resolution_entries else None)
    return ExplanationTrace(
        key=key, title=title, description=description,
        value=value, display_value=show(value) if value is not None else None,
        value_kind=value_kind,
        confidence=confidence, data_confidence=data_confidence,
        engine=recorder.engine, engine_version=recorder.engine_version,
        formula=formula, formula_display=formula,
        inputs=inputs, steps=steps, assumptions=assumptions, candidates=candidates,
        resolution=resolution, warnings=recorder.warnings,
        unresolved_dependencies=[e["message"] for e in recorder.by_kind("unresolved")],
        conflicts=[ExplanationConflict(description=e["description"],
                                       magnitude=show(e["magnitude"]) if e.get("magnitude") is not None else None)
                   for e in recorder.by_kind("conflict")],
        sensitivity=[ExplanationSensitivity(question=e["question"], effect=e["effect"], delta=e.get("delta"))
                     for e in recorder.by_kind("sensitivity")],
        assumption_set_id=assumption_set_id, scoring_config_id=scoring_config_id,
        computed_at=computed_at)


def unavailable_trace(key: str, title: str, uw=None) -> ExplanationTrace:
    trace = ExplanationTrace(key=key, title=title,
                             description="This figure cannot be produced yet.", value=None,
                             value_kind="estimated")
    if uw is not None and getattr(uw, "status", None) != "ok":
        trace.unresolved_dependencies.append(
            f"Underwriting status: {getattr(uw, 'status', 'unknown')} "
            f"({getattr(uw, 'unavailable_reason', '') or 'no further detail'})")
    else:
        trace.unresolved_dependencies.append("No analysis has been computed for this property yet.")
    return trace


def slice_recorder(recorder: TraceRecorder, *prefixes: str) -> TraceRecorder:
    sliced = TraceRecorder(engine=recorder.engine, engine_version=recorder.engine_version)
    sliced.entries = [entry for entry in recorder.entries
                      if any(entry.get("label", "").startswith(prefix) for prefix in prefixes)]
    return sliced


def scenario_label(scenario: Scenario) -> str:
    return {Scenario.CONSERVATIVE: "conservative (V low)",
            Scenario.EXPECTED: "expected",
            Scenario.OPTIMISTIC: "optimistic (V high)"}[scenario]


def scenario_value(uw, scenario: Scenario) -> Decimal | None:
    return {Scenario.CONSERVATIVE: uw.value.v_low,
            Scenario.EXPECTED: uw.value.v_expected,
            Scenario.OPTIMISTIC: uw.value.v_high}[scenario]


# --------------------------------------------------------------------------- value keys


def build_value(ctx: ExplainContext, which: str) -> ExplanationTrace:
    uw, recorder = ctx.underwriting()
    if uw is None or uw.status != "ok":
        return unavailable_trace(f"value.{which}", "Expected market value", uw)
    attr = {"expected": ("v_expected", "Expected market value"),
            "low": ("v_low", "Conservative value (V low)"),
            "high": ("v_high", "Optimistic value (V high)")}[which]
    value = getattr(uw.value, attr[0])
    trace = finish(recorder, key=f"value.{which}", title=attr[1],
                   description="A weighted blend of every valuation candidate extracted from your reports "
                               "(AVM, comparable-sale estimate, comparable listing, assessed value, list "
                               "price). The candidates are reported figures; the blend itself is calculated.",
                   value=value, value_kind="calculated",
                   formula="Σ(candidate × adjusted weight) / Σ(weights)",
                   confidence=uw.value.valuation_confidence,
                   data_confidence=(ctx.normalized.data_quality.mean_extraction_confidence
                                    if ctx.normalized else None))
    trace.candidates = _valuation_candidates(ctx, recorder)
    trace.source_facts = source_store.sources_for_property(
        ctx.session, ctx.property_id, entity_types={"valuation", "tax"},
        path_fragments=("value",))
    trace.resolution = ExplanationResolution(
        method="weighted_blend_v1",
        winner_description="All valuation candidates contribute; none is chosen alone.",
        reason="Each candidate's weight reflects source type, reported confidence, recency, and comparable quality.")
    if value is not None:
        drop = (value * Decimal("0.10")).quantize(Decimal("0.01"))
        recorder.sensitivity("If the expected value were 10% lower?",
                             f"Equity and cash-strategy profit fall by roughly {show(drop)}; "
                             "the overall score falls with them.", delta=-drop)
        trace.sensitivity = [ExplanationSensitivity(question=e["question"], effect=e["effect"], delta=e.get("delta"))
                             for e in recorder.by_kind("sensitivity")]
    return trace


def _valuation_candidates(ctx: ExplainContext, recorder: TraceRecorder) -> list[ExplanationCandidate]:
    candidates: list[ExplanationCandidate] = []
    if ctx.normalized is None:
        return candidates
    weight_steps = {step["label"]: step for step in recorder.steps if "candidate weight" in step["label"]}
    for candidate in ctx.normalized.valuation_candidates:
        if candidate.value.value is None:
            continue
        kind = candidate.valuation_type.strip().casefold()
        label = next((key for key in weight_steps if f"({kind})" in key), None)
        weight = weight_steps[label]["result"] if label else None
        db_candidates, _method, _reason = source_store.candidates_for_field(
            ctx.session, ctx.property_id, f"valuation.{kind}.value")
        winner_source = max(db_candidates, key=lambda item: item.confidence or ZERO, default=None)
        candidates.append(ExplanationCandidate(
            value=candidate.value.value, display_value=show(candidate.value.value),
            confidence=Decimal(str(candidate.reported_confidence or candidate.value.confidence)),
            source_kind=candidate.value.source_kind.value,
            origin=("extracted" if candidate.value.source_kind == SourceKind.REPORT else "reported"),
            is_winner=True,
            reason=(f"included in the weighted blend with adjusted weight {show(weight)}"
                    if weight is not None else "included in the weighted blend"),
            source=winner_source.source if winner_source is not None else None))
    return candidates


def build_arv(ctx: ExplainContext) -> ExplanationTrace:
    uw, recorder = ctx.underwriting()
    if uw is None or uw.status != "ok":
        return unavailable_trace("value.arv", "After-repair value (ARV)", uw)
    arv = uw.value.arv_by_scenario.get(Scenario.EXPECTED)
    trace = finish(slice_recorder(recorder, "After-repair value", "Base repair budget", "Repairs ("),
                   key="value.arv", title="After-repair value (ARV)",
                   description="What the property could sell for after the estimated repairs are completed: "
                               "expected value plus repair budget (recapture multiplier 1.0).",
                   value=arv, value_kind="calculated",
                   formula="ARV = value + repairs",
                   confidence=uw.value.valuation_confidence,
                   data_confidence=(ctx.normalized.data_quality.mean_extraction_confidence
                                    if ctx.normalized else None))
    if arv is None:
        trace.unresolved_dependencies.append(
            "ARV requires square footage to estimate repairs; sqft was never extracted.")
    trace.warnings.append("ARV inherits every uncertainty of the value estimate and the repair budget.")
    return trace


# --------------------------------------------------------------------------- liabilities


_LIABILITY_TITLES = {
    "confirmed": "Confirmed liabilities",
    "potential": "Potential liabilities",
    "maximum": "Maximum exposure (confirmed + potential)",
}
_LIABILITY_DESCRIPTIONS = {
    "confirmed": "Debt recorded against the property or otherwise confirmed: open mortgage balances, "
                 "recorded liens, delinquent taxes and HOA arrears. Nothing unresolved is counted here — "
                 "conditional items live under Potential.",
    "potential": "Obligations that may never attach: owner-named-only liens, unknown-amount liens valued "
                 "at type medians, and undrawn HELOC capacity. Expected amounts apply attachment probabilities.",
    "maximum": "Worst case if every potential obligation attached in full on top of confirmed debt.",
}


def build_liabilities(ctx: ExplainContext, bucket: str) -> ExplanationTrace:
    uw, recorder = ctx.underwriting()
    trace = finish(recorder, key=f"liabilities.{bucket}", title=_LIABILITY_TITLES[bucket],
                   description=_LIABILITY_DESCRIPTIONS[bucket],
                   value=getattr(uw.liabilities, bucket) if uw is not None and uw.liabilities else None,
                   value_kind="calculated",
                   formula={"confirmed": "Σ confirmed mortgage balances + recorded liens + taxes/HOA arrears",
                            "potential": "Σ potential amounts (owner-named liens, HELOC capacity, medians)",
                            "maximum": "confirmed + potential"}[bucket])
    if uw is not None and uw.liabilities is not None:
        for index, item in enumerate(uw.liabilities.breakdown):
            amount = item.get("amount")
            basis = item.get("basis", "")
            is_estimated = bool(item.get("is_estimated"))
            child = ExplanationTrace(
                key=f"liabilities.line.{index}", title=str(item.get("label", "obligation")).replace(":", " — "),
                description=("An estimated or conditional obligation — NOT part of confirmed liabilities."
                             if bucket == "potential" or is_estimated else
                             "A confirmed obligation included in the confirmed-liability total."),
                value=amount, display_value=show(amount) if amount is not None else None,
                value_kind="estimated" if is_estimated else "extracted",
                engine=recorder.engine, engine_version=recorder.engine_version,
                warnings=["Estimated balance — not a lender payoff statement."]
                if is_estimated and "amortization" in basis else [],
                unresolved_dependencies=["No reliable amount was extracted; a median estimate stands in."]
                if basis in ("heloc_capacity_no_draw_data", "median_unknown_amount") else [])
            if item.get("expected_amount") is not None:
                child.steps = [ExplanationStep(order=1, label="Probability-weighted expected amount",
                                               formula="amount × attachment probability",
                                               substitution=f"{show(item.get('amount'))} × probability → "
                                                            f"{show(item['expected_amount'])}",
                                               result=item["expected_amount"])]
            trace.children.append(child)
    trace.source_facts = source_store.sources_for_property(
        ctx.session, ctx.property_id, entity_types={"mortgage", "lien", "tax", "foreclosure"})
    if uw is not None and getattr(uw, "debt_data_present", True) is False:
        trace.unresolved_dependencies.append("No mortgage, lien, or foreclosure records were found at all.")
    if uw is not None and uw.liabilities is not None and ctx.normalized is not None:
        for lien in ctx.normalized.liens:
            if (lien.amount is None or lien.amount.value is None) and \
                    lien.status.casefold() not in {"closed", "paid", "released", "satisfied"}:
                    recorder.sensitivity(
                        f"What if the {lien.lien_type} lien's real payoff were $25,000?",
                        "Potential obligations and maximum exposure move by the difference between the "
                        "actual payoff and the assumed median; expected equity moves by the weighted amount.",
                        delta=None)
                    break
    trace.sensitivity = [ExplanationSensitivity(question=e["question"], effect=e["effect"], delta=e.get("delta"))
                         for e in recorder.by_kind("sensitivity")]
    return trace


# --------------------------------------------------------------------------- equity / repairs / costs


def build_equity(ctx: ExplainContext, scenario: Scenario) -> ExplanationTrace:
    uw, recorder = ctx.underwriting()
    block = uw.equity.get(scenario) if uw is not None else None
    trace = finish(slice_recorder(recorder, "Gross equity", "Adjusted equity", "Net realizable",
                                  "Confirmed obligations total", "Potential obligations total"),
                   key=f"equity.{scenario.value}",
                   title=f"{scenario_label(scenario).capitalize()} equity",
                   description="Equity is what remains for the seller after debt. Gross uses confirmed debt "
                               "only; adjusted also subtracts this scenario's potential bucket; net realizable "
                               "also subtracts resale costs and holding.",
                   value=block.adjusted if block else None, value_kind="calculated",
                   formula="adjusted equity = value − confirmed − scenario potential bucket")
    if block is not None:
        for name, val, label in (("gross", block.gross, "Gross equity"),
                                 ("adjusted", block.adjusted, "Adjusted equity"),
                                 ("net_realizable", block.net_realizable, "Net realizable equity"),
                                 ("equity_pct", block.equity_pct, "Equity percentage")):
            trace.children.append(ExplanationTrace(
                key=f"equity.{scenario.value}.{name}", title=label,
                description="", value=val,
                display_value=show(val) if val is not None else None,
                value_kind="calculated", engine=recorder.engine, engine_version=recorder.engine_version))
    recorder.input("Potential bucket used", scenario.value,
                   note="conservative subtracts full potential; expected subtracts the probability-weighted "
                        "amount; optimistic subtracts nothing")
    trace.inputs.extend([ExplanationInput(name=e["label"], value=e.get("value"), note=e.get("note"))
                         for e in recorder.by_kind("input")])
    trace.warnings.append("Equity depends on the estimated market value and liability completeness; "
                          "undiscovered liens would reduce it.")
    value = scenario_value(uw, scenario) if uw is not None and uw.status == "ok" else None
    if value is not None:
        drop = (value * Decimal("0.10")).quantize(Decimal("0.01"))
        recorder.sensitivity("If this property's value were 10% lower?",
                             f"{scenario_label(scenario).capitalize()} equity falls by about {show(drop)} "
                             "(equity moves dollar-for-dollar with value).", delta=-drop)
        trace.sensitivity = [ExplanationSensitivity(question=e["question"], effect=e["effect"], delta=e.get("delta"))
                             for e in recorder.by_kind("sensitivity")]
    return trace


def build_repairs(ctx: ExplainContext, scenario: Scenario) -> ExplanationTrace:
    uw, recorder = ctx.underwriting()
    cost = uw.costs.get(scenario) if uw is not None else None
    trace = finish(slice_recorder(recorder, "Base repair budget", "Repairs (", "Condition level",
                                  "$ per sqft", "Repair $/sqft", "Regional repair index"),
                   key=f"repairs.{scenario.value}", title=f"Repair budget ({scenario.value})",
                   description="ACQ estimates repairs from square footage and condition using fixed $/sqft "
                               "assumptions — never from an LLM-provided dollar figure.",
                   value=cost.repairs if cost else None, value_kind="estimated",
                   formula="sqft × $/sqft(condition) × regional index × scenario multiplier",
                   assumption_set_id=ctx.assumptions.id if ctx.assumptions else None)
    trace.warnings.append("Planning estimate from published $/sqft assumptions, not a contractor bid.")
    if ctx.normalized is not None and (ctx.normalized.attributes.sqft is None
                                       or ctx.normalized.attributes.sqft.value is None):
        trace.unresolved_dependencies.append(
            "Square footage was never extracted, so no repair budget can be estimated (never silently $0).")
    return trace


_COST_LABELS = {
    "acquisition": ("Acquisition costs", "purchase × acq% + flat",
                    "Title, escrow, inspection, legal, transfer tax and acquisition fees.",
                    ("Acquisition cost rate", "Acquisition flat costs")),
    "holding": ("Holding costs", "monthly carrying cost × months held",
                "Taxes, insurance, maintenance, utilities and HOA while you own it.",
                ("Holding cost ", "Monthly carrying cost", "Holding period ")),
    "resale": ("Resale costs", "value × resale%",
               "Agent commission, seller closing costs, concessions and misc.",
               ("Resale cost rate", "Resale costs ")),
}


def build_cost(ctx: ExplainContext, which: str, scenario: Scenario) -> ExplanationTrace:
    uw, recorder = ctx.underwriting()
    cost = uw.costs.get(scenario) if uw is not None else None
    title, formula, description, prefixes = _COST_LABELS[which]
    value = getattr(cost, which) if cost else None
    trace = finish(slice_recorder(recorder, *prefixes), key=f"costs.{which}.{scenario.value}",
                   title=f"{title} ({scenario.value})", description=description,
                   value=value, value_kind="calculated", formula=formula,
                   assumption_set_id=ctx.assumptions.id if ctx.assumptions else None)
    trace.assumptions = _cost_assumptions(ctx, which)
    if which == "holding" and cost is not None:
        months_step = next((step for step in recorder.steps
                            if step["label"].startswith("Holding period (")), None)
        if months_step is not None:
            trace.inputs.append(ExplanationInput(name="holding period (months)", value=months_step["result"]))
    return trace


def _cost_assumptions(ctx: ExplainContext, which: str) -> list[ExplanationAssumption]:
    if ctx.assumptions is None:
        return []
    items: tuple[tuple[str, Any], ...]
    if which == "acquisition":
        a = ctx.assumptions.acquisition
        items = (("closing %", a.closing_pct), ("title %", a.title_pct), ("escrow flat", a.escrow_flat),
                 ("inspection", a.inspection_flat), ("legal", a.legal_flat),
                 ("transfer tax lookup key", a.transfer_tax_lookup_key), ("acquisition fee %", a.acq_fee_pct))
    elif which == "holding":
        h = ctx.assumptions.holding
        items = (("insurance %/yr", h.insurance_pct_yr), ("utilities monthly", h.utilities_monthly),
                 ("maintenance %/yr", h.maintenance_pct_yr), ("acquisition months", h.acquisition_months),
                 ("market days default", h.market_days_default))
    else:
        r = ctx.assumptions.resale
        items = (("commission %", r.commission_pct), ("seller closing %", r.seller_closing_pct),
                 ("concessions %", r.concessions_pct), ("staging flat", r.staging_flat),
                 ("misc %", r.misc_pct))
    return [ExplanationAssumption(name=name, value=value, display_value=show(value),
                                  assumption_set_id=ctx.assumptions.id)
            for name, value in items]
