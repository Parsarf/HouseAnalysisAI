from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from common.money import money
from contracts import (
    AssumptionSet,
    AttachmentBasis,
    FlagRequest,
    FlagSummary,
    FlagType,
    NormalizedProperty,
    Scenario,
    SourceKind,
    TrackedValue,
)

LIEN_ATTACHMENT_THRESHOLD = Decimal(10000)
DISPERSION_RATIO_THRESHOLD = Decimal("1.5")
BID_MISMATCH_RATIO_THRESHOLD = Decimal("0.20")
LOW_CONFIDENCE_THRESHOLD = 0.65
INACTIVE_LIEN_STATUSES = ("released", "satisfied")

# Spec §12: identity_conflict blocks ranking until resolved.
GATING_FLAG_TYPES: frozenset[FlagType] = frozenset({FlagType.IDENTITY_CONFLICT})

# Spec §4 range sanity bounds: sale price $1k-$100M, sqft 100-100k, beds 0-30,
# year 1600-now+2, rate 0-25%, lien $1-$50M.
RANGE_BOUNDS: dict[str, tuple[Decimal, Decimal]] = {
    "sqft": (Decimal(100), Decimal(100000)),
    "beds": (Decimal(0), Decimal(30)),
    "year_built": (Decimal(1600), Decimal(datetime.now(UTC).year + 2)),
    "rate": (Decimal(0), Decimal(25)),
    "lien_amount": (Decimal(1), Decimal(50000000)),
    "value": (Decimal(1000), Decimal(100000000)),
}


def is_gating(flag_type: FlagType) -> bool:
    return flag_type in GATING_FLAG_TYPES


def flag_summaries(requests: list[FlagRequest]) -> list[FlagSummary]:
    """Summaries for NormalizedProperty.open_flags, which scoring reads for gates."""
    return [FlagSummary(type=request.flag_type, is_gating=is_gating(request.flag_type),
                        financial_impact=request.financial_impact_usd) for request in requests]


def _lien_amount(lien) -> Decimal | None:
    return lien.amount.value if lien.amount and lien.amount.value is not None else None


def _expected_adjusted_equity(record: NormalizedProperty, assumptions: AssumptionSet) -> Decimal | None:
    from finance import underwrite  # lazy: flags sits downstream of finance

    result = underwrite(record, assumptions)
    block = result.equity.get(Scenario.EXPECTED)
    return block.adjusted if block and block.adjusted is not None else None


def _financial_impact(record: NormalizedProperty, assumptions: AssumptionSet | None,
                      mutate_accept: Callable[[NormalizedProperty], None],
                      mutate_reject: Callable[[NormalizedProperty], None] | None = None) -> Decimal | None:
    """Equity delta (expected scenario) between accepting and rejecting the disputed value.

    Computed by calling WP-6 twice with the two candidate records (spec §12); this is the
    flag queue's sort key. None when assumptions are unavailable or equity is uncomputable.
    """
    if assumptions is None:
        return None
    accepted = record.model_copy(deep=True)
    mutate_accept(accepted)
    rejected = record.model_copy(deep=True)
    if mutate_reject:
        mutate_reject(rejected)
    accept_equity = _expected_adjusted_equity(accepted, assumptions)
    reject_equity = _expected_adjusted_equity(rejected, assumptions)
    if accept_equity is None or reject_equity is None:
        return None
    return abs(accept_equity - reject_equity).quantize(Decimal("0.01"))


def _attach_lien(index: int) -> Callable[[NormalizedProperty], None]:
    def mutate(record: NormalizedProperty) -> None:
        record.liens[index].attachment_basis = AttachmentBasis.RECORDED_AGAINST_PROPERTY
    return mutate


def _release_lien(index: int) -> Callable[[NormalizedProperty], None]:
    def mutate(record: NormalizedProperty) -> None:
        record.liens[index].status = "released"
    return mutate


