"""Score-component explanation builders (FoS, distress, DCS, risk).

The sub-terms come from ``ScoreSet.components`` — the same emitted values the
engines persist — so this module renders real engine output, never its own
recomputation.
"""
from __future__ import annotations

from decimal import Decimal

from common.trace import show
from contracts import ExplanationTrace

from .builder import ExplainContext, scoring_version, unavailable_trace


def build_score_component(ctx: ExplainContext, component: str) -> ExplanationTrace:
    from scoring.engine import DEFAULT_CONFIG
    from scoring.engine import resolve_config as resolve_scoring_config

    scores, config, persisted = ctx.scores()
    if scores is None:
        return unavailable_trace(f"score.{component}", component.replace("_", " "), None)
    resolved = resolve_scoring_config(config)
    titles = {
        "fos": (
            "Financial Opportunity Score",
            ("How attractive the deal looks financially: profit, ROI, equity percentage, discount to "
            "value, and margin of safety are each clamped into configured bounds and weighted.")),
        "distress": (
            "Distress score",
            ("Evidence that the owner may be under pressure to sell: foreclosure filings, bankruptcies, "
            "tax liens, absentee ownership, failed listings. Points decay as events age.")),
        "data_confidence": (
            "Data Confidence Score",
            ("How complete and trustworthy the underlying data is: critical-field coverage, corroboration "
            "across reports, recency, conflicts, human verification and extraction quality. Extraction "
            "confidence reflects confidence in the extracted information — not a probability that a fact is "
            "legally valid.")),
        "risk": (
            "Risk score",
            ("Deal risks that subtract from the overall score: liens, bankruptcy, foreclosure stage, title "
            "flags, occupancy, material conflicts, and low data confidence.")),
    }
    title, description = titles[component]
    components = scores.components or {}
    trace = ExplanationTrace(
        key=f"score.{component}", title=title, description=description,
        value=getattr(scores, component), display_value=show(getattr(scores, component)),
        value_kind="calculated", engine="scoring", engine_version=scoring_version(),
        formula={"fos": "100 × Σ(weight × clamped term)",
                 "distress": "Σ base points × recency decay 0.5^(months/18), capped at 100",
                 "data_confidence": "100 × Σ weight × coverage / corroboration / recency / conflict-free / verification / extraction",
                 "risk": "Σ risk points, capped at 100"}[component],
        scoring_config_id=scores.scoring_config_id,
        computed_at=persisted.computed_at if persisted else None)
    prefix_for = {"fos": "fos_", "distress": "distress_", "data_confidence": "dcs_", "risk": "risk_"}
    section = _section(resolved, component)
    points_cfg = _points(resolved, component)
    for name in sorted(components):
        if not name.startswith(prefix_for[component]):
            continue
        raw_value = Decimal(str(components[name]))
        weight = section.get(_weight_key(name))
        contribution = None
        if weight is not None:
            contribution = Decimal(100) * Decimal(str(weight)) * raw_value
        base_points = points_cfg.get(_point_key(name))
        child = ExplanationTrace(
            key=f"score.{component}.{name}",
            title=_plain_name(name),
            description=_term_description(name, base_points, weight),
            value=raw_value, display_value=show(raw_value),
            value_kind="calculated", engine="scoring", engine_version=scoring_version())
        if contribution is not None:
            child.steps.append(_step(1, f"{_plain_name(name)} contributes",
                                     formula=f"term × weight {show(weight)} × 100",
                                     substitution=f"{show(raw_value)} × {show(weight)} × 100",
                                     result=(contribution.quantize(Decimal('0.0001')))))
        if base_points is not None:
            child.inputs.append(_input("configured base points", base_points))
        trace.children.append(child)
    if component == "fos":
        trace.formula = (trace.formula or "") + "; terms outside their bounds clamp to 0 or 1"
        if components.get("fos_profit_norm") == 0 and not scores.components.get("profit"):
            trace.unresolved_dependencies.append(
                "No viable recommended strategy, so profit/ROI terms contribute nothing.")
    if component == "data_confidence":
        missing = _missing_critical_fields(ctx)
        if missing:
            trace.unresolved_dependencies.append(
                "Important fields with no extracted value: " + ", ".join(missing) + ".")
        dq = ctx.normalized.data_quality if ctx.normalized is not None else None
        if dq is not None:
            coverage_pct = (dq.critical_field_coverage * 100)
            trace.inputs.append(_input("critical field coverage",
                                       f"{show(coverage_pct.quantize(Decimal('0.1')))}%"))
            corroborated = sum(1 for count in dq.source_counts_by_field.values() if count >= 2)
            trace.inputs.append(_input("fields corroborated by 2+ sources", corroborated))
            trace.inputs.append(_input("material conflicts", dq.material_conflict_count))
            trace.inputs.append(_input("human-verified fields", dq.verified_field_count))
            mean_conf = dq.mean_extraction_confidence
            trace.warnings.append(
                f"Mean extraction confidence is {show(mean_conf * 100)}%. This reflects confidence in the "
                "extracted text, not a probability that the underlying facts are legally valid.")
    if component == "risk" and getattr(scores, "components", None) and \
            Decimal(str(components.get("risk_low_dcs", 0))) > 0:
        gates_cfg = ((config or {}).get("gates") if config else None)
        threshold = (gates_cfg or DEFAULT_CONFIG["gates"]).get("dcs_low_threshold", 50) \
            if isinstance((config or {}).get("gates"), dict) else DEFAULT_CONFIG["gates"]["dcs_low_threshold"]
        trace.children.append(ExplanationTrace(
            key="score.risk.low_dcs.threshold", title="Low-confidence gate",
            description=f"Applied because the Data Confidence Score fell below {show(threshold)}.",
            value=None, value_kind="calculated"))
    return trace


