"""Golden-fixture generator for the ACQ numeric core (WP-6/7/8 fixtures).

Recomputes the expected outputs for every ``fixtures/normalized/*.json`` record
directly from the specification formulas (spec S7, S8, S9, S10) and writes:

- ``fixtures/underwriting/NN_<slug>.<assumption>.json``  (12 x 3 assumption sets)
- ``fixtures/strategies/NN_<slug>.json``                 (default assumptions)
- ``fixtures/scores/NN_<slug>.json``                     (default assumptions)
- ``worksheet.csv`` next to each directory               (every calculation step)

This module deliberately does NOT import ``finance``, ``strategies`` or
``scoring`` -- the goldens are the independent hand-computation those engines
are checked against (tests/test_golden_fixtures.py).

GOLDEN FORMULA SET v1 (interpretation decisions, all traced to the spec):

General
- Money quantized to 0.01 ROUND_HALF_UP at every labelled step; downstream
  steps consume the quantized value ("calculator style", fully auditable).
  Ratios quantized to 6 dp, scores/distress points to 4 dp, dispersion to 6 dp.
- Reference date for every date-relative term = ``data_quality.newest_report_date``.
  If absent, date-relative terms are 0 (no evidence of recency). Engines must be
  deterministic, so wall-clock dates are never used.
- months_between(a, b) = (b.year-a.year)*12 + (b.month-a.month) (fixture event
  dates are all on the 1st of a month).
- decay(months, half_life) = 0.5 ** (months / half_life).

Value (spec S7.2)
- Base weight per candidate: ``assumptions.valuation_weights[type]`` if present,
  else the spec table: avm .30, comp .35, comp_listing .15, list_price .10,
  assessed .10, unknown types 1.0.
- Adjustment: avm -> x reported_confidence x recency decay (90-day half-life vs
  the reference date). Other types -> 1.0 (no comp-quality metrics exist in the
  fixture data; the adjustment is neutral). All fixture candidates share one
  as_of date, so any common decay factor cancels in the normalized average.
- V_exp = sum(v*w)/sum(w). disp = 0.15 for a single candidate, else
  clamp(weighted_stdev/V_exp, 0.04, 0.30). V_low/V_high = V_exp*(1 +/- disp).
  (comp_range clamps not exercised: no fixture supplies value_low/high.)
- valuation_confidence = mean_extraction_confidence, capped at 0.5 when there is
  a single candidate (spec S7.2).
- ARV_s = V_s + repairs_s (recapture multiplier 1.0, spec S7.2 fallback; no
  renovated comps in fixtures). ARV exists only when repairs are computable.

Liabilities (spec S7.3, S6.3, S6.5)
- Open mortgages at the same position are a conflict: the highest balance is
  kept (conservative tie-break, spec S6.2/S6.3) and noted in the breakdown.
- A published trustee bid replaces the first-mortgage balance (spec S7.3
  precedence favors the bid); |bid - estimated|/bid > 0.20 would be noted as
  bid_mismatch in the breakdown (not triggered by any fixture).
- CONFIRMED = open mortgage balances + active liens recorded_against_property
  (with amounts) + delinquent taxes + HOA arrears. Released/satisfied liens
  excluded.
- POTENTIAL = active owner_named_only/unknown liens with amounts + active liens
  without amounts at ``unknown_lien_medians`` (any basis). maximum = C + P.
- Expected-scenario potential weighting = sum(potential_i x
  attachment_probability[basis_i]); conservative weights potential in full;
  optimistic excludes it (spec S7.1/S7.4).

Costs (spec S7.5)
- Condition defaults to "moderate" when no condition signal exists.
- repairs_base = sqft * psf[condition] * regional_index; scenario x {high 1.4 /
  1.0 / low 0.75 multipliers from the assumption set}. Missing sqft -> repairs
  unavailable: flip and rental return unavailable, other strategies compute
  with repairs = 0 (spec S7.7).
- acquisition_s = V_s x (closing_pct + title_pct + acq_fee_pct) + escrow +
  inspection + legal. Transfer tax 0 (lookup key null). Financing points/flat
  are charged inside the flip strategy where a purchase price exists.
- holding: months_base = acquisition_months + repair_months[condition] +
  market_days_default/30; scenario x {1.5, 1.0, 0.75} (spec S7.1).
  monthly_s = taxes/12 + V_s*(insurance_pct + maintenance_pct)/12 + utilities +
  HOA dues. (Loan interest excluded at underwriting: no loan exists yet.)
- resale_pct = commission + seller_closing + concessions + misc; resale_s =
  V_s * resale_pct. Staging is flip-only and added there.

Equity (spec S7.4) per scenario
- gross = V_s - CONFIRMED; adjusted = V_s - CONFIRMED - potential_weighted_s;
  net_realizable = V_s*(1-resale_pct) - CONFIRMED - potential_weighted_s -
  holding_s; equity_pct = gross / V_s.

Failure cases (spec S7.7)
- No value candidates -> status insufficient_data, no value/equity/cost blocks.

Strategies (spec S8; default assumption set; price P = 0.75 x V_exp rounded to
the nearest $5,000, inside the offer-grid span)
- cash: all_in = P + acquisition + repairs + holding;
  profit = V_s*(1-resale_pct) - all_in; roi = profit/all_in;
  margin_of_safety = (V_s - all_in)/V_s;
  MAO = V_s*(1-cash_target_margin) - repairs - holding - acquisition - resale.
- flip: hard money loan = ltv x (P + repairs); financing = points x loan +
  financing_flat + loan x rate/12 x months_s; resale_flip = ARV*resale_pct +
  staging; all_in = P + repairs + holding + financing + acquisition +
  resale_flip; profit = ARV - all_in; margin = profit/ARV;
  CoC = profit / (down + holding + acquisition), down = P + repairs - loan.
  MAO solves the linear equation MAO = K - a*(MAO+repairs) - financing_flat
  with a = ltv*(points + rate/12*months_s), K = ARV*(1-margin_target) -
  repairs - holding - resale_flip - acquisition. Target margin: the
  flip_target_margin_by_arv_band "default" (fixture sets carry no ARV bands).
- wholesale: threshold = ARV*investor_pct - repairs;
  max_contract = threshold - min_assignment_spread (the target fee);
  spread = threshold - contract price; viable iff spread >= min spread AND
  DCS >= 60 (spec S8 gate on Data Confidence).
- rental: EGI = rent*12*(1-vacancy); OpEx = taxes + V_s*insurance_pct +
  HOA*12 + EGI*(maintenance + management + 5% reserves per spec);
  NOI = EGI - OpEx; cap = NOI/P; cash_flow = NOI (no rental debt service is
  parameterized); CoC = NOI/(P + acquisition + repairs); DSCR null.
- subject_to: detection only, always requires_human_review; the four spec
  conditions are reported in metrics (rate_vs_market is null: no market-rate
  input exists in the contract).
- foreclosure: unavailable unless foreclosure.is_active. total_obligations =
  published_bid (or first balance) + surviving junior recorded liens +
  delinquent taxes + transfer (0). spread = V_conservative - total_obligations
  - repairs_s - auction_holding (2 months of conservative monthly holding).
  Interior unknown would force high repairs in all scenarios (not triggered:
  fixture 04 has a condition signal). Risk flags emitted as 0/1 metrics.

Offer grid (spec S9)
- 9 offers: V_exp x {0.60..1.00 step 0.05}, rounded to the nearest $5,000,
  x3 scenarios, plus MAO_cash / MAO_flip markers (labelled) per scenario.
- payoff_fees = $1,200 when any open mortgage exists (spec S9.2 default).
- confirmed_payoffs = CONFIRMED + payoff_fees; potential_payoffs = POTENTIAL.
- closing = offer x title_pct + escrow (transfer 0, recording 0, commission 0:
  the platform's offers are direct, spec S9.2 "commission (0 if direct)").
- proceeds_high/expected/low = offer - confirmed - closing [ - weighted /
  full potential ]; is_short_sale = proceeds_low < 0.
- buyer_basis = offer + acquisition + repairs + holding;
  profit = V_s*(1-resale_pct) - buyer_basis; roi = profit/buyer_basis.

Scoring (spec S10; fixed scoring_config_id below; expected-scenario inputs)
- Best strategy = viable expected-scenario result (subject_to excluded) with
  the highest profit; tie-break priority cash > flip > wholesale > rental >
  foreclosure. alternatives = remaining viable strategies by profit desc.
- FOS = 100*(.30 n(profit,0,150k) + .25 n(roi,0,.5) + .20 n(equity_pct,0,.6)
  + .15 n(discount_to_value,0,.35) + .10 n(margin_of_safety,0,.35));
  discount_to_value = (V_exp - MAO_best)/V_exp. With no viable strategy the
  strategy-derived terms are 0 and the equity term still counts.
- Distress: spec point table, each item x decay(months_since_event, 18),
  per-category caps, capped at 100. NTS base 30 when filed <=30 days before
  the reference date else 24. "high equity" = expected gross equity_pct >= 0.5.
- DCS = 100*(.30 coverage + .20 corroboration + .20 recency
  + .15 (1 - min(1, material_conflicts/5)) + .10 verified/22
  + .05 mean_extraction_confidence); corroboration = (# fields with >=2
  sources)/22; recency = 1 when a newest_report_date exists else 0.
- RISK = clamp(6*active_liens + 15*active_bk + 12*(stage in nts/auction)
  + 10*owner_only_liens_over_10k + 10*title_flags + 8*owner_occupied
  + 8*hoa_arrears + 10*material_conflicts + 12*(DCS<50) + 6*federal_tax_lien,
  0, 100). title_flags = open flags of type identity_conflict /
  conflicting_mortgage / foreclosure_unclear.
- OVERALL = clamp(.5 FOS + .2 Distress + .2 DCS - .25 RISK, 0, 100).
  Gates: insufficient_data -> unranked; open gating flag -> unranked;
  DCS < 40 -> cap 45 + "dcs_below_40" (still rankable, spec S10);
  active foreclosure with DCS < 75 -> cap 70 (spec S8).
"""
from __future__ import annotations

