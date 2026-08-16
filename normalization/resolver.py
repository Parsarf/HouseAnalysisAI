"""WP-5 fact resolution: collapse the ExtractedFactDraft ledger into one NormalizedProperty.

Pure and deterministic (spec §6): the same fact list always produces a
byte-identical record. Scoring follows §6.2, conflicts §6.3, entity dedupe
§6.4, derived mortgage balances §6.5, and data_quality feeds WP-8 directly.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from common.dates import months_between
from common.money import money
from contracts import (AddressBlock, AttachmentBasis, BankruptcyRecord,
                       ConditionSignal, DataQualityBlock, EntityType, ExtractedFactDraft,
                       FlagSummary, FlagType, ForeclosureState, HoaBlock, LienRecord,
                       ListingRecord, MortgageRecord, NormalizedProperty, OwnershipBlock,
                       PropertyAttributes, RentalBlock, SourceKind, TaxBlock, TrackedValue,
                       ValuationCandidate)
from contracts.models import ComparableSale  # not yet re-exported by contracts/__init__

RESOLVER_VERSION = "resolver-2"

CONDITION_LADDER = ("pristine", "cosmetic", "moderate", "heavy", "gut")

_MATERIAL_THRESHOLD = Decimal("10000")
_MORTGAGE_BALANCE_TOLERANCE_PCT = Decimal("0.05")
_MORTGAGE_BALANCE_TOLERANCE_USD = Decimal("10000")
_VALUATION_DISPERSION_RATIO = Decimal("1.5")
_BID_MISMATCH_PCT = Decimal("0.20")
_LOW_CONFIDENCE_THRESHOLD = 0.65
_FOUR_PLACES = Decimal("0.0001")

# Money fields decay with a 180-day half-life; structural facts with 3 years (§6.2).
_MONEY_LEAVES = frozenset({"amount", "original_amount", "balance", "value", "value_low", "value_high",
                           "price", "purchase_price", "rent_estimate", "annual_taxes", "assessed_value",
                           "delinquent_amount", "arrears", "monthly_dues", "published_bid", "default_amount"})
# Conservative tie-breaking: highest liability, lowest asset value (§6.2).
_LIABILITY_LEAVES = frozenset({"amount", "original_amount", "balance", "delinquent_amount", "arrears",
                               "monthly_dues", "published_bid", "default_amount"})
_ASSET_LEAVES = frozenset({"value", "price", "rent_estimate", "purchase_price"})

# Leaf-name aliases: extraction prompts and fixtures may use any of these.
_PROPERTY_ALIASES = {"address": "line1", "street": "line1", "zip": "zip5", "zipcode": "zip5",
                     "zip_code": "zip5", "type": "property_type", "owner": "owner_name",
                     "owners": "owner_name", "owner_occupied": "is_owner_occupied",
                     "absentee": "is_absentee", "purchase_date": "ownership_start_date",
                     "last_sale_date": "ownership_start_date", "last_sale_price": "purchase_price",
                     "hoa_dues": "monthly_dues", "hoa_monthly_dues": "monthly_dues",
                     "hoa_arrears": "arrears", "hoa_lien": "has_lien", "hoa_has_lien": "has_lien"}
_MORTGAGE_ALIASES = {"amount": "original_amount", "original": "original_amount",
                     "original_balance": "original_amount", "loan_amount": "original_amount",
                     "current_balance": "balance", "estimated_balance": "balance",
                     "interest_rate": "rate", "term": "term_months", "loan_term_months": "term_months",
                     "date": "origination_date", "loan_date": "origination_date",
                     "doc": "recording_doc_number", "doc_number": "recording_doc_number",
                     "document_number": "recording_doc_number", "lender_name": "lender", "open": "is_open"}
_LIEN_ALIASES = {"type": "lien_type", "creditor_name": "creditor", "doc_number": "recording_doc_number",
                 "document_number": "recording_doc_number", "date": "recording_date",
                 "basis": "attachment_basis", "priority_position": "priority"}
_VALUATION_ALIASES = {"type": "valuation_type", "amount": "value", "estimate": "value",
                      "low": "value_low", "high": "value_high", "confidence": "reported_confidence",
                      "date": "as_of", "weight": "weight_hint"}
_FORECLOSURE_ALIASES = {"sale_date": "current_sale_date", "bid": "published_bid",
                        "trustee_name": "trustee", "date": "event_date", "type": "event_type",
                        "active": "is_active"}
_BANKRUPTCY_ALIASES = {"date": "filing_date", "filing_sequence": "sequence"}
_TAX_ALIASES = {"taxes": "annual_taxes", "annual_tax": "annual_taxes", "assessed": "assessed_value",
                "delinquent": "delinquent_amount"}
_RENTAL_ALIASES = {"rent": "rent_estimate", "monthly_rent": "rent_estimate"}
_LISTING_ALIASES = {"date": "list_date", "list_price": "price", "days_on_market": "dom"}
_COMP_ALIASES = {"sale_price": "price", "distance_miles": "distance", "similarity_score": "similarity"}
_CONDITION_ALIASES = {"ladder": "condition", "level": "condition"}

# §6.4 lender alias table (small by design; extend as vendors appear).
_LENDER_ALIASES = {"WELLS FARGO BANK NA": "WELLS FARGO", "WELLS FARGO HOME MTG": "WELLS FARGO",
                   "WELLS FARGO BANK N A": "WELLS FARGO", "BANK OF AMERICA NA": "BANK OF AMERICA",
                   "BANK OF AMERICA N A": "BANK OF AMERICA", "JPMORGAN CHASE BANK NA": "JPMORGAN CHASE",
                   "CHASE HOME FINANCE": "JPMORGAN CHASE", "NATIONSTAR MTG": "NATIONSTAR",
                   "NATIONSTAR MORTGAGE": "NATIONSTAR"}

# §6.5 fallback when a mortgage has no rate: 30-year fixed yearly averages.
_HISTORICAL_RATE_INDEX = {2000: Decimal(".081"), 2001: Decimal(".070"), 2002: Decimal(".065"),
                          2003: Decimal(".058"), 2004: Decimal(".058"), 2005: Decimal(".059"),
                          2006: Decimal(".064"), 2007: Decimal(".063"), 2008: Decimal(".060"),
                          2009: Decimal(".050"), 2010: Decimal(".047"), 2011: Decimal(".045"),
                          2012: Decimal(".037"), 2013: Decimal(".040"), 2014: Decimal(".042"),
                          2015: Decimal(".039"), 2016: Decimal(".036"), 2017: Decimal(".040"),
                          2018: Decimal(".045"), 2019: Decimal(".039"), 2020: Decimal(".031"),
                          2021: Decimal(".030"), 2022: Decimal(".053"), 2023: Decimal(".068"),
                          2024: Decimal(".067"), 2025: Decimal(".066"), 2026: Decimal(".063")}
_DEFAULT_HISTORICAL_RATE = Decimal(".065")
_DEFAULT_TERM_MONTHS = 360

# §7.2 valuation weights, used as hints when extraction provides none.
_VALUATION_WEIGHT_HINTS = {"avm": Decimal("0.30"), "comp": Decimal("0.35"), "comps": Decimal("0.35"),
                           "listing": Decimal("0.15"), "list_price": Decimal("0.10"),
                           "assessed": Decimal("0.10")}

# The 22 critical fields from §10 (DCS coverage) — WP-8 reads these counts, it never recomputes them.
CRITICAL_FIELDS = ("apn", "address", "sqft", "beds", "baths", "year_built", "owner", "occupancy",
                   "last_sale_date", "last_sale_price", "avm", "comp_estimate", "assessed_value",
                   "annual_taxes", "mortgage_1_original", "mortgage_1_date", "mortgage_1_rate",
                   "mortgage_1_balance", "foreclosure_status", "lien_count", "lien_total", "rent")


def normalize_source_kind(source_kind: SourceKind, ocr_applied: bool = False) -> float:
    cap = {SourceKind.HUMAN: 1.0, SourceKind.API: .9, SourceKind.REPORT: .7, SourceKind.DERIVED: .5, SourceKind.PASTED: .45}[source_kind]
    return min(cap, .8) if ocr_applied else cap


class _Context:
    """Per-run accumulator: flags, conflict counts, and critical-field bookkeeping."""

    def __init__(self, as_of: date, ocr_applied: bool):
        self.as_of = as_of
        self.ocr_applied = ocr_applied
        self.flags: list[FlagSummary] = []
        self.conflict_count = 0
        self.material_conflict_count = 0
        self.winners: dict[str, ExtractedFactDraft | None] = {}
        self.contributions: dict[str, set] = defaultdict(set)

    def register(self, field: str, fact: ExtractedFactDraft | None) -> None:
        self.winners.setdefault(field, fact)
        if fact is not None:
            self.contributions[field].add(fact.report_id)

    def flag(self, flag_type: FlagType, impact: Decimal | None = None, gating: bool = False,
             severity: str = "warning") -> None:
        self.flags.append(FlagSummary(type=flag_type, severity=severity, is_gating=gating,
                                      financial_impact=impact))

    def conflict(self, impact: Decimal | None = None, flag_type: FlagType | None = None,
                 gating: bool = False) -> None:
        self.conflict_count += 1
        if impact is not None and money(impact) >= _MATERIAL_THRESHOLD:
            self.material_conflict_count += 1
        if flag_type is not None:
            self.flag(flag_type, impact, gating)


# ---------------------------------------------------------------- fact accessors

def _leaf(field_path: str) -> str:
    leaf = field_path.rsplit(".", 1)[-1]
    return leaf.split("[", 1)[0]


def _has_value(fact: ExtractedFactDraft) -> bool:
    return any(v is not None for v in (fact.value_parsed, fact.value_text, fact.value_date, fact.value_bool))


def _num(fact: ExtractedFactDraft | None) -> Decimal | None:
    if fact is None:
        return None
    if fact.value_parsed is not None:
        return fact.value_parsed
    if fact.value_text:
        try:
            return Decimal(fact.value_text.replace(",", "").replace("$", "").strip())
        except InvalidOperation:
            return None
    return None


def _text(fact: ExtractedFactDraft | None) -> str | None:
    if fact is None:
        return None
    if fact.value_text and fact.value_text.strip():
        return fact.value_text.strip()
    if fact.value_parsed is not None:
        return str(fact.value_parsed)
    return None


def _int(fact: ExtractedFactDraft | None) -> int | None:
    value = _num(fact)
    return int(value) if value is not None else None


def _date_of(fact: ExtractedFactDraft | None) -> date | None:
    return fact.value_date if fact is not None else None


def _bool_of(fact: ExtractedFactDraft | None) -> bool | None:
    if fact is None:
        return None
    if fact.value_bool is not None:
        return fact.value_bool
    text = (fact.value_text or "").strip().casefold()
    if text in ("true", "yes", "y", "1"):
        return True
    if text in ("false", "no", "n", "0"):
        return False
    return None


def _rate_of(fact: ExtractedFactDraft | None) -> Decimal | None:
    rate = _num(fact)
    if rate is not None and rate > 1:  # stated as a percent, e.g. 6.5
        rate = rate / 100
    return rate


def _tracked(fact: ExtractedFactDraft, *, is_estimated: bool | None = None,
             confidence: float | None = None, value: Decimal | None = None,
             source_kind: SourceKind | None = None, as_of: date | None = None) -> TrackedValue:
    estimated = (fact.source_kind == SourceKind.DERIVED) if is_estimated is None else is_estimated
    return TrackedValue(value=value if value is not None else fact.value_parsed,
                        confidence=confidence if confidence is not None else fact.extraction_confidence,
                        source_kind=source_kind or fact.source_kind, is_estimated=estimated,
                        as_of=as_of if as_of is not None else fact.as_of_date)


# ---------------------------------------------------------------- precedence (§6.2)

def _specificity(fact: ExtractedFactDraft) -> float:
    """Recorded exact figure > stated range > rounded estimate."""
    if fact.value_parsed is None:
        return 1.0
    raw = (fact.value_raw or "").lower()
    if " to " in raw or raw.count("-") == 1:
        return 0.6
    magnitude = abs(fact.value_parsed)
    if magnitude >= 10000 and magnitude % 1000 == 0:
        return 0.3
    return 1.0


def _recency(fact: ExtractedFactDraft, as_of: date, is_money: bool) -> float:
    if fact.as_of_date is None:
        return 0.5
    half_life = 180.0 if is_money else 1095.0
    return 0.5 ** (max(0, (as_of - fact.as_of_date).days) / half_life)


def _agrees(a: ExtractedFactDraft, b: ExtractedFactDraft, is_money: bool) -> bool:
    if a.report_id == b.report_id:
        return False
    if is_money:
        va, vb = _num(a), _num(b)
        if va is None or vb is None:
            return False
        return abs(va - vb) <= Decimal("0.02") * max(abs(va), abs(vb))
    if a.value_date is not None or b.value_date is not None:
        return a.value_date is not None and a.value_date == b.value_date
    if a.value_bool is not None or b.value_bool is not None:
        return a.value_bool is not None and a.value_bool == b.value_bool
    ta, tb = (a.value_text or "").strip().casefold(), (b.value_text or "").strip().casefold()
    return bool(ta) and ta == tb


def _score(fact: ExtractedFactDraft, peers: list[ExtractedFactDraft], as_of: date,
           is_money: bool, ocr_applied: bool) -> float:
    rank = normalize_source_kind(fact.source_kind) * (0.85 if ocr_applied else 1.0)
    agreeing = sum(1 for peer in peers if peer is not fact and _agrees(fact, peer, is_money))
    corroboration = min(1.0, agreeing / 3.0)
    return (0.40 * rank + 0.30 * _recency(fact, as_of, is_money)
            + 0.20 * _specificity(fact) + 0.10 * corroboration)


def _pick_winner(group: list[ExtractedFactDraft], leaf: str,
                 ctx: _Context) -> tuple[ExtractedFactDraft | None, list[ExtractedFactDraft]]:
    candidates = [fact for fact in group if _has_value(fact)]
    if not candidates:
        return None, []
    human = [fact for fact in candidates if fact.source_kind == SourceKind.HUMAN]
    if human:  # a human override always wins, regardless of recency
        candidates = human
    is_money = leaf in _MONEY_LEAVES

    def sort_key(fact: ExtractedFactDraft):
        score = round(_score(fact, candidates, ctx.as_of, is_money, ctx.ocr_applied), 9)
        value = _num(fact)
        magnitude = float(value) if value is not None else 0.0
        tie = -magnitude if leaf in _LIABILITY_LEAVES else (magnitude if leaf in _ASSET_LEAVES else 0.0)
        return (-score, tie, fact.snippet, str(fact.report_id), fact.field_path)

    ordered = sorted(candidates, key=sort_key)
    return ordered[0], ordered[1:]


def _resolve_leaves(facts: list[ExtractedFactDraft], aliases: dict[str, str],
                    ctx: _Context) -> dict[str, tuple[ExtractedFactDraft | None, list[ExtractedFactDraft]]]:
    groups: dict[str, list[ExtractedFactDraft]] = defaultdict(list)
    for fact in facts:
        leaf = _leaf(fact.field_path)
        groups[aliases.get(leaf, leaf)].append(fact)
    return {leaf: _pick_winner(group, leaf, ctx) for leaf, group in groups.items()}


def _winner(resolved: dict, leaf: str) -> ExtractedFactDraft | None:
    return resolved.get(leaf, (None, []))[0]


# ---------------------------------------------------------------- entity dedupe (§6.4)

def _group_by_local_id(facts: list[ExtractedFactDraft]) -> list[list[ExtractedFactDraft]]:
    groups: dict[str, list[ExtractedFactDraft]] = defaultdict(list)
    for fact in facts:
        groups[fact.entity_local_id].append(fact)
    return [groups[key] for key in sorted(groups)]


def _normalized_name(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = "".join(ch if ch.isalnum() else " " for ch in name.upper())
    return " ".join(cleaned.split()) or None


def _canonical_lender(name: str | None) -> str | None:
    normalized = _normalized_name(name)
    return _LENDER_ALIASES.get(normalized, normalized) if normalized else None


def _merge_groups(groups: list[list[ExtractedFactDraft]], same) -> list[list[ExtractedFactDraft]]:
    parents = list(range(len(groups)))

    def find(i: int) -> int:
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            if find(i) != find(j) and same(groups[i], groups[j]):
                parents[find(j)] = find(i)
    merged: dict[int, list[ExtractedFactDraft]] = defaultdict(list)
    for i, group in enumerate(groups):
        merged[find(i)].extend(group)
    ordered = sorted(merged.values(), key=lambda fs: min(f.entity_local_id for f in fs))
    return ordered


def _same_mortgage(a_facts: list[ExtractedFactDraft], b_facts: list[ExtractedFactDraft],
                   ctx: _Context) -> bool:
    a, b = _resolve_leaves(a_facts, _MORTGAGE_ALIASES, ctx), _resolve_leaves(b_facts, _MORTGAGE_ALIASES, ctx)
    doc_a, doc_b = _text(_winner(a, "recording_doc_number")), _text(_winner(b, "recording_doc_number"))
    if doc_a and doc_b:
        return doc_a.casefold() == doc_b.casefold()
    lender_a = _canonical_lender(_text(_winner(a, "lender")))
    lender_b = _canonical_lender(_text(_winner(b, "lender")))
    if not lender_a or lender_a != lender_b:
        return False
    date_a, date_b = _date_of(_winner(a, "origination_date")), _date_of(_winner(b, "origination_date"))
    if date_a and date_b and abs((date_a - date_b).days) > 30:
        return False
    amount_a, amount_b = _num(_winner(a, "original_amount")), _num(_winner(b, "original_amount"))
    if amount_a is not None and amount_b is not None:
        if abs(amount_a - amount_b) > Decimal("0.01") * max(abs(amount_a), abs(amount_b)):
            return False
    return True


def _same_lien(a_facts: list[ExtractedFactDraft], b_facts: list[ExtractedFactDraft],
               ctx: _Context) -> bool:
    a, b = _resolve_leaves(a_facts, _LIEN_ALIASES, ctx), _resolve_leaves(b_facts, _LIEN_ALIASES, ctx)
    doc_a, doc_b = _text(_winner(a, "recording_doc_number")), _text(_winner(b, "recording_doc_number"))
    if doc_a and doc_b:
        return doc_a.casefold() == doc_b.casefold()
    type_a = (_text(_winner(a, "lien_type")) or "").casefold()
    type_b = (_text(_winner(b, "lien_type")) or "").casefold()
    creditor_a = _normalized_name(_text(_winner(a, "creditor")))
    creditor_b = _normalized_name(_text(_winner(b, "creditor")))
    if not type_a or type_a != type_b or not creditor_a or creditor_a != creditor_b:
        return False
    amount_a, amount_b = _num(_winner(a, "amount")), _num(_winner(b, "amount"))
    if amount_a is not None and amount_b is not None and abs(amount_a - amount_b) > 1:
        return False
    date_a, date_b = _date_of(_winner(a, "recording_date")), _date_of(_winner(b, "recording_date"))
    if date_a and date_b and abs((date_a - date_b).days) > 7:
        return False
    return True


# ---------------------------------------------------------------- derived balances (§6.5)

def _amortized_balance(original: Decimal, rate: Decimal, term_months: int,
                       origination_date: date, as_of: date) -> Decimal | None:
    n = months_between(origination_date, as_of)
    if n <= 0:
        return money(original)
    if n >= term_months:
        return Decimal("0")
    if rate == 0:
        return money(original * (term_months - n) / term_months)
    r = rate / 12
    growth = (1 + r) ** term_months
    return money(original * (growth - (1 + r) ** n) / (growth - 1))


def _estimate_balance(original: Decimal, rate: Decimal, term_months: int,
                      origination_date: date, as_of: date) -> Decimal | None:
    """Prefer WP-6's finance.estimate_balance when it exists; fall back to the §6.5 formula."""
    estimate = None
    try:
        from finance import estimate_balance  # type: ignore[attr-defined]
    except Exception:
        estimate_balance = None
    if callable(estimate_balance):
        try:
            estimate = estimate_balance(original, rate, term_months, origination_date, as_of)
        except Exception:
            estimate = None
    if estimate is not None:
        return money(estimate)
    return _amortized_balance(original, rate, term_months, origination_date, as_of)


