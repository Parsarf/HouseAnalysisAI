"""Deterministic validation gauntlet for extracted facts (spec §5.3).

Order: schema (Pydantic, done before facts reach here) → snippet grounding →
parse consistency → range sanity → cross-field logic → null-reason discipline.
Grounding/parse/null failures drop the fact; range and cross-field failures
keep it but mark it inactive. The model never gets the last word.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from common.errors import ErrorCode
from contracts import ExtractedFactDraft


def grounded(fact: ExtractedFactDraft, page_text: str) -> bool:
    normalized = lambda value: re.sub(r"\s+", " ", value.casefold()).strip()
    return normalized(fact.snippet) in normalized(page_text)


def validate_grounding(fact: ExtractedFactDraft, page_text: str) -> tuple[ExtractedFactDraft | None, str | None]:
    if not grounded(fact, page_text):
        return None, ErrorCode.GROUNDING_FAILED.value
    if fact.value_parsed is None and fact.value_raw is not None and fact.null_reason is None:
        return None, ErrorCode.INVALID_INPUT.value
    return fact, None


# --- Parse consistency (spec §5.3 step 3) ------------------------------------

_NUMERIC_RE = re.compile(r"^\(?\s*-?\s*\$?\s*[\d,]*\.?\d+\s*[kKmMbB]?\s*%?\s*\)?$")
_MULTIPLIERS = {"k": Decimal("1e3"), "m": Decimal("1e6"), "b": Decimal("1e9")}


def parse_numeric(value_raw: str | None) -> Decimal | None:
    """Mechanical parse of a raw numeric string: `$`, commas, parentheses
    negatives, `1.2M`-style suffixes, `%`. Returns None when not parseable."""
    if not value_raw:
        return None
    text = value_raw.strip()
    if not _NUMERIC_RE.match(text):
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace("$", "").replace(",", "").replace("%", "").strip()
    if text.startswith("-"):
        negative = True
        text = text[1:].strip()
    multiplier = Decimal(1)
    if text and text[-1].lower() in _MULTIPLIERS:
        multiplier = _MULTIPLIERS[text[-1].lower()]
        text = text[:-1]
    try:
        value = Decimal(text) * multiplier
    except Exception:
        return None
    return -value if negative else value


def _parse_consistent(fact: ExtractedFactDraft) -> bool:
    if fact.value_parsed is None or fact.value_raw is None:
        # No raw to derive from (plain numeric schema fields carry no _raw).
        return True
    parsed = parse_numeric(fact.value_raw)
    if parsed is None:
        return False
    return abs(parsed - fact.value_parsed) <= Decimal("0.01")


# --- Range sanity (spec §5.3 step 4) ------------------------------------------

def _current_year() -> int:
    return date.today().year


_PRICE_FIELDS = {"price", "value", "purchase_price", "sale_price", "list_price"}


def range_violation(fact: ExtractedFactDraft) -> str | None:
    """Returns a description when value_parsed falls outside sane bounds."""
    value = fact.value_parsed
    if value is None:
        return None
    leaf = fact.field_path.rsplit(".", 1)[-1]
    if leaf == "sqft" and not (100 <= value <= 100_000):
        return "sqft_out_of_range"
    if leaf in {"beds", "units"} and not (0 <= value <= 30):
        return "beds_out_of_range"
    if leaf in {"year_built", "tax_year"} and not (1600 <= value <= _current_year() + 2):
        return "year_out_of_range"
    if leaf == "rate" and not (0 <= value <= 25):
        return "rate_out_of_range"
    if fact.entity_type == "lien" and leaf == "amount" and not (1 <= value <= 50_000_000):
        return "lien_amount_out_of_range"
    if leaf in _PRICE_FIELDS and not (1_000 <= value <= 100_000_000):
        return "price_out_of_range"
    return None


# --- Lien attachment anchor check (spec §5.4) ---------------------------------

_ANCHOR_RE = re.compile(
    r"\b(apn|parcel|lot\s*\w|tract|block\s*\w|subdivision|legal\s+description"
    r"|book\s*\w+|page\s*\w+|instrument|doc(?:ument)?\s*(?:#|no|number)"
    r"|\d{2,}-\d{2,}"  # APN-like or recording-reference numbers
    r"|\d{2,5}\s+\w[\w ]*\b(?:st|ave|avenue|rd|road|blvd|dr|drive|ln|lane|ct|court|way|pl|place|ter|terrace)\b)",
    re.IGNORECASE,
)


def attachment_anchor_present(snippet: str) -> bool:
    return bool(_ANCHOR_RE.search(snippet))


# --- Null discipline (spec §5.3 step 6) ---------------------------------------

def _is_null_fact(fact: ExtractedFactDraft) -> bool:
    return all(
        value is None
        for value in (fact.value_raw, fact.value_parsed, fact.value_text, fact.value_date, fact.value_bool)
    )


# --- Cross-field logic (spec §5.3 step 5) -------------------------------------

def _values(facts: list[ExtractedFactDraft]) -> dict[str, ExtractedFactDraft]:
    return {fact.field_path.rsplit(".", 1)[-1]: fact for fact in facts}


def _cross_field_violations(facts: list[ExtractedFactDraft]) -> set[int]:
    """Returns ids() of facts that violate cross-field rules (spec §5.3 step 5)."""
    bad: set[int] = set()
    groups: dict[str, list[ExtractedFactDraft]] = {}
    for fact in facts:
        groups.setdefault(fact.entity_local_id, []).append(fact)
    has_first_mortgage = any(
        _values(group).get("position") is not None
        and _values(group)["position"].value_text == "first"
        for key, group in groups.items()
        if key.rsplit("[", 1)[0] == "mortgages"
    )
    for key, group in groups.items():
        values = _values(group)
        stem = key.rsplit("[", 1)[0]
        if stem == "mortgages":
            original, balance = values.get("original_amount"), values.get("balance")
            if (original and original.value_parsed is not None and balance
                    and balance.value_parsed is not None
                    and balance.value_parsed > original.value_parsed * Decimal("1.5")):
                bad.add(id(balance))
            position = values.get("position")
            if position and position.value_text in {"second", "third"} and not has_first_mortgage:
                bad.update(id(fact) for fact in group)
        if stem == "foreclosure_events":
            nod, nts = values.get("nod_date"), values.get("nts_date")
            if nod and nod.value_date and nts and nts.value_date and nts.value_date < nod.value_date:
                bad.update((id(nod), id(nts)))
    # Sale date >= year built is a property-level rule, checked across entities.
    built = next((f for f in facts if f.field_path.endswith(".year_built") and f.value_parsed is not None), None)
    sale = next((f for f in facts if f.field_path.endswith(".ownership_start_date") and f.value_date is not None), None)
    if (built is not None and built.value_parsed is not None
            and sale is not None and sale.value_date is not None
            and sale.value_date < date(int(built.value_parsed), 1, 1)):
        bad.update((id(sale), id(built)))
    return bad


# --- The gauntlet --------------------------------------------------------------

@dataclass(frozen=True)
class GauntletOutcome:
    active: list[ExtractedFactDraft]
    inactive: list[ExtractedFactDraft]
    dropped: int
    counters: dict[str, int] = field(default_factory=dict)


def run_gauntlet(
    facts: list[ExtractedFactDraft],
    page_text_by_number: dict[int, str],
    *,
    dropped: int = 0,
    counters: dict[str, int] | None = None,
) -> GauntletOutcome:
    counts: dict[str, int] = dict(counters or {})
    if dropped:
        counts["schema_failed"] = counts.get("schema_failed", 0) + dropped

    def bump(name: str) -> None:
        counts[name] = counts.get(name, 0) + 1

    survivors: list[ExtractedFactDraft] = []
    for fact in facts:
        # 2. Snippet grounding (also enforces raw-without-parse null discipline).
        valid, error = validate_grounding(fact, page_text_by_number.get(fact.page_number, ""))
        if valid is None:
            bump(error or ErrorCode.GROUNDING_FAILED.value)
            continue
        fact = valid
        # 3. Parse consistency.
        if not _parse_consistent(fact):
            bump("parse_inconsistency")
            continue
        # 5.4 Lien attachment: recorded_against_property needs a parcel anchor.
        if (fact.field_path.endswith(".attachment_basis")
                and fact.value_text == "recorded_against_property"
                and not attachment_anchor_present(fact.snippet)):
            fact = fact.model_copy(update={"value_text": "unknown"})
            bump("attachment_downgraded")
        # 6. Null discipline.
        if _is_null_fact(fact) and fact.null_reason is None:
            bump("null_reason_missing")
            continue
        survivors.append(fact)

    # 4. Range sanity — stored but inactive.
    active: list[ExtractedFactDraft] = []
    inactive: list[ExtractedFactDraft] = []
    for fact in survivors:
        if range_violation(fact):
            inactive.append(fact)
            bump("range_violation")
        else:
            active.append(fact)

    # 5. Cross-field logic — stored but inactive.
    bad_ids = _cross_field_violations(active)
    for _ in bad_ids:
        bump("cross_field_violation")
    still_active = [fact for fact in active if id(fact) not in bad_ids]
    inactive.extend(fact for fact in active if id(fact) in bad_ids)

    return GauntletOutcome(still_active, inactive, sum(
        count for name, count in counts.items()
        if name not in {"range_violation", "cross_field_violation", "attachment_downgraded"}
    ), counts)
