"""Deterministic scoring per spec section 10. Pure library: no DB, no IO, no LLM numbers.

All weights, bounds, and point values come from a scoring config (a
``scoring_configs`` row in production); ``DEFAULT_CONFIG`` is the in-code
fallback used when no config is supplied (e.g. offline tests). Every sub-term
of every component score is emitted into ``ScoreSet.components`` under a
stable name — WP-14's "why is A above B" reads those names as a contract.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from contracts import (
    AttachmentBasis,
    NormalizedProperty,
    Scenario,
    ScoreSet,
    StrategyResult,
    StrategyType,
    UnderwritingResult,
)

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)
DAYS_PER_MONTH = Decimal("30.4375")

TAX_LIEN_TYPES = frozenset({"tax", "property_tax", "state_tax", "federal_tax"})
FEDERAL_TAX_LIEN_TYPE = "federal_tax"
ACTIVE_BANKRUPTCY_STATUSES = frozenset({"active"})
PRIOR_BANKRUPTCY_STATUSES = frozenset({"dismissed", "discharged", "closed"})
FAILED_LISTING_STATUSES = frozenset({"expired", "cancelled"})
NEAR_SALE_STAGES = frozenset({"nts", "auction"})
CLOSED_LIEN_STATUSES = frozenset({"closed", "paid", "released", "satisfied"})

# Deterministic tie-break order for recommended-strategy selection.
STRATEGY_PRIORITY = (
    StrategyType.CASH,
    StrategyType.FLIP,
    StrategyType.WHOLESALE,
    StrategyType.RENTAL,
    StrategyType.SUBJECT_TO,
    StrategyType.FORECLOSURE,
)

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 0,
    "weights": {
        "overall": {"fos": Decimal("0.50"), "distress": Decimal("0.20"), "dcs": Decimal("0.20"), "risk": Decimal("0.25")},
        "fos": {
            "profit": Decimal("0.30"),
            "roi": Decimal("0.25"),
            "equity_pct": Decimal("0.20"),
            "discount_to_value": Decimal("0.15"),
            "margin_of_safety": Decimal("0.10"),
        },
        "dcs": {
            "coverage": Decimal("0.30"),
            "corroboration": Decimal("0.20"),
            "recency": Decimal("0.20"),
            "conflict": Decimal("0.15"),
            "verification": Decimal("0.10"),
            "extraction": Decimal("0.05"),
        },
    },
    "bounds": {
        "profit": (ZERO, Decimal(150000)),
        "roi": (ZERO, Decimal("0.50")),
        "equity_pct": (ZERO, Decimal("0.60")),
        "discount_to_value": (ZERO, Decimal("0.35")),
        "margin_of_safety": (ZERO, Decimal("0.35")),
        "critical_field_count": Decimal(22),
        "conflict_penalty_divisor": Decimal(5),
        "dcs_recency_half_life_days": Decimal(180),
        "distress_decay_half_life_months": Decimal(18),
        "nts_near_days": Decimal(30),
        "owner_only_lien_threshold": Decimal(10000),
        "high_equity_threshold": Decimal("0.50"),
        "years_owned_threshold": Decimal(15),
        "delinquent_years_threshold": Decimal(2),
        "near_tie_points": Decimal(5),
    },
    "distress_points": {
        "nts_near": Decimal(30),
        "nts_far": Decimal(24),
        "nod": Decimal(18),
        "prior_foreclosure_each": Decimal(8),
        "prior_foreclosure_cap": Decimal(16),
        "bankruptcy_active": Decimal(12),
        "bankruptcy_prior_each": Decimal(6),
        "bankruptcy_prior_cap": Decimal(18),
        "repeat_filings": Decimal(8),
        "tax_lien_property": Decimal(10),
        "tax_lien_owner": Decimal(4),
        "other_lien_each": Decimal(3),
        "other_lien_cap": Decimal(12),
        "taxes_delinquent": Decimal(10),
        "absentee": Decimal(5),
        "long_ownership": Decimal(4),
        "listing_failure_each": Decimal(6),
        "listing_failure_cap": Decimal(12),
        "high_equity_bonus": Decimal(5),
    },
    # Stored inside the ``distress_points`` jsonb column under a "risk" key in
    # the DB (the schema has no dedicated column); see ranking.load_active_scoring_config.
    "risk_points": {
        "lien_count": Decimal(6),
        "active_bankruptcy": Decimal(15),
        "foreclosure_stage": Decimal(12),
        "owner_only_lien": Decimal(10),
        "title_flag": Decimal(10),
        "owner_occupied": Decimal(8),
        "hoa_arrears": Decimal(8),
        "material_conflict": Decimal(10),
        "low_confidence": Decimal(12),
        "federal_tax_lien": Decimal(6),
    },
    "gates": {
        "dcs_cap_threshold": Decimal(40),
        "dcs_cap": Decimal(45),
        "dcs_low_threshold": Decimal(50),
        "foreclosure_dcs_threshold": Decimal(75),
        "foreclosure_cap": Decimal(70),
        "wholesale_min": Decimal(60),
    },
}


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def resolve_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Deep-merge a scoring config (or partial dict) over DEFAULT_CONFIG."""
    merged: dict[str, Any] = {"version": DEFAULT_CONFIG["version"]}
    for section, values in DEFAULT_CONFIG.items():
        if isinstance(values, Mapping):
            merged[section] = {key: (dict(value) if isinstance(value, Mapping) else value) for key, value in values.items()}
    if not config:
        return merged
    for section, values in config.items():
        if section == "version" or not isinstance(values, Mapping) or not isinstance(merged.get(section), Mapping):
            merged[section] = values
            continue
        for key, value in values.items():
            if isinstance(value, Mapping) and isinstance(merged[section].get(key), Mapping):
                merged[section][key] = {**merged[section][key], **value}
            else:
                merged[section][key] = value
    return merged