def _derive_balance(original: TrackedValue, rate: Decimal | None, term_months: int | None,
                    origination_date: date, ctx: _Context) -> TrackedValue | None:
    rate_estimated = rate is None
    if rate_estimated:
        rate = _HISTORICAL_RATE_INDEX.get(origination_date.year, _DEFAULT_HISTORICAL_RATE)
    value = _estimate_balance(original.value, rate, term_months or _DEFAULT_TERM_MONTHS,
                              origination_date, ctx.as_of)
    if value is None:
        return None
    confidence = 0.55 if rate_estimated else min(original.confidence, 0.8)
    return TrackedValue(value=value, confidence=confidence, source_kind=SourceKind.DERIVED,
                        is_estimated=True, as_of=ctx.as_of)


# ---------------------------------------------------------------- entity builders

def _norm_position(text: str | None) -> str:
    lowered = (text or "").strip().casefold()
    words = {"first": "1", "1st": "1", "second": "2", "2nd": "2", "third": "3", "3rd": "3"}
    if lowered in words:
        return words[lowered]
    digits = "".join(ch for ch in lowered if ch.isdigit())
    return digits or (text.strip() if text and text.strip() else "1")


def _position_key(record: MortgageRecord):
    return (0, int(record.position)) if record.position.isdigit() else (1, record.position)