def _section(resolved: dict, component: str) -> dict:
    weights = resolved.get("weights") or {}
    return dict(weights.get(component) or {})


def _points(resolved: dict, component: str) -> dict:
    if component == "distress":
        return {key: Decimal(str(value)) for key, value in (resolved.get("distress_points") or {}).items()}
    if component == "risk":
        merged = dict(DEFAULT_RISK_POINTS)
        for key, value in (resolved.get("risk_points") or {}).items():
            merged[key] = Decimal(str(value))
        return merged
    return {}


DEFAULT_RISK_POINTS = {
    name: Decimal(str(value)) for name, value in
    (("lien_count", 6), ("active_bankruptcy", 15), ("foreclosure_stage", 12), ("owner_only_lien", 10),
     ("title_flag", 10), ("owner_occupied", 8), ("hoa_arrears", 8), ("material_conflict", 10),
     ("low_confidence", 12), ("federal_tax_lien", 6))
}

_WEIGHT_ALIASES = {
    "fos_profit_norm": "profit", "fos_roi_norm": "roi", "fos_equity_pct_norm": "equity_pct",
    "fos_discount_to_value_norm": "discount_to_value", "fos_margin_of_safety_norm": "margin_of_safety",
    "dcs_field_coverage": "coverage", "dcs_corroboration": "corroboration", "dcs_recency": "recency",
    "dcs_conflict_free": "conflict", "dcs_verification": "verification",
    "dcs_extraction_quality": "extraction",
}


def _weight_key(term: str) -> str | None:
    return _WEIGHT_ALIASES.get(term)


_POINT_KEYS = {
    "distress_nts": ["nts_near", "nts_far"], "distress_nod": ["nod"],
    "distress_prior_foreclosure": ["prior_foreclosure_each"],
    "distress_bankruptcy_active": ["bankruptcy_active"],
    "distress_bankruptcy_prior": ["bankruptcy_prior_each", "bankruptcy_prior_cap"],
    "distress_repeat_filings": ["repeat_filings"],
    "distress_tax_lien_attached": ["tax_lien_property"],
    "distress_tax_lien_owner_only": ["tax_lien_owner"],
    "distress_other_involuntary_liens": ["other_lien_each", "other_lien_cap"],
    "distress_taxes_delinquent_2yr": ["taxes_delinquent"],
    "distress_absentee": ["absentee"], "distress_owned_over_15yr": ["long_ownership"],
    "distress_listing_expired": ["listing_failure_each", "listing_failure_cap"],
    "distress_high_equity_bonus": ["high_equity_bonus"],
    "risk_liens": ["lien_count"], "risk_bankruptcy": ["active_bankruptcy"],
    "risk_foreclosure_stage": ["foreclosure_stage"],
    "risk_owner_only_liens_over_10k": ["owner_only_lien"],
    "risk_title_flags": ["title_flag"], "risk_owner_occupied": ["owner_occupied"],
    "risk_hoa_arrears": ["hoa_arrears"], "risk_material_conflicts": ["material_conflict"],
    "risk_low_dcs": ["low_confidence"], "risk_federal_tax_lien": ["federal_tax_lien"],
}


