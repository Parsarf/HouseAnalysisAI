from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from common.serializers import json_safe
from contracts import NormalizedProperty


class ChangeType(StrEnum):
    """Closed enum of reportable change types (spec §14 / WP-16).

    Anything not in this enum is not a change worth reporting.
    """

    NEW_FORECLOSURE_NOTICE = "new_foreclosure_notice"
    SALE_DATE_MOVED = "sale_date_moved"
    SALE_CANCELLED = "sale_cancelled"
    SALE_COMPLETED = "sale_completed"
    NEW_LIEN = "new_lien"
    LIEN_RELEASED = "lien_released"
    LIEN_AMOUNT_CORRECTED = "lien_amount_corrected"
    NEW_LISTING = "new_listing"
    PRICE_CUT = "price_cut"
    OWNERSHIP_TRANSFER = "ownership_transfer"
    NEW_BANKRUPTCY = "new_bankruptcy"
    VALUE_SHIFT = "value_shift"


VALUE_SHIFT_THRESHOLD = Decimal("0.10")


@dataclass(frozen=True)
class ChangeEvent:
    change_type: str
    field_path: str
    old_value: object
    new_value: object
    score_delta: Decimal | None = None


def _tracked(value) -> Decimal | None:
    return None if value is None else value.value


def _lien_key(lien) -> tuple:
    return (lien.lien_type, lien.recording_date)


def _match(before_items, after_items, key):
    """Greedily pair before/after entities by identity key. Returns (pairs, only_before, only_after)."""
    remaining = list(before_items)
    pairs, only_after = [], []
    for item in after_items:
        match = next((candidate for candidate in remaining if key(candidate) == key(item)), None)
        if match is None:
            only_after.append(item)
        else:
            remaining.remove(match)
            pairs.append((match, item))
    return pairs, remaining, only_after


def _diff_foreclosure(before, after, score_delta) -> list[ChangeEvent]:
    events = []
    before_active = before is not None and before.is_active
    after_active = after is not None and after.is_active
    if not before_active and after_active:
        events.append(ChangeEvent(ChangeType.NEW_FORECLOSURE_NOTICE, "foreclosure.stage",
                                  None if before is None else before.stage, after.stage, score_delta))
        return events
    if before is None or after is None:
        if before_active and not after_active:
            stage = None if after is None else after.stage
            change_type = ChangeType.SALE_COMPLETED if stage in ("sold", "completed") else ChangeType.SALE_CANCELLED
            events.append(ChangeEvent(change_type, "foreclosure.stage", before.stage, stage, score_delta))
        return events
    if after.rescission_count > before.rescission_count or (
        after.stage in ("rescinded", "cancelled") and before.stage not in ("rescinded", "cancelled")
    ):
        events.append(ChangeEvent(ChangeType.SALE_CANCELLED, "foreclosure.stage",
                                  before.stage, after.stage, score_delta))
    elif after.stage in ("sold", "completed") and before.stage not in ("sold", "completed"):
        events.append(ChangeEvent(ChangeType.SALE_COMPLETED, "foreclosure.stage",
                                  before.stage, after.stage, score_delta))
    elif before.current_sale_date != after.current_sale_date:
        events.append(ChangeEvent(ChangeType.SALE_DATE_MOVED, "foreclosure.current_sale_date",
                                  before.current_sale_date, after.current_sale_date, score_delta))
    return events


def _diff_liens(before, after, score_delta) -> list[ChangeEvent]:
    events = []
    pairs, only_before, only_after = _match(before, after, _lien_key)
    for lien in only_after:
        events.append(ChangeEvent(ChangeType.NEW_LIEN, f"liens[{lien.lien_type}]",
                                  None, _tracked(lien.amount), score_delta))
    for lien in only_before:
        events.append(ChangeEvent(ChangeType.LIEN_RELEASED, f"liens[{lien.lien_type}]",
                                  _tracked(lien.amount), None, score_delta))
    for old, new in pairs:
        if new.status == "released" and old.status != "released":
            events.append(ChangeEvent(ChangeType.LIEN_RELEASED, f"liens[{new.lien_type}].status",
                                      old.status, new.status, score_delta))
        elif _tracked(old.amount) != _tracked(new.amount):
            events.append(ChangeEvent(ChangeType.LIEN_AMOUNT_CORRECTED, f"liens[{new.lien_type}].amount",
                                      _tracked(old.amount), _tracked(new.amount), score_delta))
    return events