def _money_conflict(winner: ExtractedFactDraft, contenders: list[ExtractedFactDraft],
                    tolerance: Decimal, ctx: _Context,
                    flag_type: FlagType | None = None) -> None:
    values = sorted({value for value in (_num(f) for f in [winner, *contenders]) if value is not None})
    if len(values) < 2:
        return
    spread = values[-1] - values[0]
    if spread > tolerance:
        ctx.conflict(impact=spread, flag_type=flag_type)


def _build_mortgage(facts: list[ExtractedFactDraft],
                    ctx: _Context) -> tuple[MortgageRecord, dict[str, ExtractedFactDraft | None]]:
    resolved = _resolve_leaves(facts, _MORTGAGE_ALIASES, ctx)
    winners = {"original": _winner(resolved, "original_amount"), "date": _winner(resolved, "origination_date"),
               "rate": _winner(resolved, "rate"), "balance": _winner(resolved, "balance")}
    original = _tracked(winners["original"]) if winners["original"] else None
    is_open = _bool_of(_winner(resolved, "is_open"))
    if is_open is None:
        status = (_text(_winner(resolved, "status")) or "").casefold()
        is_open = status not in ("paid off", "paid_off", "closed", "satisfied", "released")
    balance, method = None, "reported"
    if winners["balance"] is not None:
        balance_facts = [winners["balance"], *resolved["balance"][1]]
        amounts = [value for value in (_num(f) for f in balance_facts) if value is not None]
        tolerance = max([_MORTGAGE_BALANCE_TOLERANCE_USD,
                         *(_MORTGAGE_BALANCE_TOLERANCE_PCT * abs(value) for value in amounts)])
        _money_conflict(winners["balance"], resolved["balance"][1], tolerance, ctx,
                        FlagType.CONFLICTING_MORTGAGE)
        balance = _tracked(winners["balance"])
    elif original is not None and original.value is not None and _date_of(winners["date"]) is not None:
        balance = _derive_balance(original, _rate_of(winners["rate"]), _int(_winner(resolved, "term_months")),
                                  _date_of(winners["date"]), ctx)
        if balance is not None:
            method = "amortization_v1"
            if balance.value == 0:
                is_open = False
    record = MortgageRecord(position=_norm_position(_text(_winner(resolved, "position"))),
                            lender=_text(_winner(resolved, "lender")), original_amount=original,
                            rate=_rate_of(winners["rate"]), term_months=_int(_winner(resolved, "term_months")),
                            origination_date=_date_of(winners["date"]), estimated_balance=balance,
                            balance_method=method, is_open=is_open)
    return record, winners