import csv
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, getcontext
from pathlib import Path

from contracts import (
    AssumptionSet,
    AttachmentBasis,
    NormalizedProperty,
    Scenario,
    StrategyType,
)

getcontext().prec = 40

FIXTURES = Path(__file__).parent
Q2 = Decimal("0.01")
Q4 = Decimal("0.0001")
Q6 = Decimal("0.000001")
SCORING_CONFIG_ID = "20000000-0000-0000-0000-000000000001"
STRATEGY_PRIORITY = [
    StrategyType.CASH,
    StrategyType.FLIP,
    StrategyType.WHOLESALE,
    StrategyType.RENTAL,
    StrategyType.FORECLOSURE,
]
SPEC_TYPE_WEIGHT = {
    "avm": Decimal("0.30"),
    "comp": Decimal("0.35"),
    "comp_listing": Decimal("0.15"),
    "list_price": Decimal("0.10"),
    "assessed": Decimal("0.10"),
}
TAX_LIEN_TYPES = {"federal_tax", "state_tax", "property_tax"}
TITLE_FLAG_TYPES = {"identity_conflict", "conflicting_mortgage", "foreclosure_unclear"}
SCENARIO_ORDER = [Scenario.CONSERVATIVE, Scenario.EXPECTED, Scenario.OPTIMISTIC]
HOLDING_MULT = {Scenario.CONSERVATIVE: Decimal("1.5"), Scenario.EXPECTED: Decimal("1"), Scenario.OPTIMISTIC: Decimal("0.75")}

ROWS: list[dict[str, str]] = []


def q(value: Decimal | None, quantum: Decimal = Q2) -> Decimal | None:
    return None if value is None else value.quantize(quantum, rounding=ROUND_HALF_UP)


def note(fixture: str, assumption: str, stage: str, step: str, formula: str, inputs: str, value) -> Decimal | None:
    if isinstance(value, Decimal):
        value = format(value, "f")
    ROWS.append({"fixture": fixture, "assumption": assumption, "stage": stage, "step": step,
                 "formula": formula, "inputs": inputs, "value": "" if value is None else str(value)})
    return value


def months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)


def decay(months: Decimal, half_life: Decimal) -> Decimal:
    return Decimal("0.5") ** (months / half_life)


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def n_term(value: Decimal | None, low: Decimal, high: Decimal) -> Decimal:
    if value is None or high == low:
        return Decimal("0")
    return q(clamp((value - low) / (high - low), Decimal("0"), Decimal("1")), Q6)


def tracked(block):
    return block.value if block and block.value is not None else None