def _diff_listings(before, after, score_delta) -> list[ChangeEvent]:
    events = []
    pairs, _, only_after = _match(before, after, lambda listing: (listing.list_date, listing.status))
    for listing in only_after:
        events.append(ChangeEvent(ChangeType.NEW_LISTING, f"listings[{listing.list_date}]",
                                  None, _tracked(listing.price), score_delta))
    for old, new in pairs:
        old_price, new_price = _tracked(old.price), _tracked(new.price)
        if old_price is not None and new_price is not None and new_price < old_price:
            events.append(ChangeEvent(ChangeType.PRICE_CUT, f"listings[{new.list_date}].price",
                                      old_price, new_price, score_delta))
    return events


def _diff_ownership(before, after, score_delta) -> list[ChangeEvent]:
    if sorted(before.owner_names) == sorted(after.owner_names):
        return []
    return [ChangeEvent(ChangeType.OWNERSHIP_TRANSFER, "ownership.owner_names",
                        before.owner_names, after.owner_names, score_delta)]


def _diff_bankruptcies(before, after, score_delta) -> list[ChangeEvent]:
    _, _, only_after = _match(before, after, lambda b: (b.chapter, b.filing_date))
    return [ChangeEvent(ChangeType.NEW_BANKRUPTCY, f"bankruptcies[{b.chapter}]",
                        None, b.filing_date, score_delta) for b in only_after]


def _representative_value(candidates) -> Decimal | None:
    total_weight = Decimal("0")
    weighted = Decimal("0")
    for candidate in candidates:
        value = candidate.value.value
        if value is None:
            continue
        weight = (candidate.weight_hint or Decimal("1")) * Decimal(str(candidate.value.confidence))
        weighted += value * weight
        total_weight += weight
    return None if total_weight == 0 else weighted / total_weight


def _diff_value(before, after, score_delta) -> list[ChangeEvent]:
    old_value = _representative_value(before.valuation_candidates)
    new_value = _representative_value(after.valuation_candidates)
    if old_value is None or new_value is None or old_value == 0:
        return []
    if abs(new_value - old_value) / abs(old_value) <= VALUE_SHIFT_THRESHOLD:
        return []
    return [ChangeEvent(ChangeType.VALUE_SHIFT, "valuation.value", old_value, new_value, score_delta)]


def diff_properties(before: NormalizedProperty, after: NormalizedProperty,
                    score_delta: Decimal | None = None) -> list[ChangeEvent]:
    """Structurally diff two resolved property records at the field/entity level."""
    if before.property_id != after.property_id:
        raise ValueError("cannot diff snapshots of different properties")
    events: list[ChangeEvent] = []
    events += _diff_foreclosure(before.foreclosure, after.foreclosure, score_delta)
    events += _diff_liens(before.liens, after.liens, score_delta)
    events += _diff_listings(before.listings, after.listings, score_delta)
    events += _diff_ownership(before.ownership, after.ownership, score_delta)
    events += _diff_bankruptcies(before.bankruptcies, after.bankruptcies, score_delta)
    events += _diff_value(before, after, score_delta)
    return events


def diff_records(before: dict, after: dict, score_delta: Decimal | None = None) -> list[ChangeEvent]:
    events = []
    for field in sorted(set(before) | set(after)):
        old, new = before.get(field), after.get(field)
        if json_safe(old) == json_safe(new):
            continue
        if field.startswith("liens"):
            change_type: str = ChangeType.NEW_LIEN if old is None else ChangeType.LIEN_AMOUNT_CORRECTED
        elif field.startswith("foreclosure"):
            change_type = ChangeType.NEW_FORECLOSURE_NOTICE if old is None else ChangeType.SALE_DATE_MOVED
        else:
            change_type = "field_changed"
        events.append(ChangeEvent(change_type, field, old, new, score_delta))
    return events