def _build_lien(facts: list[ExtractedFactDraft],
                ctx: _Context) -> tuple[LienRecord, dict[str, ExtractedFactDraft | None]]:
    resolved = _resolve_leaves(facts, _LIEN_ALIASES, ctx)
    winners = {"type": _winner(resolved, "lien_type"), "amount": _winner(resolved, "amount")}
    if winners["amount"] is not None:  # doc-matched liens with genuinely different amounts (§6.3)
        _money_conflict(winners["amount"], resolved["amount"][1], Decimal("1"), ctx)
    status = (_text(_winner(resolved, "status")) or "unknown").casefold()
    basis_text = (_text(_winner(resolved, "attachment_basis")) or "").casefold()
    try:
        basis = AttachmentBasis(basis_text)
    except ValueError:
        basis = AttachmentBasis.UNKNOWN
    confidence = _num(_winner(resolved, "attachment_confidence"))
    if confidence is None:
        confidence = {AttachmentBasis.RECORDED_AGAINST_PROPERTY: Decimal("0.9"),
                      AttachmentBasis.OWNER_NAMED_ONLY: Decimal("0.6"),
                      AttachmentBasis.UNKNOWN: Decimal("0.5")}[basis]
    amount = _tracked(winners["amount"]) if winners["amount"] else None
    if amount is None and status not in ("released", "satisfied"):
        ctx.flag(FlagType.MISSING_LIEN_AMOUNT)
    record = LienRecord(lien_type=(_text(winners["type"]) or "other").casefold(), amount=amount,
                        amount_is_estimated=bool(amount and amount.is_estimated), status=status,
                        attachment_basis=basis, attachment_confidence=float(confidence),
                        recording_date=_date_of(_winner(resolved, "recording_date")),
                        priority=_int(_winner(resolved, "priority")))
    return record, winners