def _lien_attachment_flags(record: NormalizedProperty, assumptions: AssumptionSet | None) -> list[FlagRequest]:
    flags = []
    for index, lien in enumerate(record.liens):
        if lien.status in INACTIVE_LIEN_STATUSES:
            continue
        amount = _lien_amount(lien)
        if amount is None or amount < LIEN_ATTACHMENT_THRESHOLD:
            continue
        if lien.attachment_basis == AttachmentBasis.RECORDED_AGAINST_PROPERTY:
            continue
        flags.append(FlagRequest(
            property_id=record.property_id, flag_type=FlagType.LIEN_ATTACHMENT,
            payload={"index": index, "lien_type": lien.lien_type, "basis": lien.attachment_basis.value, "amount": str(amount)},
            financial_impact_usd=_financial_impact(record, assumptions, _attach_lien(index), _release_lien(index)),
            raised_by="flags", dedupe_key=f"lien-attachment:{lien.lien_type}:{amount}"))
    return flags


def _missing_lien_amount_flags(record: NormalizedProperty, assumptions: AssumptionSet | None) -> list[FlagRequest]:
    flags = []
    for index, lien in enumerate(record.liens):
        if lien.status in INACTIVE_LIEN_STATUSES or _lien_amount(lien) is not None:
            continue
        median = assumptions.unknown_lien_medians.get(lien.lien_type) if assumptions else None

        def accept(target: NormalizedProperty, index: int = index, median: Decimal | None = median) -> None:
            lien = target.liens[index]
            lien.amount = TrackedValue(value=median, confidence=0.5, source_kind=SourceKind.DERIVED, is_estimated=True)
            lien.amount_is_estimated = True

        flags.append(FlagRequest(
            property_id=record.property_id, flag_type=FlagType.MISSING_LIEN_AMOUNT,
            payload={"index": index, "lien_type": lien.lien_type, "median_used": str(median) if median is not None else None},
            financial_impact_usd=_financial_impact(record, assumptions, accept, _release_lien(index)) if median is not None else None,
            raised_by="flags", dedupe_key=f"missing-lien-amount:{lien.lien_type}:{index}"))
    return flags


def _conflicting_mortgage_flags(record: NormalizedProperty, assumptions: AssumptionSet | None) -> list[FlagRequest]:
    flags = []
    if record.data_quality.material_conflict_count:
        flags.append(FlagRequest(
            property_id=record.property_id, flag_type=FlagType.CONFLICTING_MORTGAGE,
            payload={"count": record.data_quality.material_conflict_count},
            financial_impact_usd=None, raised_by="flags", dedupe_key="material-conflict"))
    by_position: dict[str, list[tuple[int, Decimal]]] = {}
    for index, mortgage in enumerate(record.mortgages):
        if not mortgage.is_open:
            continue
        balance = mortgage.estimated_balance.value if mortgage.estimated_balance and mortgage.estimated_balance.value is not None else None
        if balance is not None:
            by_position.setdefault(mortgage.position, []).append((index, balance))
    for position, entries in by_position.items():
        balances = [balance for _, balance in entries]
        low, high = min(balances), max(balances)
        # Spec §12: balances differ > 5% or $10k.
        if len(entries) > 1 and high - low > max(high * Decimal("0.05"), Decimal(10000)):
            high_index = entries[balances.index(high)][0]
            low_index = entries[balances.index(low)][0]

            def accept_high(target: NormalizedProperty, drop: int = low_index) -> None:
                target.mortgages[drop].is_open = False

            def accept_low(target: NormalizedProperty, drop: int = high_index) -> None:
                target.mortgages[drop].is_open = False

            flags.append(FlagRequest(
                property_id=record.property_id, flag_type=FlagType.CONFLICTING_MORTGAGE,
                payload={"position": position, "balances": [str(balance) for balance in balances]},
                financial_impact_usd=_financial_impact(record, assumptions, accept_high, accept_low),
                raised_by="flags", dedupe_key=f"conflicting-mortgage:{position}:{low}:{high}"))
    return flags


