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
from typing import overload

from common.money import money
from common.mortgage import is_first, position_key
from common.trace import TraceRecorder, show
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


@overload
def _q(value: Decimal, quantum: Decimal) -> Decimal: ...

@overload
def _q(value: None, quantum: Decimal) -> None: ...


def _q(value: Decimal | None, quantum: Decimal) -> Decimal | None:
    return value.quantize(quantum, rounding=ROUND_HALF_UP) if value is not None else None


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
                       as_of: date | None, trace: TraceRecorder | None = None) -> list[tuple[str, Decimal, Decimal]]:
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
        adjustment_notes: list[str] = []
        if kind == "avm":
            reported_confidence = (
                candidate.reported_confidence
                if candidate.reported_confidence is not None
                else candidate.value.confidence
            )
            adjustment *= Decimal(str(reported_confidence))
            adjustment_notes.append(f"reported confidence {reported_confidence}")
            if candidate.as_of and as_of:
                days = max(0, (as_of - candidate.as_of).days)
                decay = Decimal("0.5") ** (Decimal(days) / AVM_HALF_LIFE_DAYS)
                adjustment *= decay
                adjustment_notes.append(f"recency decay {show(decay)} over {days} days (90-day half-life)")
        elif "comp" in kind:
            quality = _comp_quality(record, as_of)
            adjustment *= quality
            adjustment_notes.append(f"comparable-quality adjustment {show(quality)}")
        weight = _q(base * adjustment, Q6)
        if weight > ZERO:
            weighted.append((kind, candidate.value.value, weight))
            if trace is not None:
                trace.step(label=f"Valuation candidate weight ({kind})",
                           formula="weight = base_weight × adjustments",
                           inputs={"base weight": base, "adjustments": ", ".join(adjustment_notes) or "none"},
                           substitution=f"{show(base)} × {show(adjustment)}",
                           result=weight)
                trace.candidate(label=kind, value=candidate.value.value,
                                confidence=candidate.value.confidence,
                                origin="extracted", is_winner=False,
                                source_fact_id=candidate.value.fact_id)
    return weighted


def _mortgage_balance(mortgage, as_of: date | None, trace: TraceRecorder | None = None) -> tuple[Decimal | None, bool]:
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
    if trace is not None and balance is not None:
        rate_used = mortgage.rate
        rate_note = "reported mortgage rate"
        if rate_used is None:
            rate_used = historical_rate(mortgage.origination_date.year, loan_type)
            rate_note = f"historical fallback rate for {mortgage.origination_date.year} ({loan_type})"
        months_elapsed = _months_between(mortgage.origination_date, as_of) if as_of is not None else 0
        trace.step(
            label=f"Mortgage balance derived by amortization ({_position_key(mortgage.position)})",
            formula="balance = P × ((1+r)^term − (1+r)^n) / ((1+r)^term − 1), r = annual_rate/12, n = months elapsed",
            inputs={"original principal": original, "annual rate": rate_used,
                    "term (months)": mortgage.term_months or DEFAULT_TERM_MONTHS,
                    "origination date": mortgage.origination_date.isoformat(),
                    "as-of date": as_of.isoformat() if as_of else None},
            substitution=f"P={show(original)}, rate={show(rate_used)}, term={mortgage.term_months or DEFAULT_TERM_MONTHS}, n={months_elapsed}",
            result=balance)
        trace.assumption("rate source", rate_note)
        trace.candidate(label=f"amortization estimate ({_position_key(mortgage.position)})",
                        value=balance, confidence=Decimal("0.55"), origin="estimated", is_winner=True,
                        derivation_inputs={"original_amount": original, "origination_date": mortgage.origination_date.isoformat(),
                                           "rate": rate_used, "term_months": mortgage.term_months or DEFAULT_TERM_MONTHS})
        trace.resolution(method="amortization_v1",
                         winner_description="no reported current balance; derived from original loan terms",
                         reason="A report with the original amount but no current balance must still contribute debt.")
        trace.warning("Estimated mortgage balance is an amortization estimate, not a lender payoff statement.")
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