def round_5000(value: Decimal) -> Decimal:
    return (value / Decimal("5000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * Decimal("5000")


# ---------------------------------------------------------------- underwriting

def underwrite(fixture: str, record: NormalizedProperty, a: AssumptionSet) -> dict:
    stage = "underwriting"
    ref = record.data_quality.newest_report_date
    candidates = [c for c in record.valuation_candidates if c.value.value is not None]
    mortgages = [m for m in record.mortgages if m.is_open]
    active_liens = [l for l in record.liens if l.status not in ("released", "satisfied")]
    debt_data_present = bool(record.mortgages or record.liens)

    # --- liabilities (computed even when value is missing) ---
    confirmed = Decimal("0")
    potential = Decimal("0")
    potential_weighted = Decimal("0")
    breakdown: list[dict] = []
    by_position: dict[str, list] = {}
    for m in mortgages:
        by_position.setdefault(m.position, []).append(m)
    for position, group in sorted(by_position.items()):
        chosen = max(group, key=lambda m: tracked(m.estimated_balance) or Decimal("0"))
        balance = tracked(chosen.estimated_balance)
        if (
            position == "1"
            and record.foreclosure
            and record.foreclosure.is_active
            and tracked(record.foreclosure.published_bid) is not None
        ):
            bid = tracked(record.foreclosure.published_bid)
            if balance is not None and bid and abs(bid - balance) / bid > Decimal("0.20"):
                note(fixture, a.name, stage, "bid_mismatch", "|bid - est| / bid > 0.20", f"bid={bid} est={balance}", True)
            note(fixture, a.name, stage, f"mortgage[{position}].balance", "published bid replaces first balance",
                 f"bid={bid}", bid)
            balance = bid
        if balance is None:
            continue
        if len(group) > 1:
            note(fixture, a.name, stage, f"mortgage[{position}].conflict",
                 "same-position conflict: keep highest balance",
                 ",".join(format(tracked(m.estimated_balance), "f") for m in group), balance)
        balance = q(balance)
        confirmed += balance
        breakdown.append({"label": f"mortgage:{position}", "amount": balance, "basis": "recorded",
                          "is_estimated": chosen.estimated_balance.is_estimated})
    for lien in active_liens:
        amount = tracked(lien.amount)
        estimated = lien.amount_is_estimated
        if amount is None:
            amount = a.unknown_lien_medians.get(lien.lien_type, Decimal("0"))
            estimated = True
        amount = q(amount)
        basis = lien.attachment_basis
        if amount and (lien.amount is None or lien.amount.value is None):
            potential += amount
            potential_weighted += q(amount * a.attachment_probability[basis])
        elif basis == AttachmentBasis.RECORDED_AGAINST_PROPERTY:
            confirmed += amount
        else:
            potential += amount
            potential_weighted += q(amount * a.attachment_probability[basis])
        breakdown.append({"label": lien.lien_type, "amount": amount, "basis": basis.value, "is_estimated": estimated})
    for label, value in (("delinquent_taxes", tracked(record.taxes.delinquent_amount)),
                         ("hoa_arrears", tracked(record.hoa.arrears))):
        if value is not None:
            confirmed += q(value)
            breakdown.append({"label": label, "amount": q(value), "basis": "recorded", "is_estimated": False})
    confirmed = q(confirmed)
    potential = q(potential)
    potential_weighted = q(potential_weighted)
    note(fixture, a.name, stage, "liabilities.confirmed", "sum of confirmed bucket", "", confirmed)
    note(fixture, a.name, stage, "liabilities.potential", "sum of potential bucket", "", potential)
    note(fixture, a.name, stage, "liabilities.potential_weighted", "sum(potential x attachment_probability)", "", potential_weighted)
    liabilities = {"confirmed": confirmed, "potential": potential, "maximum": q(confirmed + potential),
                   "breakdown": breakdown}

    if not candidates:
        note(fixture, a.name, stage, "status", "no value candidates -> insufficient_data", "", "insufficient_data")
        return {"property_id": str(record.property_id), "assumption_set_id": str(a.id),
                "engine_version": "golden-spec-1", "status": "insufficient_data",
                "unavailable_reason": "no_valuation_candidates",
                "value": {"v_low": None, "v_expected": None, "v_high": None, "dispersion": None,
                          "arv_by_scenario": {}, "candidates_used": [], "valuation_confidence": None},
                "liabilities": liabilities, "equity": {}, "costs": {},
                "debt_data_present": debt_data_present, "confidence": Decimal("0")}

    # --- value ---
    used = []
    weighted_sum = Decimal("0")
    weight_sum = Decimal("0")
    for c in candidates:
        base = a.valuation_weights.get(c.valuation_type, SPEC_TYPE_WEIGHT.get(c.valuation_type, Decimal("1")))
        adj = Decimal("1")
        if c.valuation_type == "avm":
            if c.reported_confidence is not None:
                adj *= Decimal(str(c.reported_confidence))
            if c.as_of and ref:
                days = max(0, (ref - c.as_of).days)
                adj *= decay(Decimal(days), Decimal("90"))
        w = q(base * adj, Q6)
        weighted_sum += c.value.value * w
        weight_sum += w
        used.append({"type": c.valuation_type, "value": q(c.value.value), "weight": w})
        note(fixture, a.name, stage, f"value.candidate[{c.valuation_type}].weight",
             "base_weight x adjustment", f"base={base} adj={q(adj, Q6)}", w)
    v_exp = q(weighted_sum / weight_sum)
    if len(candidates) == 1:
        disp = Decimal("0.15")
    else:
        variance = sum(u["weight"] * (u["value"] - v_exp) ** 2 for u in used) / weight_sum
        disp = q(clamp(variance.sqrt() / v_exp, Decimal("0.04"), Decimal("0.30")), Q6)
    v_low = q(v_exp * (1 - disp))
    v_high = q(v_exp * (1 + disp))
    confidence = record.data_quality.mean_extraction_confidence
    if len(candidates) == 1:
        confidence = min(confidence, Decimal("0.5"))
    note(fixture, a.name, stage, "value.v_expected", "sum(v*w)/sum(w)", "", v_exp)
    note(fixture, a.name, stage, "value.dispersion", "single->0.15 else clamp(wstdev/V,0.04,0.30)", "", disp)
    note(fixture, a.name, stage, "value.v_low", "V*(1-disp)", "", v_low)
    note(fixture, a.name, stage, "value.v_high", "V*(1+disp)", "", v_high)

    values = {Scenario.CONSERVATIVE: v_low, Scenario.EXPECTED: v_exp, Scenario.OPTIMISTIC: v_high}

    # --- repairs ---
    sqft = tracked(record.attributes.sqft)
    condition = record.condition.condition if record.condition else "moderate"
    repair_base = q(sqft * a.repairs.psf_by_condition[condition] * a.repairs.regional_index) if sqft is not None else None
    repairs = {}
    if repair_base is not None:
        repairs = {
            Scenario.CONSERVATIVE: q(repair_base * a.repairs.high_multiplier),
            Scenario.EXPECTED: repair_base,
            Scenario.OPTIMISTIC: q(repair_base * a.repairs.low_multiplier),
        }
        note(fixture, a.name, stage, "repairs.base", "sqft x psf[condition] x regional_index",
             f"sqft={sqft} condition={condition}", repair_base)

    # --- holding period ---
    taxes_annual = tracked(record.taxes.annual_taxes) or Decimal("0")
    hoa_dues = tracked(record.hoa.monthly_dues) or Decimal("0")
    months_base = (a.holding.acquisition_months + a.holding.repair_months_by_condition[condition]
                   + Decimal(a.holding.market_days_default) / Decimal("30"))
    note(fixture, a.name, stage, "holding.months_base",
         "acquisition_months + repair_months + market_days/30", f"condition={condition}", q(months_base, Q4))

    resale_pct = a.resale.commission_pct + a.resale.seller_closing_pct + a.resale.concessions_pct + a.resale.misc_pct
    acq_pct = a.acquisition.closing_pct + a.acquisition.title_pct + a.acquisition.acq_fee_pct
    acq_flat = a.acquisition.escrow_flat + a.acquisition.inspection_flat + a.acquisition.legal_flat

    equity: dict = {}
    costs: dict = {}
    arv: dict = {}
    for s in SCENARIO_ORDER:
        v = values[s]
        months = q(months_base * HOLDING_MULT[s], Q4)
        monthly = q(taxes_annual / 12 + v * (a.holding.insurance_pct_yr + a.holding.maintenance_pct_yr) / 12
                    + a.holding.utilities_monthly + hoa_dues)
        holding = q(monthly * months)
        rep = repairs.get(s, Decimal("0")) if repairs else Decimal("0")
        acquisition = q(v * acq_pct + acq_flat)
        resale = q(v * resale_pct)
        costs[s.value] = {"acquisition": acquisition, "repairs": rep, "holding": holding,
                          "resale": resale, "financing": Decimal("0")}
        pw_s = {Scenario.CONSERVATIVE: potential, Scenario.EXPECTED: potential_weighted,
                Scenario.OPTIMISTIC: Decimal("0")}[s]
        gross = q(v - confirmed)
        equity[s.value] = {"gross": gross, "adjusted": q(v - confirmed - pw_s),
                           "net_realizable": q(v * (1 - resale_pct) - confirmed - pw_s - holding),
                           "equity_pct": q(gross / v, Q6) if v else None}
        arv[s.value] = q(v + repairs[s]) if repairs else None
        prefix = f"scenario[{s.value}]"
        note(fixture, a.name, stage, f"{prefix}.holding", "monthly x months", f"monthly={monthly} months={months}", holding)
        note(fixture, a.name, stage, f"{prefix}.acquisition", "V x acq_pct + flats", f"acq_pct={acq_pct} flat={acq_flat}", acquisition)
        note(fixture, a.name, stage, f"{prefix}.resale", "V x resale_pct", f"resale_pct={resale_pct}", resale)
        note(fixture, a.name, stage, f"{prefix}.equity.gross", "V - confirmed", "", gross)
        note(fixture, a.name, stage, f"{prefix}.equity.adjusted", "V - confirmed - potential_weighted", f"pw={pw_s}", equity[s.value]["adjusted"])
        if arv[s.value] is not None:
            note(fixture, a.name, stage, f"{prefix}.arv", "V + repairs (recapture 1.0)", f"repairs={repairs[s]}", arv[s.value])

    value_block = {"v_low": v_low, "v_expected": v_exp, "v_high": v_high, "dispersion": disp,
                   "arv_by_scenario": arv, "candidates_used": used, "valuation_confidence": confidence}
    return {"property_id": str(record.property_id), "assumption_set_id": str(a.id),
            "engine_version": "golden-spec-1", "status": "ok", "unavailable_reason": None,
            "value": value_block, "liabilities": liabilities,
            "equity": equity, "costs": costs,
            "debt_data_present": debt_data_present, "confidence": confidence}


# ------------------------------------------------------------------ strategies

def strategies(fixture: str, record: NormalizedProperty, uw: dict, a: AssumptionSet) -> dict:
    stage = "strategies"
    name = a.name
    v_exp = uw["value"]["v_expected"]
    price = round_5000(v_exp * Decimal("0.75")) if v_exp is not None else None
    if price is not None:
        note(fixture, name, stage, "purchase_price", "round_5000(0.75 x V_exp)", f"V_exp={v_exp}", price)
    resale_pct = a.resale.commission_pct + a.resale.seller_closing_pct + a.resale.concessions_pct + a.resale.misc_pct
    sqft = tracked(record.attributes.sqft)
    results: list[dict] = []

    def base(strategy: StrategyType, s: Scenario) -> dict:
        return {"strategy": strategy.value, "scenario": s.value, "status": None, "unavailable_reason": None,
                "mao": None, "all_in_basis": None, "profit": None, "roi": None, "margin_of_safety": None,
                "metrics": {}, "inputs_echo": {"purchase_price": price} if price is not None else {}, "notices": []}

    def unavailable(strategy: StrategyType, s: Scenario, reason: str) -> dict:
        r = base(strategy, s)
        r["status"] = "unavailable"
        r["unavailable_reason"] = reason
        return r

    dcs = dcs_score(fixture, record)["dcs"]

    for s in SCENARIO_ORDER:
        skey = s.value
        v = uw["value"]["arv_by_scenario"].get(skey) if uw["value"]["arv_by_scenario"] else None
        v_as_is = {Scenario.CONSERVATIVE: uw["value"]["v_low"], Scenario.EXPECTED: uw["value"]["v_expected"],
                   Scenario.OPTIMISTIC: uw["value"]["v_high"]}[s]
        cost = uw["costs"].get(skey)

        # cash (as-is value, spec S8)
        r = base(StrategyType.CASH, s)
        if v_as_is is None or price is None:
            r = unavailable(StrategyType.CASH, s, "no_value_data")
        else:
            all_in = q(price + cost["acquisition"] + cost["repairs"] + cost["holding"])
            profit = q(v_as_is * (1 - resale_pct) - all_in)
            mao = q(v_as_is * (1 - a.strategy.cash_target_margin) - cost["repairs"] - cost["holding"]
                    - cost["acquisition"] - cost["resale"])
            r.update(status="viable" if profit >= 0 else "not_viable", mao=mao, all_in_basis=all_in, profit=profit,
                     roi=q(profit / all_in, Q6) if all_in else None,
                     margin_of_safety=q((v_as_is - all_in) / v_as_is, Q6) if v_as_is else None)
            note(fixture, name, stage, f"cash[{skey}].profit", "V*(1-resale_pct) - all_in", f"all_in={all_in}", profit)
            note(fixture, name, stage, f"cash[{skey}].mao", "V*(1-margin) - repairs - holding - acq - resale", "", mao)
        results.append(r)

        # flip (ARV, spec S8)
        if v_as_is is None or price is None:
            r = unavailable(StrategyType.FLIP, s, "no_value_data")
        elif sqft is None or v is None:
            r = unavailable(StrategyType.FLIP, s, "no_sqft_data")
        else:
            months = q((a.holding.acquisition_months + a.holding.repair_months_by_condition[
                record.condition.condition if record.condition else "moderate"]
                + Decimal(a.holding.market_days_default) / 30) * HOLDING_MULT[s], Q4)
            hm = a.strategy.hard_money
            loan = q(hm["ltv"] * (price + cost["repairs"]))
            financing = q(hm["points"] * loan + a.acquisition.financing_flat + loan * hm["rate"] / 12 * months)
            resale_flip = q(v * resale_pct + a.resale.staging_flat)
            all_in = q(price + cost["repairs"] + cost["holding"] + financing + cost["acquisition"] + resale_flip)
            profit = q(v - all_in)
            down = q(price + cost["repairs"] - loan)
            coc_den = down + cost["holding"] + cost["acquisition"]
            margin_target = a.strategy.flip_target_margin_by_arv_band.get("default", Decimal("0.2"))
            coeff = hm["ltv"] * (hm["points"] + hm["rate"] / 12 * months)
            k_const = q(v * (1 - margin_target) - cost["repairs"] - cost["holding"] - resale_flip - cost["acquisition"])
            mao = q((k_const - coeff * cost["repairs"] - a.acquisition.financing_flat) / (1 + coeff))
            r = base(StrategyType.FLIP, s)
            r.update(status="viable" if profit >= 0 else "not_viable", mao=mao, all_in_basis=all_in, profit=profit,
                     roi=q(profit / all_in, Q6) if all_in else None,
                     margin_of_safety=q((v - all_in) / v, Q6) if v else None,
                     metrics={"coc": q(profit / coc_den, Q6) if coc_den else None,
                              "margin": q(profit / v, Q6) if v else None, "financing": financing, "loan": loan})
            note(fixture, name, stage, f"flip[{skey}].financing", "points*loan + flat + loan*rate/12*months",
                 f"loan={loan} months={months}", financing)
            note(fixture, name, stage, f"flip[{skey}].profit", "ARV - all_in", f"ARV={v} all_in={all_in}", profit)
            note(fixture, name, stage, f"flip[{skey}].mao", "(K - a*repairs - flat)/(1+a)",
                 f"K={k_const} a={q(coeff, Q6)}", mao)
        results.append(r)

        # wholesale (spec S8)
        if v is None or price is None:
            r = unavailable(StrategyType.WHOLESALE, s, "no_value_data" if v_as_is is None else "no_sqft_data")
        else:
            threshold = q(v * a.strategy.wholesale_investor_pct - cost["repairs"])
            max_contract = q(threshold - a.strategy.min_assignment_spread)
            spread = q(threshold - price)
            viable = spread >= a.strategy.min_assignment_spread and dcs >= 60
            r = base(StrategyType.WHOLESALE, s)
            r.update(status="viable" if viable else "not_viable", mao=max_contract, profit=spread,
                     metrics={"investor_threshold": threshold, "spread": spread, "dcs": dcs})
            note(fixture, name, stage, f"wholesale[{skey}].spread", "ARV*investor_pct - repairs - contract",
                 f"threshold={threshold} price={price} dcs={dcs}", spread)
        results.append(r)

        # rental (spec S8)
        rent = tracked(record.rental.rent_estimate)
        if rent is None:
            r = unavailable(StrategyType.RENTAL, s, "no_rent_data")
        elif v_as_is is None or price is None:
            r = unavailable(StrategyType.RENTAL, s, "no_value_data")
        elif sqft is None:
            r = unavailable(StrategyType.RENTAL, s, "no_sqft_data")
        else:
            rental_ass = a.strategy.rental
            egi = q(rent * 12 * (1 - rental_ass["vacancy"]))
            opex = q((tracked(record.taxes.annual_taxes) or Decimal("0")) + v_as_is * a.holding.insurance_pct_yr
                     + (tracked(record.hoa.monthly_dues) or Decimal("0")) * 12
                     + egi * (rental_ass["maintenance_pct"] + rental_ass["management_pct"] + Decimal("0.05")))
            noi = q(egi - opex)
            invested = price + cost["acquisition"] + cost["repairs"]
            r = base(StrategyType.RENTAL, s)
            r.update(status="viable" if noi > 0 else "not_viable", profit=noi,
                     metrics={"egi": egi, "opex": opex, "noi": noi,
                              "cap_rate": q(noi / price, Q6) if price else None, "cash_flow": noi,
                              "coc": q(noi / invested, Q6) if invested else None, "dscr": None})
            note(fixture, name, stage, f"rental[{skey}].noi", "EGI - OpEx", f"egi={egi} opex={opex}", noi)
        results.append(r)

        # subject_to (detection only, spec S8)
        r = base(StrategyType.SUBJECT_TO, s)
        first = next((m for m in record.mortgages if m.is_open and m.position == "1"), None)
        balance = tracked(first.estimated_balance) if first else None
        distress_present = bool(
            (record.foreclosure and record.foreclosure.is_active)
            or any(b.status == "active" for b in record.bankruptcies)
            or (tracked(record.taxes.delinquent_amount) or 0) > 0
        )
        r["status"] = "requires_human_review"
        r["notices"] = ["detection only - due-on-sale / legal review required"]
        r["metrics"] = {
            "condition_rate_200bps_below_market": None,
            "condition_balance_le_80pct_value": (Decimal("1") if balance is not None and v_as_is
                                                 and balance <= v_as_is * Decimal("0.8")
                                                 else Decimal("0") if balance is not None and v_as_is else None),
            "condition_no_acceleration": Decimal("1"),
            "condition_distress_present": Decimal("1") if distress_present else Decimal("0"),
        }
        results.append(r)

        # foreclosure (spec S8)
        if not (record.foreclosure and record.foreclosure.is_active):
            r = unavailable(StrategyType.FORECLOSURE, s, "no_active_foreclosure")
        elif uw["value"]["v_low"] is None:
            r = unavailable(StrategyType.FORECLOSURE, s, "no_value_data")
        else:
            bid = tracked(record.foreclosure.published_bid)
            first = next((m for m in record.mortgages if m.is_open and m.position == "1"), None)
            obligations = bid if bid is not None else (tracked(first.estimated_balance) if first else Decimal("0")) or Decimal("0")
            juniors = [m for m in record.mortgages if m.is_open and m.position != "1"]
            junior_debt = sum((tracked(m.estimated_balance) or Decimal("0") for m in juniors), Decimal("0"))
            junior_debt += sum((tracked(l.amount) or Decimal("0") for l in record.liens
                                if l.status not in ("released", "satisfied")
                                and l.attachment_basis == AttachmentBasis.RECORDED_AGAINST_PROPERTY), Decimal("0"))
            total_obligations = q(obligations + junior_debt + (tracked(record.taxes.delinquent_amount) or Decimal("0")))
            v_low = uw["value"]["v_low"]
            monthly = q((tracked(record.taxes.annual_taxes) or Decimal("0")) / 12
                        + v_low * (a.holding.insurance_pct_yr + a.holding.maintenance_pct_yr) / 12
                        + a.holding.utilities_monthly + (tracked(record.hoa.monthly_dues) or Decimal("0")))
            auction_holding = q(monthly * 2)
            interior_unknown = record.condition is None
            repairs_s = uw["costs"][skey]["repairs"] if not interior_unknown else (
                uw["costs"][Scenario.CONSERVATIVE.value]["repairs"])
            spread = q(v_low - total_obligations - repairs_s - auction_holding)
            active_liens = [l for l in record.liens if l.status not in ("released", "satisfied")]
            r = base(StrategyType.FORECLOSURE, s)
            r.update(status="viable" if spread > 0 else "not_viable", profit=spread,
                     metrics={"total_obligations": total_obligations, "auction_holding": auction_holding,
                              "spread": spread,
                              "flag_junior_liens_present": Decimal("1") if junior_debt > 0 else Decimal("0"),
                              "flag_irs_lien": Decimal("1") if any(l.lien_type == "federal_tax" for l in active_liens) else Decimal("0"),
                              "flag_hoa_super_priority": Decimal("1") if record.hoa.has_lien else Decimal("0"),
                              "flag_owner_occupied": Decimal("1") if record.ownership.is_owner_occupied else Decimal("0"),
                              "flag_interior_unknown": Decimal("1") if interior_unknown else Decimal("0"),
                              "flag_postponements_ge_3": Decimal("1") if record.foreclosure.postponement_count >= 3 else Decimal("0")})
            note(fixture, name, stage, f"foreclosure[{skey}].spread",
                 "V_low - total_obligations - repairs - auction_holding",
                 f"obligations={total_obligations} repairs={repairs_s} auction_holding={auction_holding}", spread)
        results.append(r)

    # offer grid (spec S9)
    points: list[dict] = []
    if v_exp is not None:
        mao_by = {(r["strategy"], r["scenario"]): r["mao"] for r in results if r["mao"] is not None}
        for s in SCENARIO_ORDER:
            skey = s.value
            v_s = {Scenario.CONSERVATIVE: uw["value"]["v_low"], Scenario.EXPECTED: uw["value"]["v_expected"],
                   Scenario.OPTIMISTIC: uw["value"]["v_high"]}[s]
            cost = uw["costs"][skey]
            payoff_fees = Decimal("1200") if any(m.is_open for m in record.mortgages) else Decimal("0")
            confirmed_payoffs = q(uw["liabilities"]["confirmed"] + payoff_fees)
            potential_payoffs = uw["liabilities"]["potential"]
            potential_weighted = sum(
                (q((tracked(l.amount) if tracked(l.amount) is not None else a.unknown_lien_medians.get(l.lien_type, Decimal("0")))
                    * a.attachment_probability[l.attachment_basis])
                 for l in record.liens if l.status not in ("released", "satisfied")
                 and l.attachment_basis != AttachmentBasis.RECORDED_AGAINST_PROPERTY),
                Decimal("0"))
            offers = [round_5000(v_exp * (Decimal("0.60") + Decimal(k) * Decimal("0.05"))) for k in range(9)]
            markers = [("mao_cash", mao_by.get(("cash", skey))), ("mao_flip", mao_by.get(("flip", skey)))]
            grid = [("grid", None, o) for o in offers] + [(label, label, m) for label, m in markers if m is not None]
            for _, label, offer in sorted(grid, key=lambda item: item[2]):
                closing = q(offer * a.acquisition.title_pct + a.acquisition.escrow_flat)
                proceeds_high = q(offer - confirmed_payoffs - closing)
                proceeds_expected = q(proceeds_high - potential_weighted)
                proceeds_low = q(proceeds_high - potential_payoffs)
                buyer_basis = q(offer + cost["acquisition"] + cost["repairs"] + cost["holding"])
                profit = q(v_s * (1 - resale_pct) - buyer_basis)
                points.append({"offer_price": offer, "scenario": skey, "confirmed_payoffs": confirmed_payoffs,
                               "potential_payoffs": potential_payoffs, "closing_costs": closing,
                               "proceeds_low": proceeds_low, "proceeds_expected": proceeds_expected,
                               "proceeds_high": proceeds_high, "buyer_basis": buyer_basis, "profit": profit,
                               "roi": q(profit / buyer_basis, Q6) if buyer_basis else None,
                               "is_short_sale": proceeds_low < 0, "label": label})
        note(fixture, name, stage, "offer_grid.points", "9 offers x 3 scenarios + MAO markers",
             f"payoff_fees={payoff_fees}", len(points))
    grid = {"property_id": uw["property_id"], "points": points, "interpolatable": True}
    return {"property_id": uw["property_id"], "assumption_set": name, "purchase_price": price,
            "strategies": results, "offer_grid": grid}


# ---------------------------------------------------------------------- scores

def dcs_score(fixture: str, record: NormalizedProperty) -> dict:
    dq = record.data_quality
    corroborated = sum(1 for count in dq.source_counts_by_field.values() if count >= 2)
    corrob = q(Decimal(corroborated) / Decimal("22"), Q6)
    conflict_penalty = min(Decimal("1"), Decimal(dq.material_conflict_count) / Decimal("5"))
    verification = q(Decimal(dq.verified_field_count) / Decimal("22"), Q6)
    recency = Decimal("1") if dq.newest_report_date else Decimal("0")
    dcs = q(Decimal("100") * (Decimal("0.30") * dq.critical_field_coverage + Decimal("0.20") * corrob
                              + Decimal("0.20") * recency + Decimal("0.15") * (1 - conflict_penalty)
                              + Decimal("0.10") * verification + Decimal("0.05") * dq.mean_extraction_confidence), Q4)
    note(fixture, "default", "scoring", "dcs",
         "100*(.30 coverage + .20 corroboration + .20 recency + .15 (1-conflict) + .10 verification + .05 extraction)",
         f"coverage={dq.critical_field_coverage} corrob={corrob} recency={recency} "
         f"conflict_penalty={conflict_penalty} verification={verification} mean={dq.mean_extraction_confidence}", dcs)
    return {"dcs": dcs, "corroboration": corrob, "recency": recency,
            "conflict_penalty": conflict_penalty, "verification": verification}


def score(fixture: str, record: NormalizedProperty, uw: dict, strategies_result: dict) -> dict:
    stage = "scoring"
    ref = record.data_quality.newest_report_date
    results = strategies_result["strategies"]
    expected = [r for r in results if r["scenario"] == "expected" and r["status"] == "viable"
                and r["strategy"] != "subject_to"]
    best = None
    for strategy in STRATEGY_PRIORITY:
        candidates = [r for r in expected if r["strategy"] == strategy.value]
        if candidates and (best is None or max(r["profit"] for r in candidates) > best["profit"]):
            best = max(candidates, key=lambda r: r["profit"])
    if expected and best is None:
        best = max(expected, key=lambda r: r["profit"])

    v_exp = uw["value"]["v_expected"]
    equity_pct = uw["equity"].get("expected", {}).get("equity_pct") if uw["equity"] else None
    profit = best["profit"] if best else None
    roi = best["roi"] if best else None
    mos = best["margin_of_safety"] if best else None
    discount = q((v_exp - best["mao"]) / v_exp, Q6) if best and best["mao"] is not None and v_exp else Decimal("0")

    terms = {
        "fos_profit_norm": n_term(profit, Decimal("0"), Decimal("150000")),
        "fos_roi_norm": n_term(roi, Decimal("0"), Decimal("0.5")),
        "fos_equity_pct_norm": n_term(equity_pct, Decimal("0"), Decimal("0.6")),
        "fos_discount_to_value_norm": n_term(discount, Decimal("0"), Decimal("0.35")),
        "fos_margin_of_safety_norm": n_term(mos, Decimal("0"), Decimal("0.35")),
    }
    fos = q(Decimal("100") * (Decimal("0.30") * terms["fos_profit_norm"] + Decimal("0.25") * terms["fos_roi_norm"]
                              + Decimal("0.20") * terms["fos_equity_pct_norm"]
                              + Decimal("0.15") * terms["fos_discount_to_value_norm"]
                              + Decimal("0.10") * terms["fos_margin_of_safety_norm"]), Q4)
    note(fixture, "default", stage, "fos", "100*(.30 profit + .25 roi + .20 equity + .15 discount + .10 mos)",
         f"profit={profit} roi={roi} equity_pct={equity_pct} discount={discount} mos={mos}", fos)

    # distress (spec S10 point table)
    def item_decay(event_date: date | None) -> Decimal:
        if event_date is None or ref is None:
            return Decimal("0")
        return decay(Decimal(max(0, months_between(event_date, ref))), Decimal("18"))

    distress_items: dict[str, Decimal] = {}
    fc = record.foreclosure
    if fc and fc.is_active:
        if fc.nts_date:
            recent = ref is not None and 0 <= (ref - fc.nts_date).days <= 30
            base = Decimal("30") if recent else Decimal("24")
            distress_items["distress_nts"] = q(base * item_decay(fc.nts_date), Q4)
        if fc.nod_date:
            distress_items["distress_nod"] = q(Decimal("18") * item_decay(fc.nod_date), Q4)
        prior = min(Decimal("16"), Decimal("8") * Decimal(fc.rescission_count))
        if prior:
            distress_items["distress_prior_foreclosure"] = q(prior, Q4)
    active_bk = [b for b in record.bankruptcies if b.status == "active"]
    if active_bk:
        distress_items["distress_bankruptcy_active"] = q(sum(
            (Decimal("12") * item_decay(b.filing_date) for b in active_bk), Decimal("0")), Q4)
    prior_bk = [b for b in record.bankruptcies if b.status != "active"]
    if prior_bk:
        distress_items["distress_bankruptcy_prior"] = q(min(Decimal("18"), sum(
            (Decimal("6") * item_decay(b.filing_date) for b in prior_bk), Decimal("0"))), Q4)
    if any(b.sequence and b.sequence > 1 for b in record.bankruptcies):
        distress_items["distress_repeat_filings"] = Decimal("8")
    other_involuntary = Decimal("0")
    for lien in record.liens:
        if lien.status in ("released", "satisfied"):
            continue
        d = item_decay(lien.recording_date)
        if lien.lien_type in TAX_LIEN_TYPES:
            key = "distress_tax_lien_attached" if lien.attachment_basis == AttachmentBasis.RECORDED_AGAINST_PROPERTY else "distress_tax_lien_owner_only"
            distress_items[key] = q(distress_items.get(key, Decimal("0")) + (Decimal("10") if key == "distress_tax_lien_attached" else Decimal("4")) * d, Q4)
        else:
            other_involuntary += Decimal("3") * d
    if record.hoa.has_lien and (tracked(record.hoa.arrears) or 0) > 0:
        other_involuntary += Decimal("3")
    if other_involuntary:
        distress_items["distress_other_involuntary_liens"] = q(min(Decimal("12"), other_involuntary), Q4)
    if record.taxes.delinquent_years and record.taxes.delinquent_years >= 2:
        distress_items["distress_taxes_delinquent_2yr"] = Decimal("10")
    if record.ownership.is_absentee:
        distress_items["distress_absentee"] = Decimal("5")
    if record.ownership.years_owned and record.ownership.years_owned > 15:
        distress_items["distress_owned_over_15yr"] = Decimal("4")
    expired = [l for l in record.listings if l.status in ("expired", "cancelled")]
    if expired:
        distress_items["distress_listing_expired"] = q(min(Decimal("12"), sum(
            (Decimal("6") * item_decay(l.delist_date) for l in expired), Decimal("0"))), Q4)
    subtotal = sum(distress_items.values(), Decimal("0"))
    if equity_pct is not None and equity_pct >= Decimal("0.5") and subtotal > 0:
        distress_items["distress_high_equity_bonus"] = Decimal("5")
    distress = q(min(Decimal("100"), sum(distress_items.values(), Decimal("0"))), Q4)
    for key, value in distress_items.items():
        note(fixture, "default", stage, key, "spec S10 point table x recency decay", "", value)
    note(fixture, "default", stage, "distress", "sum, capped 100", "", distress)

    dcs_parts = dcs_score(fixture, record)
    dcs = dcs_parts["dcs"]

    active_liens = [l for l in record.liens if l.status not in ("released", "satisfied")]
    risk_terms = {
        "risk_liens": Decimal(6 * len(active_liens)),
        "risk_bankruptcy": Decimal("15") if active_bk else Decimal("0"),
        "risk_foreclosure_stage": Decimal("12") if fc and fc.is_active and fc.stage in ("nts", "auction") else Decimal("0"),
        "risk_owner_only_liens_over_10k": Decimal(10 * sum(
            1 for l in active_liens if l.attachment_basis == AttachmentBasis.OWNER_NAMED_ONLY
            and (tracked(l.amount) or Decimal("0")) > Decimal("10000"))),
        "risk_title_flags": Decimal(10 * sum(1 for f in record.open_flags if f.type.value in TITLE_FLAG_TYPES)),
        "risk_owner_occupied": Decimal("8") if record.ownership.is_owner_occupied else Decimal("0"),
        "risk_hoa_arrears": Decimal("8") if (tracked(record.hoa.arrears) or 0) > 0 else Decimal("0"),
        "risk_material_conflicts": Decimal(10 * record.data_quality.material_conflict_count),
        "risk_low_dcs": Decimal("12") if dcs < 50 else Decimal("0"),
        "risk_federal_tax_lien": Decimal("6") if any(l.lien_type == "federal_tax" for l in active_liens) else Decimal("0"),
    }
    risk = q(clamp(sum(risk_terms.values(), Decimal("0")), Decimal("0"), Decimal("100")), Q4)
    note(fixture, "default", stage, "risk", "spec S10 additive penalties", json.dumps({k: format(v, "f") for k, v in risk_terms.items()}), risk)

    overall = q(clamp(Decimal("0.5") * fos + Decimal("0.2") * distress + Decimal("0.2") * dcs
                      - Decimal("0.25") * risk, Decimal("0"), Decimal("100")), Q4)
    gates: list[str] = []
    is_rankable = True
    if uw["status"] != "ok":
        gates.append("insufficient_data")
        is_rankable = False
    if any(f.is_gating for f in record.open_flags):
        gates.append("open_gating_flag")
        is_rankable = False
    if dcs < 40:
        gates.append("dcs_below_40")
        overall = min(overall, Decimal("45"))
    if fc and fc.is_active and dcs < 75:
        gates.append("foreclosure_cap")
        overall = min(overall, Decimal("70"))
    overall = q(overall, Q4)
    note(fixture, "default", stage, "overall", "clamp(.5 FOS + .2 Distress + .2 DCS - .25 Risk, 0, 100) + gates",
         f"fos={fos} distress={distress} dcs={dcs} risk={risk} gates={gates}", overall)

    components = dict(terms)
    components.update(distress_items)
    components.update({
        "dcs_field_coverage": q(record.data_quality.critical_field_coverage, Q6),
        "dcs_corroboration": dcs_parts["corroboration"],
        "dcs_recency": dcs_parts["recency"],
        "dcs_conflict_free": q(1 - dcs_parts["conflict_penalty"], Q6),
        "dcs_verification": dcs_parts["verification"],
        "dcs_extraction_quality": record.data_quality.mean_extraction_confidence,
    })
    components.update(risk_terms)
    components.update({
        "profit": profit if profit is not None else Decimal("0"),
        "roi": roi if roi is not None else Decimal("0"),
        "equity_pct": equity_pct if equity_pct is not None else Decimal("0"),
        "discount_to_value": discount,
        "margin_of_safety": mos if mos is not None else Decimal("0"),
        "mao_best": best["mao"] if best and best["mao"] is not None else Decimal("0"),
    })

    alternatives = [r["strategy"] for r in sorted(expected, key=lambda r: r["profit"], reverse=True)
                    if best is None or r["strategy"] != best["strategy"]]
    return {"property_id": uw["property_id"], "scoring_config_id": SCORING_CONFIG_ID,
            "fos": fos, "distress": distress, "data_confidence": dcs, "risk": risk, "overall": overall,
            "components": components, "gates_applied": gates, "is_rankable": is_rankable,
            "recommended_strategy": best["strategy"] if best else None,
            "recommended_alternatives": alternatives}


# ------------------------------------------------------------------------ main

def main() -> None:
    assumptions = {p.stem: AssumptionSet.model_validate(json.loads(p.read_text()))
                   for p in sorted((FIXTURES / "assumptions").glob("*.json"))}
    normalized = sorted((FIXTURES / "normalized").glob("*.json"))
    for path in normalized:
        slug = path.stem
        record = NormalizedProperty.model_validate(json.loads(path.read_text()))
        uw_by_assumption = {}
        for a_name, a in assumptions.items():
            uw = underwrite(slug, record, a)
            uw_by_assumption[a_name] = uw
            out = FIXTURES / "underwriting" / f"{slug}.{a_name}.json"
            out.write_text(json.dumps(uw, indent=2, default=_json_default) + "\n")
        default_uw = uw_by_assumption["default"]
        strat = strategies(slug, record, default_uw, assumptions["default"])
        (FIXTURES / "strategies" / f"{slug}.json").write_text(
            json.dumps(strat, indent=2, default=_json_default) + "\n")
        sc = score(slug, record, default_uw, strat)
        (FIXTURES / "scores" / f"{slug}.json").write_text(
            json.dumps(sc, indent=2, default=_json_default) + "\n")
        print("generated", slug)
    for directory, stages in (("underwriting", {"underwriting"}), ("strategies", {"strategies"}),
                              ("scores", {"scoring"})):
        rows = [r for r in ROWS if r["stage"] in stages]
        with (FIXTURES / directory / "worksheet.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["fixture", "assumption", "stage", "step", "formula", "inputs", "value"])
            writer.writeheader()
            writer.writerows(rows)
        print("worksheet", directory, len(rows), "rows")


def _json_default(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date,)):
        return value.isoformat()
    raise TypeError(f"not serializable: {type(value)}")


if __name__ == "__main__":
    main()