def _point_key(term: str):
    keys = _POINT_KEYS.get(term)
    if not keys:
        return None
    return keys[-1]


_PLAIN_NAMES = {
    "fos_profit_norm": "Normalized profit (clamped into configured bounds)",
    "fos_roi_norm": "Normalized ROI",
    "fos_equity_pct_norm": "Normalized equity percentage",
    "fos_discount_to_value_norm": "Normalized discount to expected value",
    "fos_margin_of_safety_norm": "Normalized margin of safety",
    "distress_nts": "Notice of trustee sale posted",
    "distress_nod": "Notice of default recorded",
    "distress_prior_foreclosure": "Prior foreclosure events",
    "distress_bankruptcy_active": "Active bankruptcy",
    "distress_bankruptcy_prior": "Prior bankruptcy filings",
    "distress_repeat_filings": "Repeat bankruptcy filings",
    "distress_tax_lien_attached": "Tax lien recorded against the property",
    "distress_tax_lien_owner_only": "Tax lien against the owner only",
    "distress_other_involuntary_liens": "Other involuntary liens (including HOA)",
    "distress_taxes_delinquent_2yr": "Property taxes two or more years delinquent",
    "distress_absentee": "Absentee owner (mailing address differs)",
    "distress_owned_over_15yr": "Long ownership tenure (over the configured years)",
    "distress_listing_expired": "Expired or cancelled listing attempts",
    "distress_high_equity_bonus": "High-equity distress bonus",
    "dcs_field_coverage": "Critical field coverage",
    "dcs_corroboration": "Fields corroborated by 2+ sources",
    "dcs_recency": "Newest report recency",
    "dcs_conflict_free": "Freedom from material conflicts",
    "dcs_verification": "Human-verified fields",
    "dcs_extraction_quality": "Mean extraction confidence",
    "risk_liens": "Open lien count",
    "risk_bankruptcy": "Active bankruptcy",
    "risk_foreclosure_stage": "Foreclosure at notice/sale stage",
    "risk_owner_only_liens_over_10k": "Owner-named liens over $10k",
    "risk_title_flags": "Title-related flags",
    "risk_owner_occupied": "Owner occupied",
    "risk_hoa_arrears": "HOA arrears present",
    "risk_material_conflicts": "Material data conflicts",
    "risk_low_dcs": "Low data confidence",
    "risk_federal_tax_lien": "Federal tax lien",
}


def _plain_name(term: str) -> str:
    return _PLAIN_NAMES.get(term, term.replace("_", " ").capitalize())


def _term_description(name: str, base_points, weight) -> str:
    parts = []
    if base_points is not None:
        parts.append(f"Configured base points: {show(base_points)}"
                     + (" (decayed by event recency)" if name.startswith("distress_") else "") + ".")
    if weight is not None:
        parts.append(f"Active configuration weight: {show(weight)}.")
    return " ".join(parts) or "Emitted sub-term of the score."


def _missing_critical_fields(ctx: ExplainContext) -> list[str]:
    record = ctx.normalized
    if record is None:
        return []
    missing: list[str] = []

    def check(label, tracked) -> None:
        if tracked is None or getattr(tracked, "value", None) is None:
            missing.append(label)

    check("square footage", record.attributes.sqft)
    check("bedrooms", record.attributes.beds)
    check("bathrooms", record.attributes.baths)
    check("year built", record.attributes.year_built)
    check("annual taxes", record.taxes.annual_taxes)
    check("rent estimate", record.rental.rent_estimate)
    if not record.valuation_candidates:
        missing.append("any valuation candidate")
    if not record.mortgages:
        missing.append("mortgage records")
    if record.condition is None:
        missing.append("property condition")
    return missing


def _step(order: int, label: str, formula: str, substitution: str, result):
    from contracts import ExplanationStep

    return ExplanationStep(order=order, label=label, formula=formula,
                           substitution=substitution, result=result,
                           display_result=show(result))


def _input(name: str, value, note: str | None = None):
    from contracts import ExplanationInput

    return ExplanationInput(name=name, value=value, display_value=show(value), note=note)