def _foreclosure_unclear_flags(record: NormalizedProperty) -> list[FlagRequest]:
    foreclosure = record.foreclosure
    if not foreclosure:
        return []
    contradictions = []
    if foreclosure.nts_date and not foreclosure.nod_date:
        contradictions.append("nts_without_nod")
    if foreclosure.nod_date and foreclosure.nts_date and foreclosure.nts_date < foreclosure.nod_date:
        contradictions.append("nts_before_nod")
    if foreclosure.original_sale_date and foreclosure.current_sale_date and foreclosure.current_sale_date < foreclosure.original_sale_date:
        contradictions.append("sale_date_moved_earlier")
    if not contradictions:
        return []
    return [FlagRequest(
        property_id=record.property_id, flag_type=FlagType.FORECLOSURE_UNCLEAR,
        payload={"contradictions": contradictions, "stage": foreclosure.stage},
        financial_impact_usd=None, raised_by="flags", dedupe_key="foreclosure-unclear")]


def _valuation_dispersion_flags(record: NormalizedProperty, assumptions: AssumptionSet | None) -> list[FlagRequest]:
    candidates = [(index, candidate.value.value) for index, candidate in enumerate(record.valuation_candidates)
                  if candidate.value.value is not None]
    if len(candidates) < 2:
        return []
    values = [value for _, value in candidates]
    low, high = min(values), max(values)
    if low <= 0 or high / low <= DISPERSION_RATIO_THRESHOLD:
        return []
    low_index = candidates[values.index(low)][0]
    high_index = candidates[values.index(high)][0]

    def keep_only(target: NormalizedProperty, index: int) -> None:
        target.valuation_candidates = [target.valuation_candidates[index]]

    return [FlagRequest(
        property_id=record.property_id, flag_type=FlagType.VALUATION_DISPERSION,
        payload={"ratio": str((high / low).quantize(Decimal("0.0001"))),
                 "candidates": [{"type": record.valuation_candidates[index].valuation_type, "value": str(value)} for index, value in candidates]},
        financial_impact_usd=_financial_impact(record, assumptions,
                                               lambda target: keep_only(target, high_index),
                                               lambda target: keep_only(target, low_index)),
        raised_by="flags", dedupe_key=f"valuation-dispersion:{low}:{high}")]


def _missing_apn_flags(record: NormalizedProperty) -> list[FlagRequest]:
    if record.apn:
        return []
    return [FlagRequest(
        property_id=record.property_id, flag_type=FlagType.MISSING_APN,
        payload={"address": record.address.line1, "zip5": record.address.zip5},
        financial_impact_usd=None, raised_by="flags", dedupe_key="missing-apn")]


def _tracked_fields(record: NormalizedProperty) -> list[tuple[str, TrackedValue | None]]:
    fields: list[tuple[str, TrackedValue | None]] = [
        ("attributes.sqft", record.attributes.sqft),
        ("attributes.beds", record.attributes.beds),
        ("attributes.year_built", record.attributes.year_built),
    ]
    for index, candidate in enumerate(record.valuation_candidates):
        fields.append((f"valuation[{index}].value", candidate.value))
    for index, mortgage in enumerate(record.mortgages):
        fields.append((f"mortgages[{index}].estimated_balance", mortgage.estimated_balance))
    for index, lien in enumerate(record.liens):
        fields.append((f"liens[{index}].amount", lien.amount))
    if record.foreclosure:
        fields.append(("foreclosure.published_bid", record.foreclosure.published_bid))
    return fields


def _low_extraction_confidence_flags(record: NormalizedProperty) -> list[FlagRequest]:
    weak = [{"field": field, "confidence": tracked.confidence}
            for field, tracked in _tracked_fields(record)
            if tracked is not None and tracked.value is not None and tracked.confidence < LOW_CONFIDENCE_THRESHOLD]
    if not weak:
        return []
    return [FlagRequest(
        property_id=record.property_id, flag_type=FlagType.LOW_EXTRACTION_CONFIDENCE,
        payload={"fields": weak},
        financial_impact_usd=None, raised_by="flags", dedupe_key="low-extraction-confidence")]