def _lien_key(record: LienRecord):
    return (record.recording_date or date.min, record.lien_type,
            record.amount.value if record.amount and record.amount.value is not None else Decimal("0"))


def _build_valuation(facts: list[ExtractedFactDraft],
                     ctx: _Context) -> tuple[ValuationCandidate | None, ExtractedFactDraft | None]:
    resolved = _resolve_leaves(facts, _VALUATION_ALIASES, ctx)
    value_fact = _winner(resolved, "value")
    if value_fact is None or _num(value_fact) is None:
        return None, None
    valuation_type = (_text(_winner(resolved, "valuation_type")) or "unknown").casefold()
    hint = _num(_winner(resolved, "weight_hint"))
    if hint is None:
        hint = _VALUATION_WEIGHT_HINTS.get(valuation_type)
    reported = _num(_winner(resolved, "reported_confidence"))
    candidate = ValuationCandidate(valuation_type=valuation_type, value=_tracked(value_fact),
                                   value_low=_num(_winner(resolved, "value_low")),
                                   value_high=_num(_winner(resolved, "value_high")),
                                   as_of=_date_of(_winner(resolved, "as_of")) or value_fact.as_of_date,
                                   reported_confidence=float(reported) if reported is not None else None,
                                   weight_hint=hint)
    return candidate, value_fact


