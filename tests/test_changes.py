from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from changes import ChangeType, diff_properties, diff_records
from contracts import (
    AddressBlock,
    AttachmentBasis,
    BankruptcyRecord,
    ForeclosureState,
    LienRecord,
    ListingRecord,
    NormalizedProperty,
    OwnershipBlock,
    SourceKind,
    TrackedValue,
    ValuationCandidate,
)


def money(value, estimated=False):
    return TrackedValue(value=Decimal(str(value)), confidence=0.9,
                        source_kind=SourceKind.REPORT, is_estimated=estimated)


def base_property(**overrides):
    kwargs = {"property_id": uuid4(), "address": AddressBlock(line1="1 Main St", zip5="90001"),
                  "ownership": OwnershipBlock(owner_names=["Jane Doe"]),
                  "resolution_version": "test"}
    kwargs.update(overrides)
    return NormalizedProperty(**kwargs)


def lien(lien_type="tax", amount="10000", status="active", recording_date=None,
         basis=AttachmentBasis.RECORDED_AGAINST_PROPERTY):
    return LienRecord(lien_type=lien_type, amount=money(amount), status=status,
                      recording_date=recording_date, attachment_basis=basis,
                      attachment_confidence=0.9)


def test_identical_snapshots_produce_zero_events():
    prop = base_property(liens=[lien()], resolution_version="v1")
    before = prop.model_copy(deep=True)
    after = prop.model_copy(deep=True)
    after.resolution_version = "v2"
    assert diff_properties(before, after) == []


def test_new_lien_and_amount_correction_have_different_change_types():
    before = base_property(liens=[lien(amount="10000", recording_date=date(2024, 1, 15))])
    after = before.model_copy(deep=True)
    after.liens[0].amount = money("12500")
    after.liens.append(lien(lien_type="judgment", amount="5000", recording_date=date(2024, 3, 1)))
    events = diff_properties(before, after)
    by_type = {event.change_type: event for event in events}
    assert ChangeType.LIEN_AMOUNT_CORRECTED in by_type
    assert ChangeType.NEW_LIEN in by_type
    correction = by_type[ChangeType.LIEN_AMOUNT_CORRECTED]
    assert correction.field_path == "liens[tax].amount"
    assert correction.old_value == Decimal(10000)
    assert correction.new_value == Decimal(12500)
    assert by_type[ChangeType.NEW_LIEN].field_path == "liens[judgment]"


def test_lien_released_by_status_and_by_removal():
    before = base_property(liens=[lien(recording_date=date(2024, 1, 15)), lien(lien_type="hoa")])
    after = before.model_copy(deep=True)
    after.liens[0].status = "released"
    after.liens = after.liens[:1]
    events = diff_properties(before, after)
    assert [event.change_type for event in events] == [ChangeType.LIEN_RELEASED] * 2


def test_new_foreclosure_notice():
    before = base_property()
    after = before.model_copy(deep=True)
    after.foreclosure = ForeclosureState(stage="nod", nod_date=date(2025, 1, 10), is_active=True)
    (event,) = diff_properties(before, after)
    assert event.change_type == ChangeType.NEW_FORECLOSURE_NOTICE
    assert event.new_value == "nod"


def test_sale_date_moved():
    foreclosure = ForeclosureState(stage="nts", is_active=True,
                                   current_sale_date=date(2025, 6, 1))
    before = base_property(foreclosure=foreclosure)
    after = before.model_copy(deep=True)
    after.foreclosure.current_sale_date = date(2025, 7, 15)
    (event,) = diff_properties(before, after)
    assert event.change_type == ChangeType.SALE_DATE_MOVED
    assert event.field_path == "foreclosure.current_sale_date"
    assert event.old_value == date(2025, 6, 1)
    assert event.new_value == date(2025, 7, 15)


def test_sale_cancelled_on_rescission():
    before = base_property(foreclosure=ForeclosureState(stage="nts", is_active=True,
                                                        current_sale_date=date(2025, 6, 1)))
    after = before.model_copy(deep=True)
    after.foreclosure.rescission_count = 1
    after.foreclosure.stage = "rescinded"
    (event,) = diff_properties(before, after)
    assert event.change_type == ChangeType.SALE_CANCELLED


def test_sale_completed():
    before = base_property(foreclosure=ForeclosureState(stage="nts", is_active=True,
                                                        current_sale_date=date(2025, 6, 1)))
    after = before.model_copy(deep=True)
    after.foreclosure.stage = "sold"
    after.foreclosure.is_active = False
    (event,) = diff_properties(before, after)
    assert event.change_type == ChangeType.SALE_COMPLETED


def test_new_listing_and_price_cut():
    listing = ListingRecord(list_date=date(2025, 2, 1), price=money("350000"), status="active")
    before = base_property(listings=[listing])
    after = before.model_copy(deep=True)
    after.listings[0].price = money("329000")
    after.listings.append(ListingRecord(list_date=date(2025, 5, 1), price=money("319000"),
                                        status="active"))
    events = diff_properties(before, after)
    by_type = {event.change_type: event for event in events}
    assert by_type[ChangeType.PRICE_CUT].old_value == Decimal(350000)
    assert by_type[ChangeType.PRICE_CUT].new_value == Decimal(329000)
    assert ChangeType.NEW_LISTING in by_type


def test_ownership_transfer():
    before = base_property()
    after = before.model_copy(deep=True)
    after.ownership.owner_names = ["John Smith"]
    (event,) = diff_properties(before, after)
    assert event.change_type == ChangeType.OWNERSHIP_TRANSFER
    assert event.old_value == ["Jane Doe"]
    assert event.new_value == ["John Smith"]


def test_new_bankruptcy():
    before = base_property()
    after = before.model_copy(deep=True)
    after.bankruptcies = [BankruptcyRecord(chapter="13", status="active",
                                           filing_date=date(2025, 4, 1))]
    (event,) = diff_properties(before, after)
    assert event.change_type == ChangeType.NEW_BANKRUPTCY


def test_value_shift_only_above_ten_percent():
    before = base_property(valuation_candidates=[
        ValuationCandidate(valuation_type="avm", value=money("400000"))])
    small_move = before.model_copy(deep=True)
    small_move.valuation_candidates[0].value = money("430000")
    assert diff_properties(before, small_move) == []

    big_move = before.model_copy(deep=True)
    big_move.valuation_candidates[0].value = money("460000")
    (event,) = diff_properties(before, big_move)
    assert event.change_type == ChangeType.VALUE_SHIFT
    assert event.old_value == Decimal(400000)
    assert event.new_value == Decimal(460000)


def test_score_delta_is_propagated():
    before = base_property()
    after = before.model_copy(deep=True)
    after.liens.append(lien())
    (event,) = diff_properties(before, after, score_delta=Decimal("-7.5"))
    assert event.score_delta == Decimal("-7.5")


def test_diff_requires_same_property():
    with pytest.raises(ValueError):
        diff_properties(base_property(), base_property())


def test_diff_records_flat_dict_fallback():
    events = diff_records({"liens": None}, {"liens": [{"amount": 5}]})
    (event,) = events
    assert event.change_type == ChangeType.NEW_LIEN
    assert diff_records({"a": 1}, {"a": 1}) == []