def _liabilities(record: NormalizedProperty, assumptions: AssumptionSet, as_of: date | None,
                 trace: TraceRecorder | None = None):
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
                probability = assumptions.attachment_probability.get(AttachmentBasis.UNDRAWN_HELOC_CAPACITY, Decimal("0.50"))
                potential_weighted += money(capacity * _clamp(probability, ZERO, ONE)) or ZERO
                breakdown.append({
                    "label": f"mortgage:{position}:draw_unknown",
                    "amount": capacity,
                    "expected_amount": capacity,
                    "basis": "heloc_capacity_no_draw_data",
                    "is_estimated": True,
                })
                if trace is not None:
                    trace.step(label=f"Undrawn HELOC capacity treated as potential ({position})",
                               formula="potential += credit limit (no draw data)",
                               inputs={"credit limit": capacity, "attachment probability": probability},
                               substitution=f"limit={show(capacity)}, p={show(probability)}",
                               result=capacity)
                    trace.warning(f"HELOC at position {position} has a reported limit but no draw balance; "
                                  "the full limit is held as a potential obligation.")
            continue
        balance, is_estimated, chosen = max(evaluated, key=lambda item: item[0])
        basis = "recorded" if not is_estimated else "estimated_recorded"
        if chosen.estimated_balance is None or chosen.estimated_balance.value is None:
            basis = "amortization_v1"
        if trace is not None and len(evaluated) > 1:
            for candidate_balance, candidate_estimated, mortgage_record in evaluated:
                trace.candidate(label=f"mortgage {_position_key(mortgage_record.position)}",
                                value=candidate_balance,
                                origin="reported" if not candidate_estimated else "estimated",
                                is_winner=mortgage_record is chosen,
                                reason="conservative tie-break: highest balance wins" if mortgage_record is chosen else None)
        if bid is not None and _is_first(chosen):
            # Published-bid reconciliation: the trustee's bid is their actual accounting;
            # precedence favors the bid. Divergence >20% is raised via finance_flags().
            if trace is not None and bid != balance:
                trace.candidate(label="foreclosure published bid", value=bid, origin="reported", is_winner=True,
                                reason="the trustee's published bid is their actual accounting and wins over the derived balance")
                trace.conflict(description=f"Published foreclosure bid {show(bid)} diverges from the first-mortgage balance "
                                          f"{show(balance)} by more than 20%; the bid was used.", magnitude=abs(bid - balance))
            balance = bid
            if chosen.estimated_balance and chosen.estimated_balance.value is not None:
                is_estimated = chosen.estimated_balance.is_estimated
        if balance is None:
            continue
        if _is_heloc(chosen):
            # Drawn HELOC is confirmed; undrawn capacity is potential (spec §7.3).
            confirmed += balance
            breakdown.append({"label": f"mortgage:{position}", "amount": balance, "basis": basis, "is_estimated": is_estimated})
            if trace is not None:
                trace.step(label=f"Confirmed mortgage balance ({position})",
                           formula="confirmed += reported/derived drawn balance",
                           inputs={"balance": balance, "basis": basis},
                           substitution=f"balance={show(balance)}, basis={basis}, bucket={'estimated' if is_estimated else 'reported'}",
                           result=balance)
            original = chosen.original_amount.value if chosen.original_amount else None
            if original is not None and original > balance:
                undrawn = money(original - balance) or ZERO
                potential += undrawn
                probability = assumptions.attachment_probability.get(AttachmentBasis.UNDRAWN_HELOC_CAPACITY, Decimal("0.50"))
                potential_weighted += money(undrawn * _clamp(probability, ZERO, ONE)) or ZERO
                breakdown.append({"label": f"mortgage:{position}:undrawn", "amount": undrawn,
                                  "expected_amount": undrawn, "basis": "undrawn_heloc_capacity",
                                  "is_estimated": True})
            continue
        confirmed += balance
        breakdown.append({"label": f"mortgage:{position}", "amount": balance, "basis": basis, "is_estimated": is_estimated})
        if trace is not None:
            trace.step(label=f"Confirmed mortgage balance ({position})",
                       formula="confirmed += reported/derived balance",
                       inputs={"balance": balance, "basis": basis},
                       substitution=f"balance={show(balance)}, basis={basis}, bucket={'estimated' if is_estimated else 'reported'}",
                       result=balance)
        # A separately reported HELOC at the same nominal priority may be a
        # distinct obligation. With no draw data, keep its limit out of
        # confirmed debt but expose the largest reported capacity as potential.
        if unknown_heloc_limits:
            capacity = money(max(unknown_heloc_limits)) or ZERO
            potential += capacity
            probability = assumptions.attachment_probability.get(AttachmentBasis.UNDRAWN_HELOC_CAPACITY, Decimal("0.50"))
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
        amount = (lien.amount.value if lien.amount is not None and lien.amount.value is not None
                  else assumptions.unknown_lien_medians.get(lien.lien_type, ZERO))
        amount = money(amount) or ZERO
        estimated = lien.amount_is_estimated if has_amount else True
        if trace is not None:
            trace.input(f"Lien: {lien.lien_type}", amount if has_amount else None,
                        note="reported amount" if has_amount else
                             f"no reliable amount extracted; valued at the {lien.lien_type} median assumption")
        if amount and not has_amount:
            potential += amount
            probability = _clamp(
                assumptions.attachment_probability.get(lien.attachment_basis, ONE), ZERO, ONE,
            )
            expected_amount = money(amount * probability) or ZERO
            potential_weighted += expected_amount
            if trace is not None:
                trace.step(label=f"Potential lien with unknown amount ({lien.lien_type})",
                           formula="median amount × attachment probability",
                           inputs={"median amount": amount, "attachment probability": probability},
                           substitution=f"{show(amount)} × {show(probability)}",
                           result=expected_amount)
                trace.warning(f"The {lien.lien_type} lien has no extracted amount; the type-median "
                              "estimate is held as a potential obligation, so equity may be overstated.")
        elif lien.attachment_basis == AttachmentBasis.RECORDED_AGAINST_PROPERTY:
            confirmed += amount
            expected_amount = None
            if trace is not None:
                trace.step(label=f"Confirmed lien ({lien.lien_type})",
                           formula="confirmed += recorded-against-property amount",
                           inputs={"amount": amount, "status": lien.status},
                           substitution=f"{show(amount)} (basis: recorded_against_property, status: {lien.status})",
                           result=amount)
        else:
            potential += amount
            probability = _clamp(
                assumptions.attachment_probability.get(lien.attachment_basis, ONE), ZERO, ONE,
            )
            expected_amount = money(amount * probability) or ZERO
            potential_weighted += expected_amount
            if trace is not None:
                trace.step(label=f"Potential lien ({lien.lien_type})",
                           formula="amount × attachment probability",
                           inputs={"amount": amount, "attachment probability": probability,
                                   "attachment basis": lien.attachment_basis.value},
                           substitution=f"{show(amount)} × {show(probability)} ({lien.attachment_basis.value})",
                           result=expected_amount)
                trace.resolution(method=f"attachment:{lien.attachment_basis.value}",
                                 winner_description=f"{lien.lien_type} held as a potential obligation",
                                 reason=f"The lien names the owner but is not recorded against the property "
                                        f"(basis: {lien.attachment_basis.value}), so it may never attach.")
        item = {"label": lien.lien_type, "amount": amount,
                "basis": lien.attachment_basis.value, "is_estimated": estimated}
        if expected_amount is not None:
            item["expected_amount"] = expected_amount
        breakdown.append(item)
    for label, tracked in (("delinquent_taxes", record.taxes.delinquent_amount), ("hoa_arrears", record.hoa.arrears)):
        if tracked and tracked.value is not None:
            confirmed += money(tracked.value) or ZERO
            breakdown.append({"label": label, "amount": money(tracked.value), "basis": "recorded", "is_estimated": False})
            if trace is not None:
                trace.step(label=f"Confirmed {label.replace('_', ' ')}",
                           formula="confirmed += reported amount",
                           inputs={"amount": tracked.value},
                           substitution=show(tracked.value), result=money(tracked.value))
    block = LiabilityBlock(confirmed=money(confirmed) or ZERO, potential=money(potential) or ZERO,
                           maximum=money(confirmed + potential) or ZERO, breakdown=breakdown)
    if trace is not None:
        trace.step(label="Confirmed obligations total",
                   formula="Σ confirmed mortgage balances + confirmed liens + delinquent taxes/hoa arrears",
                   inputs={"confirmed": confirmed}, substitution=show(confirmed), result=block.confirmed)
        trace.step(label="Potential obligations total",
                   formula="Σ potential amounts (owner-named liens, undrawn HELOC capacity, unknown-amount medians)",
                   inputs={"potential": potential}, substitution=show(potential), result=block.potential)
        trace.step(label="Maximum exposure",
                   formula="confirmed + potential",
                   inputs={"confirmed": block.confirmed, "potential": block.potential},
                   substitution=f"{show(block.confirmed)} + {show(block.potential)}", result=block.maximum)
        for item in breakdown:
            trace.input(f"Liability line: {item.get('label')}", item.get("amount"),
                        note=f"bucket={'estimated' if item.get('is_estimated') else 'reported'}; basis={item.get('basis')}")
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