def _build_foreclosure(facts: list[ExtractedFactDraft],
                       ctx: _Context) -> tuple[ForeclosureState, ExtractedFactDraft | None]:
    resolved = _resolve_leaves(facts, _FORECLOSURE_ALIASES, ctx)
    # Stage follows the most recent event group (postponements never rewrite history).
    stage_choices = []
    for local_id in sorted({fact.entity_local_id for fact in facts}):
        group = [fact for fact in facts if fact.entity_local_id == local_id]
        stage_fact = next((fact for fact in group
                           if _FORECLOSURE_ALIASES.get(_leaf(fact.field_path), _leaf(fact.field_path)) == "stage"
                           and fact.value_text), None)
        if stage_fact is not None:
            latest = max((fact.value_date for fact in group if fact.value_date), default=date.min)
            stage_choices.append((latest, local_id, stage_fact.value_text.strip(), stage_fact))
    if stage_choices:
        stage, stage_fact = max(stage_choices)[2], max(stage_choices)[3]
    else:
        stage, stage_fact = None, None
    sale_dates = sorted({fact.value_date for fact in facts
                         if fact.value_date is not None
                         and _FORECLOSURE_ALIASES.get(_leaf(fact.field_path), _leaf(fact.field_path))
                         in ("current_sale_date", "original_sale_date")})
    postponements = [fact for fact in facts
                     if "postpon" in (fact.value_text or "").casefold()
                     and _FORECLOSURE_ALIASES.get(_leaf(fact.field_path), _leaf(fact.field_path)) == "event_type"]
    if stage is None:  # no explicit stage facts: derive from the latest event type
        events = [fact for fact in facts
                  if _FORECLOSURE_ALIASES.get(_leaf(fact.field_path), _leaf(fact.field_path)) == "event_type"
                  and fact.value_text]
        latest = max(events, key=lambda f: (f.value_date or date.min, f.snippet), default=None)
        stage = (latest.value_text.strip().casefold() if latest else "unknown")
    if stage_fact is None:
        stage_fact = _winner(resolved, "stage") or (facts[0] if facts else None)
    is_active = _bool_of(_winner(resolved, "is_active"))
    if is_active is None:
        is_active = stage.casefold() in ("nod", "nts", "auction", "postponed", "scheduled", "active")
    explicit_count = _int(_winner(resolved, "postponement_count"))
    postponement_count = explicit_count if explicit_count is not None else len(
        {(f.entity_local_id, f.value_date) for f in postponements})
    state = ForeclosureState(stage=stage.casefold(), nod_date=_date_of(_winner(resolved, "nod_date")),
                             nts_date=_date_of(_winner(resolved, "nts_date")),
                             original_sale_date=_date_of(_winner(resolved, "original_sale_date"))
                             or (sale_dates[0] if sale_dates else None),
                             current_sale_date=sale_dates[-1] if sale_dates else None,
                             published_bid=_tracked(_winner(resolved, "published_bid"))
                             if _winner(resolved, "published_bid") else None,
                             default_amount=_tracked(_winner(resolved, "default_amount"))
                             if _winner(resolved, "default_amount") else None,
                             postponement_count=postponement_count,
                             rescission_count=_int(_winner(resolved, "rescission_count")) or 0,
                             trustee=_text(_winner(resolved, "trustee")), is_active=is_active)
    return state, stage_fact


def _build_bankruptcy(facts: list[ExtractedFactDraft], ctx: _Context) -> BankruptcyRecord | None:
    resolved = _resolve_leaves(facts, _BANKRUPTCY_ALIASES, ctx)
    chapter = _text(_winner(resolved, "chapter"))
    if chapter is None:
        return None
    return BankruptcyRecord(chapter=chapter.replace("chapter", "").strip(),
                            status=(_text(_winner(resolved, "status")) or "unknown").casefold(),
                            filing_date=_date_of(_winner(resolved, "filing_date")),
                            discharge_date=_date_of(_winner(resolved, "discharge_date")),
                            sequence=_int(_winner(resolved, "sequence")))


def _build_listing(facts: list[ExtractedFactDraft], ctx: _Context) -> ListingRecord | None:
    resolved = _resolve_leaves(facts, _LISTING_ALIASES, ctx)
    list_date = _date_of(_winner(resolved, "list_date"))
    if list_date is None:
        return None
    price_fact = _winner(resolved, "price")
    return ListingRecord(list_date=list_date, delist_date=_date_of(_winner(resolved, "delist_date")),
                         price=_tracked(price_fact) if price_fact else None,
                         status=(_text(_winner(resolved, "status")) or "unknown").casefold(),
                         dom=_int(_winner(resolved, "dom")))


def _build_comp(facts: list[ExtractedFactDraft], ctx: _Context) -> ComparableSale | None:
    resolved = _resolve_leaves(facts, _COMP_ALIASES, ctx)
    address = _text(_winner(resolved, "address"))
    if address is None:
        return None
    price_fact = _winner(resolved, "price")
    return ComparableSale(address=address, sale_date=_date_of(_winner(resolved, "sale_date")),
                          price=_tracked(price_fact) if price_fact else None,
                          sqft=_num(_winner(resolved, "sqft")), distance=_num(_winner(resolved, "distance")),
                          similarity=_num(_winner(resolved, "similarity")),
                          included=_bool_of(_winner(resolved, "included")) if _bool_of(_winner(resolved, "included")) is not None else True)


def _build_property_core(facts: list[ExtractedFactDraft], ctx: _Context):
    resolved = _resolve_leaves(facts, _PROPERTY_ALIASES, ctx)
    apn_fact, apn_contenders = resolved.get("apn", (None, []))
    line1_fact, line1_contenders = resolved.get("line1", (None, []))
    distinct_apns = {(_text(f) or "").casefold() for f in [apn_fact, *apn_contenders] if f and _text(f)}
    distinct_lines = {(_text(f) or "").casefold() for f in [line1_fact, *line1_contenders] if f and _text(f)}
    if len(distinct_apns) > 1 or len(distinct_lines) > 1:  # §6.3: APN/address conflicts are gating
        ctx.conflict(flag_type=FlagType.IDENTITY_CONFLICT, gating=True)
    ctx.register("apn", apn_fact)
    ctx.register("address", line1_fact)

    def tracked(leaf: str) -> TrackedValue | None:
        fact = _winner(resolved, leaf)
        return _tracked(fact) if fact else None

    address = AddressBlock(line1=_text(line1_fact), unit=_text(_winner(resolved, "unit")),
                           city=_text(_winner(resolved, "city")), state=_text(_winner(resolved, "state")),
                           zip5=_text(_winner(resolved, "zip5")), county=_text(_winner(resolved, "county")),
                           fips=_text(_winner(resolved, "fips")), lat=_num(_winner(resolved, "lat")),
                           lng=_num(_winner(resolved, "lng")))
    attributes = PropertyAttributes(beds=tracked("beds"), baths=tracked("baths"), sqft=tracked("sqft"),
                                    lot_sqft=tracked("lot_sqft"), year_built=tracked("year_built"),
                                    units=tracked("units"))
    for leaf, field in (("sqft", "sqft"), ("beds", "beds"), ("baths", "baths"),
                        ("year_built", "year_built"), ("is_owner_occupied", "occupancy"),
                        ("ownership_start_date", "last_sale_date"), ("purchase_price", "last_sale_price")):
        ctx.register(field, _winner(resolved, leaf))
    owner_winner, owner_contenders = resolved.get("owner_name", (None, []))
    owner_facts = [f for f in [owner_winner, *owner_contenders] if f is not None]
    owner_names = sorted({name for name in (_text(f) for f in owner_facts) if name})
    if owner_facts:
        ctx.register("owner", owner_facts[0])
    ownership = OwnershipBlock(owner_names=owner_names, entity_type=_text(_winner(resolved, "entity_type")),
                               is_owner_occupied=_bool_of(_winner(resolved, "is_owner_occupied")),
                               is_absentee=_bool_of(_winner(resolved, "is_absentee")),
                               ownership_start_date=_date_of(_winner(resolved, "ownership_start_date")),
                               purchase_price=tracked("purchase_price"))
    hoa = HoaBlock(monthly_dues=tracked("monthly_dues"), arrears=tracked("arrears"),
                   has_lien=_bool_of(_winner(resolved, "has_lien")) or False)
    return _text(apn_fact), address, attributes, ownership, hoa