def _bid_mismatch_flags(record: NormalizedProperty, assumptions: AssumptionSet | None) -> list[FlagRequest]:
    foreclosure = record.foreclosure
    if not foreclosure or not foreclosure.published_bid or foreclosure.published_bid.value is None:
        return []
    bid = foreclosure.published_bid.value
    first = next((mortgage for mortgage in record.mortgages
                  if mortgage.is_open and mortgage.position in ("first", "1")
                  and mortgage.estimated_balance and mortgage.estimated_balance.value is not None), None)
    if first is None:
        return []
    balance = first.estimated_balance.value
    if bid <= 0 or abs(bid - balance) / bid <= BID_MISMATCH_RATIO_THRESHOLD:
        return []

    def accept_bid(target: NormalizedProperty) -> None:
        pass  # the engine already gives the published bid precedence (spec §7.3)

    def reject_bid(target: NormalizedProperty) -> None:
        target.foreclosure.published_bid = None  # engine falls back to the estimated balance

    return [FlagRequest(
        property_id=record.property_id, flag_type=FlagType.BID_MISMATCH,
        payload={"published_bid": str(bid), "estimated_first_balance": str(balance),
                 "ratio": str((abs(bid - balance) / bid).quantize(Decimal("0.0001")))},
        financial_impact_usd=_financial_impact(record, assumptions, accept_bid, reject_bid),
        raised_by="flags", dedupe_key=f"bid-mismatch:{money(bid)}:{money(balance)}")]


def _range_violation_flags(record: NormalizedProperty) -> list[FlagRequest]:
    flags = []
    checks: list[tuple[str, Decimal | None]] = [
        ("sqft", record.attributes.sqft.value if record.attributes.sqft else None),
        ("beds", record.attributes.beds.value if record.attributes.beds else None),
        ("year_built", record.attributes.year_built.value if record.attributes.year_built else None),
    ]
    checks += [("value", candidate.value.value) for candidate in record.valuation_candidates if candidate.value.value is not None]
    checks += [("rate", mortgage.rate) for mortgage in record.mortgages if mortgage.rate is not None]
    checks += [("lien_amount", amount) for amount in (_lien_amount(lien) for lien in record.liens) if amount is not None]
    for field, value in checks:
        if value is None or field not in RANGE_BOUNDS:
            continue
        low, high = RANGE_BOUNDS[field]
        if value < low or value > high:
            flags.append(FlagRequest(
                property_id=record.property_id, flag_type=FlagType.RANGE_VIOLATION,
                payload={"field": field, "value": str(value), "bounds": [str(low), str(high)]},
                financial_impact_usd=None, raised_by="flags", dedupe_key=f"range-violation:{field}:{value}"))
    return flags


def _identity_conflict_flags(record: NormalizedProperty) -> list[FlagRequest]:
    if record.data_quality.conflict_count <= 0:
        return []
    conflicts = [field for field, count in record.data_quality.source_counts_by_field.items()
                 if count > 1 and (field.startswith(("property.apn", "property.address")))]
    if not conflicts:
        return []
    return [FlagRequest(
        property_id=record.property_id, flag_type=FlagType.IDENTITY_CONFLICT,
        payload={"fields": sorted(conflicts)},
        financial_impact_usd=None, raised_by="flags", dedupe_key=f"identity-conflict:{','.join(sorted(conflicts))}")]


def collect_flags(property: NormalizedProperty, assumptions: AssumptionSet | None = None) -> list[FlagRequest]:
    """Generate flag requests for a normalized property (spec §12, all ten trigger types).

    ``assumptions`` enables ``financial_impact_usd`` — the equity delta between accepting and
    rejecting the disputed value, computed via WP-6. Without it, impacts are left None.
    """
    return [
        *_identity_conflict_flags(property),
        *_lien_attachment_flags(property, assumptions),
        *_conflicting_mortgage_flags(property, assumptions),
        *_foreclosure_unclear_flags(property),
        *_missing_lien_amount_flags(property, assumptions),
        *_valuation_dispersion_flags(property, assumptions),
        *_missing_apn_flags(property),
        *_low_extraction_confidence_flags(property),
        *_bid_mismatch_flags(property, assumptions),
        *_range_violation_flags(property),
    ]