def underwrite(record: NormalizedProperty, assumptions: AssumptionSet, as_of: date | None = None,
               trace: TraceRecorder | None = None) -> UnderwritingResult:
    # Precision 40 matches the golden generator; quantization happens per labelled step.
    _validate_assumptions(assumptions)
    with localcontext() as ctx:
        ctx.prec = 40
        return _underwrite(record, assumptions, as_of, trace)


def _underwrite(record: NormalizedProperty, assumptions: AssumptionSet, as_of: date | None,
                trace: TraceRecorder | None = None) -> UnderwritingResult:
    # Reference date for every date-relative term: caller-supplied as_of, then
    # newest_report_date. Wall-clock dates are never used, keeping runs deterministic.
    ref = as_of or record.data_quality.newest_report_date
    if trace is not None:
        trace.assumption("Assumption set", f"{assumptions.name} v{assumptions.version}",
                         assumption_set_id=assumptions.id)
        trace.assumption("Repair $/sqft by condition",
                         {key: show(value) for key, value in assumptions.repairs.psf_by_condition.items()})
        trace.assumption("Regional repair index", show(assumptions.repairs.regional_index))
        trace.assumption("Valuation candidate weights",
                         {key: show(value) for key, value in assumptions.valuation_weights.items()})
        trace.assumption("Holding months by condition",
                         {key: show(value) for key, value in assumptions.holding.repair_months_by_condition.items()})
    debt_data_present = _has_debt_data(record, assumptions, ref)
    liabilities, potential_weighted = _liabilities(record, assumptions, ref, trace)
    candidates = _candidate_weights(record, assumptions, ref, trace)
    if trace is not None:
        trace.step(label="Weighted valuation sum",
                   formula="Σ(value × adjusted weight) / Σ(adjusted weights)",
                   inputs={kind: value for kind, value, _ in candidates},
                   substitution=" + ".join(f"({show(value)} × {show(weight)})" for _, value, weight in candidates),
                   result=None)
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
    if trace is not None:
        trace.steps[-1]["result"] = v_exp
        trace.steps[-1]["display_result"] = show(v_exp)
        trace.step(label="Total candidate weight",
                   formula="Σ(adjusted weights)",
                   inputs={}, substitution=" + ".join(show(weight) for _, _, weight in candidates),
                   result=weight_sum)
        trace.step(label="Expected value",
                   formula="weighted_sum / weight_sum",
                   inputs={"weighted sum": weighted_sum, "weight sum": weight_sum},
                   substitution=f"{show(weighted_sum)} / {show(weight_sum)}", result=v_exp)
    if v_exp is None:
        return UnderwritingResult(property_id=record.property_id, assumption_set_id=assumptions.id, engine_version=ENGINE_VERSION,
                                  status="insufficient_data", unavailable_reason="valuation_not_numeric",
                                  liabilities=liabilities, debt_data_present=debt_data_present, confidence=ZERO)
    if len(candidates) == 1:
        dispersion = Decimal("0.15")  # one estimate is not agreement (spec §7.2)
        if trace is not None:
            trace.assumption("dispersion", "fixed at 15% — a single valuation candidate shows no independent agreement")
    else:
        variance = sum((weight * (value - v_exp) ** 2 for _, value, weight in candidates), ZERO) / weight_sum
        dispersion = _q(_clamp(variance.sqrt() / v_exp if v_exp else Decimal("0.30"), Decimal("0.04"), Decimal("0.30")), Q6)
        if trace is not None:
            trace.step(label="Value dispersion",
                       formula="clamp(√(Σ(weight × (value − V)²) / Σweight) / V, 4%, 30%)",
                       inputs={}, substitution=f"weighted std dev / expected value = {show(dispersion)}",
                       result=dispersion)
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
    if trace is not None:
        trace.step(label="Conservative value (V low)",
                   formula="V × (1 − dispersion), clamped up to the lowest comparable evidence",
                   inputs={"expected value": v_exp, "dispersion": dispersion},
                   substitution=f"{show(v_exp)} × (1 − {show(dispersion)})",
                   result=v_low)
        trace.step(label="Optimistic value (V high)",
                   formula="V × (1 + dispersion), clamped down to the highest comparable evidence",
                   inputs={"expected value": v_exp, "dispersion": dispersion},
                   substitution=f"{show(v_exp)} × (1 + {show(dispersion)})",
                   result=v_high)
    confidence = _clamp(record.data_quality.mean_extraction_confidence, ZERO, ONE)
    if len(candidates) == 1:
        confidence = min(confidence, Decimal("0.5"))
        if trace is not None:
            trace.assumption("valuation confidence cap",
                             "capped at 50% because only one valuation candidate exists")
    values = {Scenario.CONSERVATIVE: v_low, Scenario.EXPECTED: v_exp, Scenario.OPTIMISTIC: v_high}

    repair_base = _repairs_base(record, assumptions)
    repairs = {}
    if repair_base is not None:
        repairs = {Scenario.CONSERVATIVE: money(repair_base * assumptions.repairs.high_multiplier),
                   Scenario.EXPECTED: repair_base,
                   Scenario.OPTIMISTIC: money(repair_base * assumptions.repairs.low_multiplier)}
    if trace is not None:
        sqft_value = record.attributes.sqft.value if record.attributes.sqft else None
        condition_value = record.condition.condition if record.condition else None
        if sqft_value is None:
            trace.unresolved("Repair estimate: property square footage was never extracted, so no "
                             "repair budget can be computed (it is never silently treated as $0).")
        else:
            psf_rate = assumptions.repairs.psf_by_condition.get(
                condition_value or "moderate", assumptions.repairs.psf_by_condition.get("moderate"))
            inferred = condition_value is None
            trace.step(label="Base repair budget",
                       formula="sqft × $/sqft(condition) × regional index",
                       inputs={"sqft": sqft_value, "$/sqft": psf_rate,
                               "regional index": assumptions.repairs.regional_index},
                       substitution=f"{show(sqft_value)} × {show(psf_rate)} × {show(assumptions.repairs.regional_index)}",
                       result=repair_base)
            trace.input("Condition level", condition_value or "moderate",
                        note="inferred fallback 'moderate' — no condition signal was extracted"
                             if inferred else "reported condition")
            trace.assumption("$ per sqft by condition", show(assumptions.repairs.psf_by_condition))
            for scenario_name, multiplier in (("conservative", assumptions.repairs.high_multiplier),
                                              ("expected", ONE),
                                              ("optimistic", assumptions.repairs.low_multiplier)):
                scenario_key = Scenario(scenario_name)
                trace.step(label=f"Repairs ({scenario_name})",
                           formula="repair_base × scenario multiplier",
                           inputs={"base": repair_base, "multiplier": multiplier},
                           substitution=f"{show(repair_base)} × {show(multiplier)}",
                           result=repairs.get(scenario_key, ZERO))

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
    if trace is not None:
        trace.step(label="Acquisition cost rate",
                   formula="closing% + title% + acquisition fee% + transfer tax%",
                   inputs={}, substitution=" + ".join([
                       f"closing {show(assumptions.acquisition.closing_pct)}",
                       f"title {show(assumptions.acquisition.title_pct)}",
                       f"fee {show(assumptions.acquisition.acq_fee_pct)}",
                       f"transfer tax {show(transfer_tax_rate(assumptions.acquisition.transfer_tax_lookup_key))}"]),
                   result=acq_pct)
        trace.step(label="Acquisition flat costs",
                   formula="escrow + inspection + legal",
                   inputs={}, substitution=f"{show(assumptions.acquisition.escrow_flat)} + "
                                           f"{show(assumptions.acquisition.inspection_flat)} + "
                                           f"{show(assumptions.acquisition.legal_flat)}",
                   result=acq_flat)
        trace.step(label="Resale cost rate",
                   formula="commission% + seller closing% + concessions% + misc%",
                   inputs={}, substitution=f"{show(assumptions.resale.commission_pct)} + "
                                           f"{show(assumptions.resale.seller_closing_pct)} + "
                                           f"{show(assumptions.resale.concessions_pct)} + "
                                           f"{show(assumptions.resale.misc_pct)}",
                   result=resale_pct)
        trace.step(label="Holding period (base)",
                   formula="acquisition months + repair months(condition) + market days / 30",
                   inputs={"acquisition months": assumptions.holding.acquisition_months,
                           "repair months": _holding_months_base(record, assumptions)},
                   substitution=show(months_base), result=months_base)
    for scenario, value in values.items():
        if value is None:
            continue
        months = _q(months_base * HOLDING_PERIOD_FACTOR[scenario], Q4) or ZERO
        monthly = money(taxes_annual / MONTHS_PER_YEAR + value * (assumptions.holding.insurance_pct_yr + assumptions.holding.maintenance_pct_yr) / MONTHS_PER_YEAR
                        + assumptions.holding.utilities_monthly + hoa_dues) or ZERO
        holding = money(monthly * months) or ZERO
        repair_cost = repairs.get(scenario, ZERO)
        acquisition = money(value * acq_pct + acq_flat)
        resale = money(value * resale_pct)
        costs[scenario] = CostBlock(acquisition=acquisition or ZERO, repairs=repair_cost or ZERO,
                                    holding=holding or ZERO, resale=resale or ZERO, financing=ZERO)
        if trace is not None:
            scenario_name = scenario.value
            trace.step(label=f"Holding period ({scenario_name})",
                       formula="base months × scenario factor",
                       inputs={"base months": months_base, "factor": HOLDING_PERIOD_FACTOR[scenario]},
                       substitution=f"{show(months_base)} × {show(HOLDING_PERIOD_FACTOR[scenario])}",
                       result=months)
            trace.step(label=f"Monthly carrying cost ({scenario_name})",
                       formula="annual taxes/12 + value × (insurance%yr + maintenance%yr)/12 + utilities + HOA dues",
                       inputs={"annual taxes": taxes_annual, "insurance %yr": assumptions.holding.insurance_pct_yr,
                               "maintenance %yr": assumptions.holding.maintenance_pct_yr,
                               "utilities": assumptions.holding.utilities_monthly, "HOA monthly": hoa_dues},
                       substitution=f"{show(taxes_annual)}/12 + {show(value)}×({show(assumptions.holding.insurance_pct_yr)}"
                                   f"+{show(assumptions.holding.maintenance_pct_yr)})/12 + "
                                   f"{show(assumptions.holding.utilities_monthly)} + {show(hoa_dues)}",
                       result=monthly)
            trace.step(label=f"Holding cost ({scenario_name})",
                       formula="monthly × months",
                       inputs={"monthly": monthly, "months": months},
                       substitution=f"{show(monthly)} × {show(months)}", result=holding)
            trace.step(label=f"Acquisition costs ({scenario_name})",
                       formula="value × acq_pct + flat",
                       inputs={"value": value, "acq rate": acq_pct, "flat": acq_flat},
                       substitution=f"{show(value)} × {show(acq_pct)} + {show(acq_flat)}", result=acquisition)
            trace.step(label=f"Resale costs ({scenario_name})",
                       formula="value × resale%",
                       inputs={"value": value, "resale rate": resale_pct},
                       substitution=f"{show(value)} × {show(resale_pct)}", result=resale)
        potential_s = {Scenario.CONSERVATIVE: liabilities.potential,
                       Scenario.EXPECTED: potential_weighted,
                       Scenario.OPTIMISTIC: ZERO}[scenario]
        confirmed = liabilities.confirmed or ZERO
        gross = money(value - confirmed) or ZERO
        equity[scenario] = EquityBlock(gross=gross,
                                       adjusted=money(value - confirmed - potential_s),
                                       net_realizable=money(value * (ONE - resale_pct) - confirmed - potential_s - holding),
                                       equity_pct=_q(gross / value, Q6) if value != ZERO else None)
        if trace is not None:
            scenario_name = scenario.value
            trace.step(label=f"Gross equity ({scenario_name})",
                       formula="value − confirmed obligations",
                       inputs={"value": value, "confirmed obligations": confirmed},
                       substitution=f"{show(value)} − {show(confirmed)}", result=gross)
            trace.step(label=f"Adjusted equity ({scenario_name})",
                       formula="value − confirmed − scenario potential bucket",
                       inputs={"value": value, "confirmed": confirmed, "potential": potential_s},
                       substitution=f"{show(value)} − {show(confirmed)} − {show(potential_s)}",
                       result=equity[scenario].adjusted)
            trace.step(label=f"Net realizable equity ({scenario_name})",
                       formula="value × (1 − resale%) − confirmed − potential − holding",
                       substitution=f"{show(value)} × (1−{show(resale_pct)}) − {show(confirmed)} − "
                                    f"{show(potential_s)} − {show(holding)}",
                       result=equity[scenario].net_realizable)
            trace.input("Potential bucket used", scenario.value,
                        note="conservative uses full potential; expected uses probability-weighted potential; optimistic uses none")
        # ARV is the after-repair value and exists only when repairs are computable
        # (spec §7.2; recapture multiplier 1.0, deliberately unaggressive).
        repair_value = repairs.get(scenario)
        arv_by_scenario[scenario] = money(value + repair_value) if repair_value is not None else None
        if trace is not None and repair_value is not None:
            trace.step(label=f"After-repair value ARV ({scenario.value})",
                       formula="value + repairs (recapture multiplier 1.0)",
                       inputs={"value": value, "repairs": repair_value},
                       substitution=f"{show(value)} + {show(repair_value)}",
                       result=arv_by_scenario[scenario])
    return UnderwritingResult(property_id=record.property_id, assumption_set_id=assumptions.id, engine_version=ENGINE_VERSION,
                              status="ok", value=ValueBlock(v_low=v_low, v_expected=v_exp, v_high=v_high, dispersion=dispersion,
                              arv_by_scenario=arv_by_scenario,
                              candidates_used=[{"type": kind, "value": money(value), "weight": weight} for kind, value, weight in candidates],
                              valuation_confidence=confidence),
                              liabilities=liabilities, equity=equity, costs=costs, holding_months_base=months_base,
                              debt_data_present=debt_data_present, confidence=confidence)