# ---------------------------------------------------------------- data quality (§10 DCS inputs)

def _critical_presence(apn, address, attributes, ownership, taxes, rental, valuations,
                       mortgages, liens, foreclosure) -> dict[str, bool]:
    first = next((m for m in mortgages if m.position == "1"), mortgages[0] if mortgages else None)

    def has(tracked: TrackedValue | None) -> bool:
        return tracked is not None and tracked.value is not None

    return {
        "apn": apn is not None, "address": address.line1 is not None,
        "sqft": has(attributes.sqft), "beds": has(attributes.beds), "baths": has(attributes.baths),
        "year_built": has(attributes.year_built), "owner": bool(ownership.owner_names),
        "occupancy": ownership.is_owner_occupied is not None,
        "last_sale_date": ownership.ownership_start_date is not None,
        "last_sale_price": has(ownership.purchase_price),
        "avm": any("avm" in c.valuation_type for c in valuations),
        "comp_estimate": any("comp" in c.valuation_type for c in valuations),
        "assessed_value": has(taxes.assessed_value), "annual_taxes": has(taxes.annual_taxes),
        "mortgage_1_original": bool(first and has(first.original_amount)),
        "mortgage_1_date": bool(first and first.origination_date),
        "mortgage_1_rate": bool(first and first.rate is not None),
        "mortgage_1_balance": bool(first and has(first.estimated_balance)),
        "foreclosure_status": foreclosure is not None, "lien_count": bool(liens),
        "lien_total": any(has(lien.amount) for lien in liens), "rent": has(rental.rent_estimate),
    }


def _data_quality(presence: dict[str, bool], facts: list[ExtractedFactDraft],
                  ctx: _Context) -> DataQualityBlock:
    covered = sum(1 for present in presence.values() if present)
    coverage = (Decimal(covered) / Decimal(len(presence))).quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)
    verified = sum(1 for field, present in presence.items()
                   if present and (winner := ctx.winners.get(field)) is not None
                   and winner.source_kind == SourceKind.HUMAN)
    if facts:
        mean = Decimal(str(sum(f.extraction_confidence for f in facts) / len(facts)))
        mean = mean.quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)
    else:
        mean = Decimal("0")
    return DataQualityBlock(critical_field_coverage=coverage,
                            source_counts_by_field={field: len(reports) for field, reports
                                                    in sorted(ctx.contributions.items())},
                            conflict_count=ctx.conflict_count,
                            material_conflict_count=ctx.material_conflict_count,
                            verified_field_count=verified, ocr_applied=ctx.ocr_applied,
                            newest_report_date=max((f.as_of_date for f in facts if f.as_of_date), default=None),
                            mean_extraction_confidence=mean)


# ---------------------------------------------------------------- top-level resolver