def _weights(config: Mapping[str, Any], section: str) -> dict[str, Decimal]:
    values = {key: _d(value) for key, value in config["weights"][section].items()}
    if any(value < ZERO for value in values.values()):
        raise ValueError(f"{section} weights must be nonnegative")
    return values


def _points(config: Mapping[str, Any], section: str) -> dict[str, Decimal]:
    values = {key: _d(value) for key, value in config[section].items()}
    if any(value < ZERO for value in values.values()):
        raise ValueError(f"{section} values must be nonnegative")
    return values


def _bound(config: Mapping[str, Any], key: str) -> Any:
    value = config["bounds"][key]
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            raise ValueError(f"{key} bound must contain exactly two values")
        return _d(value[0]), _d(value[1])
    return _d(value)


def n(value: Decimal | None, low: Decimal, high: Decimal) -> Decimal:
    if value is None:
        return ZERO
    if high <= low:
        raise ValueError("normalization upper bound must be greater than lower bound")
    return max(ZERO, min(ONE, (value - low) / (high - low)))


def _q(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def _clamp100(value: Decimal) -> Decimal:
    return max(ZERO, min(HUNDRED, value))


def _months_between(as_of: date, when: date | None) -> Decimal:
    if when is None:
        return ZERO
    return Decimal(max(0, (as_of - when).days)) / DAYS_PER_MONTH


def _decay(months: Decimal, half_life_months: Decimal) -> Decimal:
    """Recency decay 0.5^(months/half_life); dateless events are treated as fresh."""
    if months <= 0 or half_life_months <= 0:
        return ONE
    return Decimal("0.5") ** (months / half_life_months)


def _is_open_lien(lien: Any) -> bool:
    return lien.status.casefold() not in CLOSED_LIEN_STATUSES


def _recommend(config: Mapping[str, Any], strategies: list[StrategyResult]) -> tuple[StrategyResult | None, list[StrategyType]]:
    """Highest-scoring viable strategy; near-ties within the configured point
    band are alternatives. Ties break by STRATEGY_PRIORITY."""
    viable = [
        item for item in strategies
        if item.scenario == Scenario.EXPECTED and item.status == "viable"
        and item.strategy != StrategyType.SUBJECT_TO
    ]
    if not viable:
        return None, []
    low, high = _bound(config, "profit")
    near_tie = _bound(config, "near_tie_points")

    def points(item: StrategyResult) -> Decimal:
        return HUNDRED * n(item.profit, low, high)

    best = max(viable, key=lambda item: (points(item), -STRATEGY_PRIORITY.index(item.strategy)))
    best_points = points(best)
    alternatives = []
    for item in sorted(viable, key=lambda item: STRATEGY_PRIORITY.index(item.strategy)):
        if (item is not best and item.strategy != best.strategy and item.strategy not in alternatives
                and best_points - points(item) <= near_tie):
            alternatives.append(item.strategy)
    return best, alternatives


def _fos(config: Mapping[str, Any], profit: Decimal | None, roi: Decimal | None, equity_pct: Decimal | None,
         discount: Decimal, margin: Decimal | None) -> tuple[Decimal, dict[str, Decimal]]:
    weights = _weights(config, "fos")
    terms = {}
    inputs = (("profit", profit), ("roi", roi), ("equity_pct", equity_pct),
              ("discount_to_value", discount), ("margin_of_safety", margin))
    for key, value in inputs:
        low, high = _bound(config, key)
        terms[f"fos_{key}_norm"] = _q(n(value, low, high), Decimal("0.000001"))
    score = HUNDRED * sum(
        (weights[key] * terms[f"fos_{key}_norm"] for key, _ in inputs), ZERO,
    )
    return _clamp100(score), terms


def _distress(record: NormalizedProperty, config: Mapping[str, Any], as_of: date,
              equity_pct: Decimal | None) -> tuple[Decimal, dict[str, Decimal]]:
    """Additive points with recency decay 0.5^(months/18) on dated events,
    per-category caps, and an overall cap of 100. Distress indicates financial
    pressure, not willingness to sell."""
    points_cfg = _points(config, "distress_points")
    half_life = _bound(config, "distress_decay_half_life_months")
    if half_life <= ZERO:
        raise ValueError("distress decay half-life must be greater than zero")
    terms: dict[str, Decimal] = {}

    def event_decay(event_date: date | None) -> Decimal:
        # A current status with no event date remains real; missing dates are a
        # DCS problem, not a reason to erase the distress signal entirely.
        if event_date is None:
            return ONE
        return _decay(_months_between(as_of, event_date), half_life)

    foreclosure = record.foreclosure
    if foreclosure and foreclosure.is_active:
        stage = foreclosure.stage.casefold()
        if foreclosure.nts_date or stage in NEAR_SALE_STAGES:
            days_to_sale = (
                (foreclosure.current_sale_date - as_of).days
                if foreclosure.current_sale_date is not None else None
            )
            near_days = int(_bound(config, "nts_near_days"))
            sale_is_near = days_to_sale is not None and 0 <= days_to_sale <= near_days
            base = points_cfg["nts_near"] if sale_is_near else points_cfg["nts_far"]
            terms["distress_nts"] = _q(base * event_decay(foreclosure.nts_date), Decimal("0.0001"))
        if foreclosure.nod_date:
            terms["distress_nod"] = _q(points_cfg["nod"] * event_decay(foreclosure.nod_date), Decimal("0.0001"))
        prior = min(points_cfg["prior_foreclosure_cap"],
                    points_cfg["prior_foreclosure_each"] * foreclosure.rescission_count)
        if prior:
            terms["distress_prior_foreclosure"] = _q(prior, Decimal("0.0001"))

    active_bk = [item for item in record.bankruptcies if item.status.casefold() in ACTIVE_BANKRUPTCY_STATUSES]
    prior_bk = [item for item in record.bankruptcies if item.status.casefold() in PRIOR_BANKRUPTCY_STATUSES]
    if active_bk:
        terms["distress_bankruptcy_active"] = _q(
            points_cfg["bankruptcy_active"]
            * max(event_decay(item.filing_date) for item in active_bk),
            Decimal("0.0001"),
        )
    if prior_bk:
        terms["distress_bankruptcy_prior"] = _q(min(
            points_cfg["bankruptcy_prior_cap"],
            sum((points_cfg["bankruptcy_prior_each"] * event_decay(item.filing_date) for item in prior_bk), ZERO),
        ), Decimal("0.0001"))
    is_repeat = any(item.sequence is not None and item.sequence > 1 for item in record.bankruptcies)
    if is_repeat:
        terms["distress_repeat_filings"] = points_cfg["repeat_filings"]

    tax_property = tax_owner = other = ZERO
    for lien in record.liens:
        if not _is_open_lien(lien):
            continue
        decay = event_decay(lien.recording_date)
        lien_type = lien.lien_type.casefold()
        if lien_type in TAX_LIEN_TYPES:
            if lien.attachment_basis == AttachmentBasis.RECORDED_AGAINST_PROPERTY:
                tax_property += points_cfg["tax_lien_property"] * decay
            else:
                tax_owner += points_cfg["tax_lien_owner"] * decay
        else:
            other += points_cfg["other_lien_each"] * decay
    if tax_property:
        terms["distress_tax_lien_attached"] = _q(tax_property, Decimal("0.0001"))
    if tax_owner:
        terms["distress_tax_lien_owner_only"] = _q(tax_owner, Decimal("0.0001"))
    if record.hoa.has_lien and record.hoa.arrears and (record.hoa.arrears.value or ZERO) > ZERO:
        other += points_cfg["other_lien_each"]
    if other:
        terms["distress_other_involuntary_liens"] = _q(min(points_cfg["other_lien_cap"], other), Decimal("0.0001"))

    delinquent_years = record.taxes.delinquent_years or 0
    if delinquent_years >= int(_bound(config, "delinquent_years_threshold")):
        terms["distress_taxes_delinquent_2yr"] = points_cfg["taxes_delinquent"]
    if record.ownership.is_absentee:
        terms["distress_absentee"] = points_cfg["absentee"]
    years_owned = record.ownership.years_owned
    if years_owned is not None and years_owned > _bound(config, "years_owned_threshold"):
        terms["distress_owned_over_15yr"] = points_cfg["long_ownership"]

    listing_failures = sum(
        (
            points_cfg["listing_failure_each"] * _decay(_months_between(as_of, listing.delist_date or listing.list_date), half_life)
            for listing in record.listings
            if listing.status.casefold() in FAILED_LISTING_STATUSES
        ),
        ZERO,
    )
    if listing_failures:
        terms["distress_listing_expired"] = _q(min(points_cfg["listing_failure_cap"], listing_failures), Decimal("0.0001"))

    high_equity = equity_pct is not None and equity_pct >= _bound(config, "high_equity_threshold")
    if high_equity and sum(terms.values(), ZERO) > ZERO:
        terms["distress_high_equity_bonus"] = points_cfg["high_equity_bonus"]

    return _clamp100(sum(terms.values(), ZERO)), terms


def _dcs(record: NormalizedProperty, config: Mapping[str, Any], as_of: date) -> tuple[Decimal, dict[str, Decimal]]:
    weights = _weights(config, "dcs")
    quality = record.data_quality
    critical = _bound(config, "critical_field_count")
    divisor = _bound(config, "conflict_penalty_divisor")
    half_life_days = _bound(config, "dcs_recency_half_life_days")
    if critical <= ZERO or divisor <= ZERO or half_life_days <= ZERO:
        raise ValueError("DCS count, conflict, and recency bounds must be greater than zero")

    coverage = max(ZERO, min(ONE, quality.critical_field_coverage))
    corroborated = sum(1 for count in quality.source_counts_by_field.values() if count >= 2)
    corroboration = max(ZERO, min(ONE, Decimal(corroborated) / critical))
    if quality.newest_report_date is None:
        recency = ZERO
    else:
        age_days = Decimal(max(0, (as_of - quality.newest_report_date).days))
        recency = _decay(age_days, half_life_days)
    conflict_penalty = max(
        ZERO, min(ONE, Decimal(quality.material_conflict_count) / divisor),
    )
    verification = max(ZERO, min(ONE, Decimal(quality.verified_field_count) / critical))
    extraction = max(ZERO, min(ONE, quality.mean_extraction_confidence))

    terms = {
        "dcs_field_coverage": _q(coverage, Decimal("0.000001")),
        "dcs_corroboration": _q(corroboration, Decimal("0.000001")),
        "dcs_recency": _q(recency, Decimal("0.000001")),
        "dcs_conflict_free": _q(ONE - conflict_penalty, Decimal("0.000001")),
        "dcs_verification": _q(verification, Decimal("0.000001")),
        "dcs_extraction_quality": _q(extraction, Decimal("0.000001")),
    }
    weighted = (HUNDRED * (weights["coverage"] * coverage
                           + weights["corroboration"] * terms["dcs_corroboration"]
                           + weights["recency"] * recency
                           + weights["conflict"] * (ONE - conflict_penalty)
                           + weights["verification"] * terms["dcs_verification"]
                           + weights["extraction"] * extraction))
    return _q(weighted, Decimal("0.0001")), terms


def data_confidence(record: NormalizedProperty, config: Mapping[str, Any] | None = None,
                    as_of: date | None = None) -> Decimal:
    """Return the resolved 0–100 DCS used by scoring and strategy gates."""
    value, _ = _dcs(record, resolve_config(config), as_of or datetime.now(UTC).date())
    return value


def _risk(record: NormalizedProperty, config: Mapping[str, Any], dcs: Decimal) -> tuple[Decimal, dict[str, Decimal]]:
    points_cfg = _points(config, "risk_points")
    gates = _points(config, "gates")
    open_liens = [lien for lien in record.liens if _is_open_lien(lien)]
    foreclosure = record.foreclosure
    terms: dict[str, Decimal] = {}

    terms["risk_liens"] = points_cfg["lien_count"] * Decimal(len(open_liens))
    terms["risk_bankruptcy"] = (
        points_cfg["active_bankruptcy"] if any(item.status.casefold() in ACTIVE_BANKRUPTCY_STATUSES for item in record.bankruptcies) else ZERO
    )
    terms["risk_foreclosure_stage"] = (
        points_cfg["foreclosure_stage"] if foreclosure and foreclosure.is_active and foreclosure.stage.casefold() in NEAR_SALE_STAGES else ZERO
    )
    threshold = _bound(config, "owner_only_lien_threshold")
    over_threshold = sum(
        1
        for lien in open_liens
        if lien.attachment_basis == AttachmentBasis.OWNER_NAMED_ONLY
        and lien.amount is not None
        and lien.amount.value is not None
        and lien.amount.value > threshold
    )
    terms["risk_owner_only_liens_over_10k"] = points_cfg["owner_only_lien"] * Decimal(over_threshold)
    title_flags = sum(1 for flag in record.open_flags if flag.type.value in {
        "identity_conflict", "conflicting_mortgage", "foreclosure_unclear"})
    terms["risk_title_flags"] = points_cfg["title_flag"] * Decimal(title_flags)
    terms["risk_owner_occupied"] = points_cfg["owner_occupied"] if record.ownership.is_owner_occupied else ZERO
    arrears = record.hoa.arrears
    terms["risk_hoa_arrears"] = (
        points_cfg["hoa_arrears"] if arrears is not None and arrears.value is not None and arrears.value > ZERO else ZERO
    )
    terms["risk_material_conflicts"] = points_cfg["material_conflict"] * Decimal(record.data_quality.material_conflict_count)
    terms["risk_low_dcs"] = points_cfg["low_confidence"] if dcs < gates["dcs_low_threshold"] else ZERO
    terms["risk_federal_tax_lien"] = (
        points_cfg["federal_tax_lien"] if any(lien.lien_type.casefold() == FEDERAL_TAX_LIEN_TYPE for lien in open_liens) else ZERO
    )

    return _q(_clamp100(sum(terms.values(), ZERO)), Decimal("0.0001")), terms


def score(record: NormalizedProperty, underwriting: UnderwritingResult, scoring_config_id: UUID,
          strategies: list[StrategyResult] | None = None, config: Mapping[str, Any] | None = None,
          as_of: date | None = None) -> ScoreSet:
    """Score a property per spec section 10. ``config`` is a scoring_configs
    row (or partial override dict); when omitted the in-code DEFAULT_CONFIG is
    used. ``as_of`` anchors all recency math (defaults to today)."""
    resolved = resolve_config(config)
    as_of = as_of or datetime.now(UTC).date()
    strategies = strategies or []

    expected_value = underwriting.value.v_expected
    equity_block = underwriting.equity.get(Scenario.EXPECTED)
    equity_pct = equity_block.equity_pct if equity_block else None

    best, alternatives = _recommend(resolved, strategies)
    profit = best.profit if best else None
    roi = best.roi if best else None
    margin = best.margin_of_safety if best else None
    if expected_value and best and best.mao is not None:
        discount = _q((expected_value - best.mao) / expected_value, Decimal("0.000001"))
    else:
        discount = ZERO

    fos, fos_norm = _fos(resolved, profit, roi, equity_pct, discount, margin)
    fos = _q(fos, Decimal("0.0001"))
    distress, distress_terms = _distress(record, resolved, as_of, equity_pct)
    dcs, dcs_terms = _dcs(record, resolved, as_of)
    risk, risk_terms = _risk(record, resolved, dcs)

    overall_weights = _weights(resolved, "overall")
    gates_cfg = _points(resolved, "gates")
    overall = _clamp100(
        overall_weights["fos"] * fos + overall_weights["distress"] * distress
        + overall_weights["dcs"] * dcs - overall_weights["risk"] * risk
    )

    gates: list[str] = []
    if underwriting.status != "ok":
        gates.append("insufficient_data")
    if dcs < gates_cfg["dcs_cap_threshold"]:
        gates.append("dcs_below_40")
        overall = min(overall, gates_cfg["dcs_cap"])
    # Spec §8.6: active foreclosure opportunities are capped until the data
    # confidence score reaches 75. This is a score cap, not a fabricated
    # foreclosure recommendation or a substitute for missing data.
    if (record.foreclosure is not None and record.foreclosure.is_active
            and dcs < gates_cfg["foreclosure_dcs_threshold"]):
        gates.append("foreclosure_cap")
        overall = min(overall, gates_cfg["foreclosure_cap"])
    if any(flag.is_gating for flag in record.open_flags):
        gates.append("open_gating_flag")

    components = {
        "profit": profit or ZERO,
        "roi": roi or ZERO,
        "equity_pct": equity_pct or ZERO,
        "discount_to_value": discount,
        "margin_of_safety": margin or ZERO,
        "mao_best": best.mao if best and best.mao is not None else ZERO,
        **fos_norm,
        **distress_terms,
        **dcs_terms,
        **risk_terms,
    }
    return ScoreSet(
        property_id=record.property_id,
        scoring_config_id=scoring_config_id,
        fos=_q(fos, Decimal("0.0001")),
        distress=_q(distress, Decimal("0.0001")),
        data_confidence=_q(dcs, Decimal("0.0001")),
        risk=_q(risk, Decimal("0.0001")),
        overall=_q(overall, Decimal("0.0001")),
        components=components,
        gates_applied=gates,
        is_rankable="insufficient_data" not in gates and "open_gating_flag" not in gates,
        recommended_strategy=best.strategy if best else None,
        recommended_alternatives=alternatives,
    )
