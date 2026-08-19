"""WP-6 deterministic financial engine (spec §7). Pure library: no DB, no IO, no floats.

Reconciled against GOLDEN FORMULA SET v1 (fixtures/generate_goldens.py docstring);
deviations the golden set documents — all traced to spec ambiguity — are noted inline:
- No financing costs at underwrite time: there is no purchase price yet; financing
  points/flat and loan interest are charged inside the flip strategy (spec §8),
  so `CostBlock.financing` is 0 here and holding excludes loan interest (spec §7.5
  lists loan interest under holding, but no loan exists at underwrite time).
- Resale cost % is the standard assumption-set rate in all scenarios and staging is
  flip-only (spec §7.1 varies resale % by scenario but defines no magnitudes).
- Market time is always `market_days_default`: the contract carries no local-market
  DOM field (a property's own listing history is not the local DOM of spec §7.5).
- `confidence` is extraction confidence (single-candidate cap 0.5 per §7.2); the
  §7.7 "confidence heavily penalized" for missing debt records is realized through
  `debt_data_present=false` and the scoring DCS coverage term, not a multiplier here.
- The §6.5 ±150bps band belongs to the derived-balance TrackedValue rendered by
  normalization; the §7.1 scenario vectors do not vary confirmed debt.

Pct-based cost models use the scenario value as the notional purchase basis —
`underwrite` has no offer price; strategies recompute against the actual offer.

Quantization convention (golden): money 2dp ROUND_HALF_UP at every labelled step,
ratios/weights 6dp, holding months 4dp; downstream steps consume quantized values.
All arithmetic runs at decimal precision 40, matching the golden generator.
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext

from common.money import money
from common.mortgage import is_first, position_key
from contracts import (
    AssumptionSet,
    AttachmentBasis,
    CostBlock,
    EquityBlock,
    FlagRequest,
    FlagType,
    LiabilityBlock,
    NormalizedProperty,
    Scenario,
    UnderwritingResult,
    ValueBlock,
)

from .rates import historical_rate
from .transfer_tax import transfer_tax_rate

ENGINE_VERSION = "finance-3"
ZERO = Decimal(0)
ONE = Decimal(1)

Q4 = Decimal("0.0001")
Q6 = Decimal("0.000001")

DEFAULT_TERM_MONTHS = 360
AVM_HALF_LIFE_DAYS = Decimal(90)    # vendor AVM recency decay half-life (spec §7.2)
DAYS_PER_MONTH = Decimal(30)
MONTHS_PER_YEAR = Decimal(12)

# Spec §7.2 candidate weight table; assumption-set valuation_weights override it.
SPEC_TYPE_WEIGHT = {
    "avm": Decimal("0.30"),
    "comp": Decimal("0.35"),
    "comp_listing": Decimal("0.15"),
    "list_price": Decimal("0.10"),
    "assessed": Decimal("0.10"),
}

HOLDING_PERIOD_FACTOR = {Scenario.CONSERVATIVE: Decimal("1.5"), Scenario.EXPECTED: ONE, Scenario.OPTIMISTIC: Decimal("0.75")}
BID_MISMATCH_TOLERANCE = Decimal("0.20")  # spec §7.3 published-bid reconciliation
CLOSED_STATUSES = frozenset({"closed", "paid", "released", "satisfied"})


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def _q(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def _validate_assumptions(assumptions: AssumptionSet) -> None:
    acquisition = assumptions.acquisition
    acquisition_rates = (
        acquisition.closing_pct, acquisition.title_pct, acquisition.financing_points,
        acquisition.acq_fee_pct,
    )
    resale = assumptions.resale
    resale_rates = (
        resale.commission_pct, resale.seller_closing_pct,
        resale.concessions_pct, resale.misc_pct,
    )
    holding = assumptions.holding
    if any(value < ZERO for value in acquisition_rates + resale_rates):
        raise ValueError("acquisition and resale percentages must be nonnegative")
    if sum(acquisition_rates, ZERO) >= ONE or sum(resale_rates, ZERO) >= ONE:
        raise ValueError("acquisition and resale percentage totals must be below 100%")
    if any(value < ZERO for value in (
        acquisition.escrow_flat, acquisition.financing_flat,
        acquisition.inspection_flat, acquisition.legal_flat, resale.staging_flat,
        holding.insurance_pct_yr, holding.utilities_monthly,
        holding.maintenance_pct_yr, holding.acquisition_months,
    )) or holding.market_days_default < 0:
        raise ValueError("cost and duration assumptions must be nonnegative")
    if any(value < ZERO for value in holding.repair_months_by_condition.values()):
        raise ValueError("repair durations must be nonnegative")
    repairs = assumptions.repairs
    if (repairs.regional_index <= ZERO
            or any(value < ZERO for value in repairs.psf_by_condition.values())
            or not ZERO <= repairs.low_multiplier <= ONE <= repairs.high_multiplier):
        raise ValueError("repair assumptions must preserve low <= expected <= high")
    if any(not ZERO <= value <= ONE for value in assumptions.attachment_probability.values()):
        raise ValueError("attachment probabilities must be between zero and one")
    if any(value < ZERO for value in assumptions.unknown_lien_medians.values()):
        raise ValueError("unknown-lien medians must be nonnegative")
    if any(value < ZERO for value in assumptions.valuation_weights.values()):
        raise ValueError("valuation weights must be nonnegative")


def _months_between(start: date, end: date) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    if end.day < start.day:
        months -= 1
    return months


def estimate_balance(original: Decimal | None, rate: Decimal | None, term_months: int | None,
                     origination_date: date | None, as_of: date | None,
                     loan_type: str = "conventional") -> Decimal | None:
    """Amortization-derived mortgage balance (spec §6.5).

    Missing rate falls back to the static `historical_rate_index` (year × loan
    type); callers should then tag the result derived/`amortization_v1` at
    confidence 0.55 and widen the rendered band by ±150bps. Returns None when
    the balance cannot be derived at all.
    """
    if original is None or original < ZERO or origination_date is None or as_of is None:
        return None
    term = term_months or DEFAULT_TERM_MONTHS
    if term <= 0:
        return None
    if rate is None:
        rate = historical_rate(origination_date.year, loan_type)
        if rate is None:
            return None
    n = _months_between(origination_date, as_of)
    if n < 0:
        return None
    if n == 0:
        return money(original)
    if n >= term:
        return ZERO
    if rate < ZERO:
        return None
    if rate == ZERO:
        return money(original * Decimal(term - n) / Decimal(term))
    with localcontext() as ctx:
        ctx.prec = 40
        r = rate / MONTHS_PER_YEAR
        growth_term = (ONE + r) ** term
        growth_now = (ONE + r) ** n
        return money(original * (growth_term - growth_now) / (growth_term - ONE))


def finance_flags(record: NormalizedProperty, as_of: date | None = None) -> list[FlagRequest]:
    """FlagRequests raised by the financial engine; WP-9 collects and persists these."""
    as_of = as_of or record.data_quality.newest_report_date
    flags = []
    mismatch = _bid_mismatch(record, as_of)
    if mismatch:
        bid, balance = mismatch
        flags.append(FlagRequest(property_id=record.property_id, flag_type=FlagType.BID_MISMATCH,
                                 payload={"published_bid": bid, "estimated_first_balance": balance,
                                          "divergence": abs(bid - balance) / bid},
                                 financial_impact_usd=abs(bid - balance), raised_by="finance",
                                 dedupe_key=f"bid-mismatch:{bid}:{balance}"))
    for index, lien in enumerate(record.liens):
        if lien.status.casefold() in CLOSED_STATUSES:
            continue
        if lien.amount is None or lien.amount.value is None:
            flags.append(FlagRequest(property_id=record.property_id, flag_type=FlagType.MISSING_LIEN_AMOUNT,
                                     payload={"index": index, "lien_type": lien.lien_type},
                                     financial_impact_usd=None, raised_by="finance",
                                     dedupe_key=f"missing-lien-amount:{lien.lien_type}:{index}"))
    return flags


def _comp_quality(record: NormalizedProperty, as_of: date | None) -> Decimal:
    """Comp-quality weight adjustment (count, distance, recency, sqft delta; spec §7.2).

    Neutral (1.0) when no comparables exist — there are no metrics to adjust by.
    """
    comps = [c for c in record.comparables if c.included and c.price and c.price.value is not None]
    if not comps:
        return ONE
    count = len(comps)
    factor = ONE if count >= 5 else Decimal("0.85") if count >= 3 else Decimal("0.7")
    distances = [c.distance for c in comps if c.distance is not None]
    if distances:
        mean_distance = sum(distances) / Decimal(len(distances))
        factor *= ONE if mean_distance <= ONE else Decimal("0.9") if mean_distance <= 2 else Decimal("0.8")
    if as_of:
        sale_days = [max(0, (as_of - c.sale_date).days) for c in comps if c.sale_date]
        if sale_days:
            mean_days = sum(sale_days) / len(sale_days)
            factor *= ONE if mean_days <= 180 else Decimal("0.9") if mean_days <= 365 else Decimal("0.8")
    subject_sqft = record.attributes.sqft.value if record.attributes.sqft else None
    if subject_sqft:
        deltas = [abs(c.sqft - subject_sqft) / subject_sqft for c in comps if c.sqft]
        if deltas:
            mean_delta = sum(deltas) / Decimal(len(deltas))
            factor *= ONE if mean_delta <= Decimal("0.1") else Decimal("0.9") if mean_delta <= Decimal("0.2") else Decimal("0.8")
    return _clamp(factor, Decimal("0.3"), ONE)


def _comp_range(record: NormalizedProperty) -> tuple[Decimal | None, Decimal | None]:
    """comp_range_low/high for the §7.2 V_low/V_high clamps (min/max of the evidence)."""
    lows = []
    highs = []
    for comp in record.comparables:
        if comp.included and comp.price and comp.price.value is not None:
            lows.append(comp.price.value)
            highs.append(comp.price.value)
    for candidate in record.valuation_candidates:
        if "comp" not in candidate.valuation_type.lower():
            continue
        if candidate.value_low is not None:
            lows.append(candidate.value_low)
        if candidate.value_high is not None:
            highs.append(candidate.value_high)
    return (min(lows) if lows else None, max(highs) if highs else None)


def _candidate_weights(record: NormalizedProperty, assumptions: AssumptionSet,
                       as_of: date | None) -> list[tuple[str, Decimal, Decimal]]:
    """(type, value, weight) per candidate; weight = base × adjustment, quantized 6dp.

    Base: assumption-set `valuation_weights[type]`, else the spec §7.2 table, else 1.0.
    Adjustment: avm → × reported_confidence × recency decay (90-day half-life vs the
    reference date); comp types → × comp quality (neutral without comparables).
    """
    weighted = []
    for candidate in record.valuation_candidates:
        if candidate.value.value is None:
            continue
        kind = candidate.valuation_type.strip().casefold()
        if candidate.value.value <= ZERO:
            continue
        base = assumptions.valuation_weights.get(
            kind, SPEC_TYPE_WEIGHT.get(kind, ONE))
        if base <= ZERO:
            continue
        adjustment = ONE
        if kind == "avm":
            reported_confidence = (
                candidate.reported_confidence
                if candidate.reported_confidence is not None
                else candidate.value.confidence
            )
            adjustment *= Decimal(str(reported_confidence))
            if candidate.as_of and as_of:
                days = max(0, (as_of - candidate.as_of).days)
                adjustment *= Decimal("0.5") ** (Decimal(days) / AVM_HALF_LIFE_DAYS)
        elif "comp" in kind:
            adjustment *= _comp_quality(record, as_of)
        weight = _q(base * adjustment, Q6)
        if weight > ZERO:
            weighted.append((kind, candidate.value.value, weight))
    return weighted


def _mortgage_balance(mortgage, as_of: date | None) -> tuple[Decimal | None, bool]:
    """(balance, is_estimated): reported balance wins; otherwise derive by amortization
    (spec §6.5 — a report with original amount but no balance must not contribute $0)."""
    if mortgage.estimated_balance and mortgage.estimated_balance.value is not None:
        if mortgage.estimated_balance.value < ZERO:
            return None, False
        return money(mortgage.estimated_balance.value), mortgage.estimated_balance.is_estimated
    original = mortgage.original_amount.value if mortgage.original_amount else None
    if original is None or mortgage.origination_date is None:
        return None, False
    loan_type = "heloc" if _is_heloc(mortgage) else "conventional"
    # A HELOC's original amount is normally its credit limit, not proof of the
    # drawn balance. Amortizing that limit fabricates confirmed debt.
    if loan_type == "heloc":
        return None, False
    balance = estimate_balance(original, mortgage.rate, mortgage.term_months,
                               mortgage.origination_date, as_of, loan_type)
    return balance, balance is not None


def _is_first(mortgage) -> bool:
    return is_first(mortgage.position)


def _is_heloc(mortgage) -> bool:
    return "heloc" in mortgage.position.casefold()


def _position_key(position: str) -> str:
    return position_key(position)


def _published_bid(record: NormalizedProperty) -> Decimal | None:
    foreclosure = record.foreclosure
    if foreclosure and foreclosure.is_active and foreclosure.published_bid and foreclosure.published_bid.value is not None:
        return money(foreclosure.published_bid.value)
    return None


def _first_mortgage_balance(record: NormalizedProperty, as_of: date | None) -> Decimal | None:
    for mortgage in record.mortgages:
        if mortgage.is_open and _is_first(mortgage):
            balance, _ = _mortgage_balance(mortgage, as_of)
            if balance is not None:
                return balance
    return None


def _bid_mismatch(record: NormalizedProperty, as_of: date | None) -> tuple[Decimal, Decimal] | None:
    """(bid, first_balance) when they diverge by >20% (spec §7.3)."""
    bid = _published_bid(record)
    if bid is None or not bid:
        return None
    balance = _first_mortgage_balance(record, as_of)
    if balance is None:
        return None
    if abs(bid - balance) / bid > BID_MISMATCH_TOLERANCE:
        return bid, balance
    return None


def _liabilities(record: NormalizedProperty, assumptions: AssumptionSet, as_of: date | None):
    """Three-bucket liabilities (spec §7.3). Returns (block, potential_weighted) where
    potential_weighted = Σ potential_i × attachment_probability[basis_i] (spec §7.4)."""
    confirmed = ZERO
    potential = ZERO
    potential_weighted = ZERO
    breakdown: list[dict] = []
    bid = _published_bid(record)
    by_position: dict[str, list] = {}
    for mortgage in record.mortgages:
        if mortgage.is_open:
            by_position.setdefault(_position_key(mortgage.position), []).append(mortgage)
    for position, group in sorted(by_position.items()):
        # Same-position open mortgages conflict: keep the highest balance
        # (conservative tie-break, spec §6.2/§6.3).
        evaluated = [
            (balance, is_estimated, mortgage)
            for mortgage in group
            for balance, is_estimated in [_mortgage_balance(mortgage, as_of)]
            if balance is not None
        ]
        unknown_heloc_limits = [
            mortgage.original_amount.value
            for mortgage in group
            if _is_heloc(mortgage)
            and _mortgage_balance(mortgage, as_of)[0] is None
            and mortgage.original_amount is not None
            and mortgage.original_amount.value is not None
            and mortgage.original_amount.value > ZERO
        ]
        if not evaluated:
            if unknown_heloc_limits:
                capacity = money(max(unknown_heloc_limits)) or ZERO
                potential += capacity
                probability = assumptions.attachment_probability.get("undrawn_heloc_capacity", Decimal("0.50"))
                potential_weighted += money(capacity * _clamp(probability, ZERO, ONE)) or ZERO
                breakdown.append({
                    "label": f"mortgage:{position}:draw_unknown",
                    "amount": capacity,
                    "expected_amount": capacity,
                    "basis": "heloc_capacity_no_draw_data",
                    "is_estimated": True,
                })
            continue
        balance, is_estimated, chosen = max(evaluated, key=lambda item: item[0])
        basis = "recorded" if not is_estimated else "estimated_recorded"
        if chosen.estimated_balance is None or chosen.estimated_balance.value is None:
            basis = "amortization_v1"
        if bid is not None and _is_first(chosen):
            # Published-bid reconciliation: the trustee's bid is their actual accounting;
            # precedence favors the bid. Divergence >20% is raised via finance_flags().
            balance = bid
            if chosen.estimated_balance and chosen.estimated_balance.value is not None:
                is_estimated = chosen.estimated_balance.is_estimated
        if balance is None:
            continue
        if _is_heloc(chosen):
            # Drawn HELOC is confirmed; undrawn capacity is potential (spec §7.3).
            confirmed += balance
            breakdown.append({"label": f"mortgage:{position}", "amount": balance, "basis": basis, "is_estimated": is_estimated})
            original = chosen.original_amount.value if chosen.original_amount else None
            if original is not None and original > balance:
                undrawn = money(original - balance) or ZERO
                potential += undrawn
                probability = assumptions.attachment_probability.get("undrawn_heloc_capacity", Decimal("0.50"))
                potential_weighted += money(undrawn * _clamp(probability, ZERO, ONE)) or ZERO
                breakdown.append({"label": f"mortgage:{position}:undrawn", "amount": undrawn,
                                  "expected_amount": undrawn, "basis": "undrawn_heloc_capacity",
                                  "is_estimated": True})
            continue
        confirmed += balance
        breakdown.append({"label": f"mortgage:{position}", "amount": balance, "basis": basis, "is_estimated": is_estimated})
        # A separately reported HELOC at the same nominal priority may be a
        # distinct obligation. With no draw data, keep its limit out of
        # confirmed debt but expose the largest reported capacity as potential.
        if unknown_heloc_limits:
            capacity = money(max(unknown_heloc_limits)) or ZERO
            potential += capacity
            probability = assumptions.attachment_probability.get("undrawn_heloc_capacity", Decimal("0.50"))
            potential_weighted += money(capacity * _clamp(probability, ZERO, ONE)) or ZERO
            breakdown.append({
                "label": f"mortgage:{position}:draw_unknown",
                "amount": capacity,
                "expected_amount": capacity,
                "basis": "heloc_capacity_no_draw_data",
                "is_estimated": True,
            })
    if bid is not None and not any(m.is_open and _is_first(m) for m in record.mortgages):
        confirmed += bid
        breakdown.append({"label": "foreclosure:published_bid", "amount": bid, "basis": "published_bid", "is_estimated": False})
    for lien in record.liens:
        if lien.status.casefold() in CLOSED_STATUSES:
            continue
        has_amount = lien.amount is not None and lien.amount.value is not None
        # Liens with unknown amounts are valued at the type median and land in
        # POTENTIAL whatever their attachment basis (spec §7.3).
        amount = lien.amount.value if has_amount else assumptions.unknown_lien_medians.get(lien.lien_type, ZERO)
        amount = money(amount) or ZERO
        estimated = lien.amount_is_estimated if has_amount else True
        if amount and not has_amount:
            potential += amount
            probability = _clamp(
                assumptions.attachment_probability.get(lien.attachment_basis, ONE), ZERO, ONE,
            )
            expected_amount = money(amount * probability) or ZERO
            potential_weighted += expected_amount
        elif lien.attachment_basis == AttachmentBasis.RECORDED_AGAINST_PROPERTY:
            confirmed += amount
            expected_amount = None
        else:
            potential += amount
            probability = _clamp(
                assumptions.attachment_probability.get(lien.attachment_basis, ONE), ZERO, ONE,
            )
            expected_amount = money(amount * probability) or ZERO
            potential_weighted += expected_amount
        item = {"label": lien.lien_type, "amount": amount,
                "basis": lien.attachment_basis.value, "is_estimated": estimated}
        if expected_amount is not None:
            item["expected_amount"] = expected_amount
        breakdown.append(item)
    for label, tracked in (("delinquent_taxes", record.taxes.delinquent_amount), ("hoa_arrears", record.hoa.arrears)):
        if tracked and tracked.value is not None:
            confirmed += money(tracked.value) or ZERO
            breakdown.append({"label": label, "amount": money(tracked.value), "basis": "recorded", "is_estimated": False})
    block = LiabilityBlock(confirmed=money(confirmed) or ZERO, potential=money(potential) or ZERO,
                           maximum=money(confirmed + potential) or ZERO, breakdown=breakdown)
    return block, money(potential_weighted) or ZERO


def _repairs_base(record: NormalizedProperty, assumptions: AssumptionSet) -> Decimal | None:
    """repairs_base = sqft × psf[condition] × regional_index (spec §7.5) — the engine
    never accepts a repair dollar figure from an LLM. Missing sqft → None (unavailable,
    never a silent $0); missing condition signal → "moderate" fallback."""
    sqft = record.attributes.sqft.value if record.attributes.sqft and record.attributes.sqft.value is not None else None
    if sqft is None:
        return None
    condition = record.condition.condition if record.condition else "moderate"
    rate = assumptions.repairs.psf_by_condition.get(condition, assumptions.repairs.psf_by_condition.get("moderate"))
    return None if rate is None else money(sqft * rate * assumptions.repairs.regional_index)


def _holding_months_base(record: NormalizedProperty, assumptions: AssumptionSet) -> Decimal:
    """acquisition_months + repair duration by condition + market time (spec §7.5)."""
    condition = record.condition.condition if record.condition else "moderate"
    repair_months = assumptions.holding.repair_months_by_condition.get(
        condition, assumptions.holding.repair_months_by_condition.get("moderate", Decimal(3)))
    return assumptions.holding.acquisition_months + repair_months + Decimal(assumptions.holding.market_days_default) / DAYS_PER_MONTH


def _has_debt_data(record: NormalizedProperty, assumptions: AssumptionSet, as_of: date | None) -> bool:
    if _published_bid(record) is not None:
        return True
    if any(mortgage.is_open and _mortgage_balance(mortgage, as_of)[0] is not None
           for mortgage in record.mortgages):
        return True
    if any(
        mortgage.is_open and _is_heloc(mortgage)
        and mortgage.original_amount is not None
        and mortgage.original_amount.value is not None
        and mortgage.original_amount.value > ZERO
        for mortgage in record.mortgages
    ):
        return True
    return any(
        lien.status.casefold() not in CLOSED_STATUSES
        and (
            lien.amount is not None and lien.amount.value is not None
            or assumptions.unknown_lien_medians.get(lien.lien_type, ZERO) > ZERO
        )
        for lien in record.liens
    )


def underwrite(record: NormalizedProperty, assumptions: AssumptionSet, as_of: date | None = None) -> UnderwritingResult:
    # Precision 40 matches the golden generator; quantization happens per labelled step.
    _validate_assumptions(assumptions)
    with localcontext() as ctx:
        ctx.prec = 40
        return _underwrite(record, assumptions, as_of)


def _underwrite(record: NormalizedProperty, assumptions: AssumptionSet, as_of: date | None) -> UnderwritingResult:
    # Reference date for every date-relative term: caller-supplied as_of, then
    # newest_report_date. Wall-clock dates are never used, keeping runs deterministic.
    ref = as_of or record.data_quality.newest_report_date
    debt_data_present = _has_debt_data(record, assumptions, ref)
    liabilities, potential_weighted = _liabilities(record, assumptions, ref)
    candidates = _candidate_weights(record, assumptions, ref)
    if not candidates:
        return UnderwritingResult(property_id=record.property_id, assumption_set_id=assumptions.id, engine_version=ENGINE_VERSION,
                                  status="insufficient_data", unavailable_reason="no_valuation_candidates",
                                  liabilities=liabilities, debt_data_present=debt_data_present, confidence=ZERO)
    weighted_sum = sum((value * weight for _, value, weight in candidates), ZERO)
    weight_sum = sum((weight for _, _, weight in candidates), ZERO)
    if not weight_sum:
        return UnderwritingResult(property_id=record.property_id, assumption_set_id=assumptions.id, engine_version=ENGINE_VERSION,
                                  status="insufficient_data", unavailable_reason="no_valuation_candidates",
                                  liabilities=liabilities, debt_data_present=debt_data_present, confidence=ZERO)
    v_exp = money(weighted_sum / weight_sum)
    if len(candidates) == 1:
        dispersion = Decimal("0.15")  # one estimate is not agreement (spec §7.2)
    else:
        variance = sum((weight * (value - v_exp) ** 2 for _, value, weight in candidates), ZERO) / weight_sum
        dispersion = _q(_clamp(variance.sqrt() / v_exp if v_exp else Decimal("0.30"), Decimal("0.04"), Decimal("0.30")), Q6)
    comp_low, comp_high = _comp_range(record)
    # V_low = max(V×(1−disp), comp_range_low or 0); V_high = min(V×(1+disp), comp_range_high or ∞) (spec §7.2)
    raw_low = v_exp * (ONE - dispersion)
    # A comp range wholly above/below the weighted expectation is conflicting
    # evidence, not permission to invert conservative/expected/optimistic order.
    v_low = money(max(raw_low, comp_low) if comp_low is not None and comp_low <= v_exp else raw_low)
    v_high_raw = v_exp * (ONE + dispersion)
    v_high = money(
        min(v_high_raw, comp_high)
        if comp_high is not None and comp_high >= v_exp
        else v_high_raw
    )
    confidence = _clamp(record.data_quality.mean_extraction_confidence, ZERO, ONE)
    if len(candidates) == 1:
        confidence = min(confidence, Decimal("0.5"))
    values = {Scenario.CONSERVATIVE: v_low, Scenario.EXPECTED: v_exp, Scenario.OPTIMISTIC: v_high}

    repair_base = _repairs_base(record, assumptions)
    repairs = {}
    if repair_base is not None:
        repairs = {Scenario.CONSERVATIVE: money(repair_base * assumptions.repairs.high_multiplier),
                   Scenario.EXPECTED: repair_base,
                   Scenario.OPTIMISTIC: money(repair_base * assumptions.repairs.low_multiplier)}

    taxes_annual = record.taxes.annual_taxes.value if record.taxes.annual_taxes and record.taxes.annual_taxes.value is not None else ZERO
    hoa_dues = record.hoa.monthly_dues.value if record.hoa.monthly_dues and record.hoa.monthly_dues.value is not None else ZERO
    months_base = _holding_months_base(record, assumptions)
    resale_pct = (assumptions.resale.commission_pct + assumptions.resale.seller_closing_pct
                  + assumptions.resale.concessions_pct + assumptions.resale.misc_pct)
    acq_pct = (assumptions.acquisition.closing_pct + assumptions.acquisition.title_pct
               + assumptions.acquisition.acq_fee_pct + transfer_tax_rate(assumptions.acquisition.transfer_tax_lookup_key))
    acq_flat = assumptions.acquisition.escrow_flat + assumptions.acquisition.inspection_flat + assumptions.acquisition.legal_flat

    equity = {}
    costs = {}
    arv_by_scenario: dict[Scenario, Decimal | None] = {}
    for scenario, value in values.items():
        months = _q(months_base * HOLDING_PERIOD_FACTOR[scenario], Q4)
        monthly = money(taxes_annual / MONTHS_PER_YEAR + value * (assumptions.holding.insurance_pct_yr + assumptions.holding.maintenance_pct_yr) / MONTHS_PER_YEAR
                        + assumptions.holding.utilities_monthly + hoa_dues)
        holding = money(monthly * months)
        repair_cost = repairs.get(scenario, ZERO)
        acquisition = money(value * acq_pct + acq_flat)
        resale = money(value * resale_pct)
        costs[scenario] = CostBlock(acquisition=acquisition or ZERO, repairs=repair_cost or ZERO,
                                    holding=holding or ZERO, resale=resale or ZERO, financing=ZERO)
        potential_s = {Scenario.CONSERVATIVE: liabilities.potential,
                       Scenario.EXPECTED: potential_weighted,
                       Scenario.OPTIMISTIC: ZERO}[scenario]
        gross = money(value - liabilities.confirmed)
        equity[scenario] = EquityBlock(gross=gross,
                                       adjusted=money(value - liabilities.confirmed - potential_s),
                                       net_realizable=money(value * (ONE - resale_pct) - liabilities.confirmed - potential_s - holding),
                                       equity_pct=_q(gross / value, Q6) if value else None)
        # ARV is the after-repair value and exists only when repairs are computable
        # (spec §7.2; recapture multiplier 1.0, deliberately unaggressive).
        arv_by_scenario[scenario] = money(value + repairs[scenario]) if repairs else None
    return UnderwritingResult(property_id=record.property_id, assumption_set_id=assumptions.id, engine_version=ENGINE_VERSION,
                              status="ok", value=ValueBlock(v_low=v_low, v_expected=v_exp, v_high=v_high, dispersion=dispersion,
                              arv_by_scenario=arv_by_scenario,
                              candidates_used=[{"type": kind, "value": money(value), "weight": weight} for kind, value, weight in candidates],
                              valuation_confidence=confidence),
                              liabilities=liabilities, equity=equity, costs=costs, holding_months_base=months_base,
                              debt_data_present=debt_data_present, confidence=confidence)