def resolve_facts(property_id, facts: list[ExtractedFactDraft], *, as_of: date | None = None,
                  ocr_applied: bool = False) -> NormalizedProperty:
    as_of = as_of or date.today()
    ctx = _Context(as_of, ocr_applied)
    by_type: dict[EntityType, list[ExtractedFactDraft]] = defaultdict(list)
    for fact in facts:
        by_type[fact.entity_type].append(fact)

    apn, address, attributes, ownership, hoa = _build_property_core(by_type[EntityType.PROPERTY], ctx)

    # One candidate per valuation type: groups reporting the same type are merged first.
    valuation_groups: dict[str, list[ExtractedFactDraft]] = defaultdict(list)
    for group in _group_by_local_id(by_type[EntityType.VALUATION]):
        resolved = _resolve_leaves(group, _VALUATION_ALIASES, ctx)
        valuation_type = (_text(_winner(resolved, "valuation_type")) or "unknown").casefold()
        valuation_groups[valuation_type].extend(group)
    valuations, valuation_winners = [], []
    for valuation_type in sorted(valuation_groups):
        candidate, value_fact = _build_valuation(valuation_groups[valuation_type], ctx)
        if candidate is not None:
            valuations.append(candidate)
            valuation_winners.append((candidate, value_fact))
    valuation_values = sorted({c.value.value for c in valuations if c.value.value is not None})
    if len(valuation_values) > 1 and valuation_values[-1] > _VALUATION_DISPERSION_RATIO * valuation_values[0]:
        ctx.conflict(impact=valuation_values[-1] - valuation_values[0],
                     flag_type=FlagType.VALUATION_DISPERSION)
    valuations.sort(key=lambda c: (c.valuation_type, c.value.value or Decimal("0")))
    for candidate, value_fact in valuation_winners:
        if "avm" in candidate.valuation_type:
            ctx.register("avm", value_fact)
        if "comp" in candidate.valuation_type:
            ctx.register("comp_estimate", value_fact)

    mortgage_groups = _merge_groups(_group_by_local_id(by_type[EntityType.MORTGAGE]),
                                    lambda a, b: _same_mortgage(a, b, ctx))
    built_mortgages = [_build_mortgage(group, ctx) for group in mortgage_groups]
    built_mortgages.sort(key=lambda item: _position_key(item[0]))
    mortgages = [record for record, _ in built_mortgages]
    if built_mortgages:
        first_winners = next((w for record, w in built_mortgages if record.position == "1"),
                             built_mortgages[0][1])
        ctx.register("mortgage_1_original", first_winners["original"])
        ctx.register("mortgage_1_date", first_winners["date"])
        ctx.register("mortgage_1_rate", first_winners["rate"])
        ctx.register("mortgage_1_balance", first_winners["balance"])

    lien_groups = _merge_groups(_group_by_local_id(by_type[EntityType.LIEN]),
                                lambda a, b: _same_lien(a, b, ctx))
    built_liens = [_build_lien(group, ctx) for group in lien_groups]
    built_liens.sort(key=lambda item: _lien_key(item[0]))
    liens = [record for record, _ in built_liens]
    if built_liens:
        ctx.register("lien_count", built_liens[0][1]["type"])
        ctx.register("lien_total", next((w["amount"] for _, w in built_liens if w["amount"]), None))

    foreclosure = None
    foreclosure_facts = by_type[EntityType.FORECLOSURE]
    if foreclosure_facts:
        foreclosure, stage_fact = _build_foreclosure(foreclosure_facts, ctx)
        ctx.register("foreclosure_status", stage_fact)

    bankruptcies = sorted((record for group in _group_by_local_id(by_type[EntityType.BANKRUPTCY])
                           if (record := _build_bankruptcy(group, ctx)) is not None),
                          key=lambda r: (r.filing_date or date.min, r.chapter))

    tax_facts = by_type[EntityType.TAX]
    tax_resolved = _resolve_leaves(tax_facts, _TAX_ALIASES, ctx) if tax_facts else {}

    def tax_tracked(leaf: str) -> TrackedValue | None:
        fact = _winner(tax_resolved, leaf)
        if fact:
            ctx.register({"assessed_value": "assessed_value", "annual_taxes": "annual_taxes"}[leaf], fact)
            return _tracked(fact)
        return None

    taxes = TaxBlock(annual_taxes=tax_tracked("annual_taxes"), assessed_value=tax_tracked("assessed_value"),
                     delinquent_amount=_tracked(_winner(tax_resolved, "delinquent_amount"))
                     if _winner(tax_resolved, "delinquent_amount") else None,
                     delinquent_years=_int(_winner(tax_resolved, "delinquent_years")))

    rental_facts = by_type[EntityType.RENTAL]
    rental_resolved = _resolve_leaves(rental_facts, _RENTAL_ALIASES, ctx) if rental_facts else {}
    rent_fact = _winner(rental_resolved, "rent_estimate")
    if rent_fact:
        ctx.register("rent", rent_fact)
    rental = RentalBlock(rent_estimate=_tracked(rent_fact) if rent_fact else None,
                         source=_text(_winner(rental_resolved, "source")))

    condition = None
    for group in _group_by_local_id(by_type[EntityType.CONDITION]):
        resolved = _resolve_leaves(group, _CONDITION_ALIASES, ctx)
        condition_fact = _winner(resolved, "condition")
        level = (_text(condition_fact) or "").casefold()
        if level in CONDITION_LADDER:
            condition = ConditionSignal(condition=level, evidence=_text(_winner(resolved, "evidence"))
                                        or (condition_fact.snippet if condition_fact else None))
            break

    listings = sorted((record for group in _group_by_local_id(by_type[EntityType.LISTING])
                       if (record := _build_listing(group, ctx)) is not None),
                      key=lambda r: (r.list_date, r.status))
    comparables = sorted((record for group in _group_by_local_id(by_type[EntityType.COMP])
                          if (record := _build_comp(group, ctx)) is not None),
                         key=lambda r: (r.address.casefold(), r.sale_date or date.min))

    if apn is None:
        ctx.flag(FlagType.MISSING_APN)
    if foreclosure and foreclosure.published_bid and foreclosure.published_bid.value is not None:
        reference = next((m.estimated_balance for m in mortgages
                          if m.position == "1" and m.is_open and m.estimated_balance
                          and m.estimated_balance.value), None)
        reference = reference or next((m.estimated_balance for m in mortgages
                                       if m.is_open and m.estimated_balance and m.estimated_balance.value), None)
        if reference and reference.value:
            divergence = abs(foreclosure.published_bid.value - reference.value) / reference.value
            if divergence > _BID_MISMATCH_PCT:
                ctx.conflict(impact=abs(foreclosure.published_bid.value - reference.value),
                             flag_type=FlagType.BID_MISMATCH)
    if any(winner is not None and winner.extraction_confidence < _LOW_CONFIDENCE_THRESHOLD
           for field, winner in ctx.winners.items() if field in CRITICAL_FIELDS):
        ctx.flag(FlagType.LOW_EXTRACTION_CONFIDENCE)

    ctx.flags.sort(key=lambda f: (f.financial_impact is None,
                                  -(f.financial_impact or Decimal("0")), f.type.value))
    presence = _critical_presence(apn, address, attributes, ownership, taxes, rental, valuations,
                                  mortgages, liens, foreclosure)
    return NormalizedProperty(property_id=property_id, apn=apn, address=address, attributes=attributes,
                              ownership=ownership, valuation_candidates=valuations, mortgages=mortgages,
                              liens=liens, foreclosure=foreclosure, bankruptcies=bankruptcies, taxes=taxes,
                              hoa=hoa, rental=rental, listings=listings, comparables=comparables,
                              condition=condition,
                              data_quality=_data_quality(presence, facts, ctx), open_flags=ctx.flags,
                              resolution_version=RESOLVER_VERSION)
