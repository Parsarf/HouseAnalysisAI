"""WP-5 normalization tests. Fully offline: inline ExtractedFactDraft sets, no DB/network."""
import sys
import types
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from contracts import EntityType, FlagType, SourceKind
from contracts.models import ExtractedFactDraft
from normalization import normalize_source_kind, resolve_facts
from normalization.resolver import RESOLVER_VERSION, _amortized_balance

AS_OF = date(2026, 1, 1)
PROPERTY_ID = UUID("00000000-0000-0000-0000-0000000000a1")


def fact(entity_type, local_id, field_path, *, parsed=None, text=None, when=None,
         flag=None, conf=0.9, source=SourceKind.REPORT, as_of=None, report=None):
    return ExtractedFactDraft(report_id=report or uuid4(), extraction_unit_id=uuid4(),
                              entity_type=entity_type, entity_local_id=local_id,
                              field_path=field_path, value_parsed=parsed, value_text=text,
                              value_date=when, value_bool=flag, as_of_date=as_of,
                              page_number=1, snippet="snippet", extraction_confidence=conf,
                              source_kind=source)


def base_facts(**overrides):
    facts = [
        fact(EntityType.PROPERTY, "p1", "property.apn", text="APN-100", as_of=AS_OF),
        fact(EntityType.PROPERTY, "p1", "property.address", text="100 Main St", as_of=AS_OF),
        fact(EntityType.PROPERTY, "p1", "property.address.city", text="Testville", as_of=AS_OF),
        fact(EntityType.PROPERTY, "p1", "property.address.state", text="CA", as_of=AS_OF),
        fact(EntityType.PROPERTY, "p1", "property.address.zip5", text="90001", as_of=AS_OF),
        fact(EntityType.PROPERTY, "p1", "property.sqft", parsed=Decimal(1812), as_of=AS_OF),
        fact(EntityType.VALUATION, "v1", "valuation.type", text="comp", as_of=AS_OF),
        fact(EntityType.VALUATION, "v1", "valuation.value", parsed=Decimal(500000), as_of=AS_OF),
    ]
    return facts


def test_address_and_apn_resolution_without_placeholders():
    record = resolve_facts(PROPERTY_ID, base_facts(), as_of=AS_OF)
    assert record.apn == "APN-100"
    assert record.address.line1 == "100 Main St"
    assert record.address.city == "Testville" and record.address.zip5 == "90001"
    assert record.attributes.sqft.value == Decimal(1812)
    assert record.resolution_version == RESOLVER_VERSION
    assert record.data_quality.critical_field_coverage > 0


def test_empty_fact_list_yields_valid_sparse_record():
    record = resolve_facts(PROPERTY_ID, [], as_of=AS_OF)
    assert record.apn is None
    assert record.data_quality.critical_field_coverage == Decimal(0)
    assert any(f.type == FlagType.MISSING_APN for f in record.open_flags)


def test_human_override_always_wins_regardless_of_recency():
    facts = base_facts()
    facts.append(fact(EntityType.PROPERTY, "p1", "property.sqft", parsed=Decimal(2401),
                      conf=0.7, source=SourceKind.HUMAN, as_of=date(2010, 1, 1)))
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    assert record.attributes.sqft.value == Decimal(2401)
    assert record.attributes.sqft.source_kind == SourceKind.HUMAN
    assert record.data_quality.verified_field_count >= 1


def test_recency_wins_between_equal_sources_for_money():
    facts = base_facts()
    facts += [
        fact(EntityType.VALUATION, "v2", "valuation.type", text="avm"),
        fact(EntityType.VALUATION, "v2", "valuation.value", parsed=Decimal(510000),
             as_of=date(2025, 12, 15)),
        fact(EntityType.VALUATION, "v3", "valuation.type", text="avm"),
        fact(EntityType.VALUATION, "v3", "valuation.value", parsed=Decimal(480000),
             as_of=date(2023, 1, 1)),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    avm = next(c for c in record.valuation_candidates if c.valuation_type == "avm")
    assert avm.value.value == Decimal(510000)  # fresher candidate wins within its entity


def test_conservative_tie_break_picks_highest_liability_lowest_asset():
    # identical source/date/confidence -> exact score tie -> conservative choice
    day = date(2025, 6, 1)
    facts = [
        fact(EntityType.MORTGAGE, "m1", "mortgage.position", text="1", as_of=day),
        fact(EntityType.MORTGAGE, "m1", "mortgage.balance", parsed=Decimal(250000),
             conf=0.8, as_of=day, report=UUID(int=1)),
        fact(EntityType.MORTGAGE, "m1", "mortgage.balance", parsed=Decimal(265000),
             conf=0.8, as_of=day, report=UUID(int=2)),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    assert record.mortgages[0].estimated_balance.value == Decimal(265000)


def test_conflicting_mortgage_balances_raise_exactly_one_flag():
    day = date(2025, 6, 1)
    facts = [
        fact(EntityType.MORTGAGE, "m1", "mortgage.position", text="1", as_of=day),
        fact(EntityType.MORTGAGE, "m1", "mortgage.balance", parsed=Decimal(250000),
             conf=0.8, as_of=day, report=UUID(int=1)),
        fact(EntityType.MORTGAGE, "m1", "mortgage.balance", parsed=Decimal(265000),
             conf=0.8, as_of=day, report=UUID(int=2)),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    mortgage_flags = [f for f in record.open_flags if f.type == FlagType.CONFLICTING_MORTGAGE]
    assert len(mortgage_flags) == 1  # 6% apart, $15k spread
    assert mortgage_flags[0].financial_impact == Decimal(15000)
    assert record.data_quality.conflict_count == 1
    assert record.data_quality.material_conflict_count == 1


def test_balances_within_tolerance_do_not_flag():
    day = date(2025, 6, 1)
    facts = [
        fact(EntityType.MORTGAGE, "m1", "mortgage.balance", parsed=Decimal(250000),
             conf=0.8, as_of=day, report=UUID(int=1)),
        fact(EntityType.MORTGAGE, "m1", "mortgage.balance", parsed=Decimal(252000),
             conf=0.8, as_of=day, report=UUID(int=2)),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    assert not [f for f in record.open_flags if f.type == FlagType.CONFLICTING_MORTGAGE]
    assert record.data_quality.conflict_count == 0


def test_mortgage_dedupe_by_doc_number_and_lender_alias():
    facts = [
        fact(EntityType.MORTGAGE, "m1", "mortgage.lender", text="Wells Fargo Bank NA"),
        fact(EntityType.MORTGAGE, "m1", "mortgage.original_amount", parsed=Decimal(300000)),
        fact(EntityType.MORTGAGE, "m1", "mortgage.origination_date", when=date(2020, 6, 15)),
        fact(EntityType.MORTGAGE, "m1", "mortgage.doc_number", text="DOC-1"),
        fact(EntityType.MORTGAGE, "m2", "mortgage.lender", text="WELLS FARGO HOME MTG"),
        fact(EntityType.MORTGAGE, "m2", "mortgage.original_amount", parsed=Decimal(301000)),
        fact(EntityType.MORTGAGE, "m2", "mortgage.origination_date", when=date(2020, 7, 1)),
        fact(EntityType.MORTGAGE, "m2", "mortgage.doc_number", text="DOC-1"),
        fact(EntityType.MORTGAGE, "m3", "mortgage.lender", text="WELLS FARGO HOME MTG"),
        fact(EntityType.MORTGAGE, "m3", "mortgage.original_amount", parsed=Decimal(301000)),
        fact(EntityType.MORTGAGE, "m3", "mortgage.origination_date", when=date(2020, 7, 1)),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    assert len(record.mortgages) == 1  # doc-number match and alias+fuzzy match both merge
    assert record.mortgages[0].lender in ("Wells Fargo Bank NA", "WELLS FARGO HOME MTG")


def test_distinct_mortgages_are_not_merged():
    facts = [
        fact(EntityType.MORTGAGE, "m1", "mortgage.position", text="first"),
        fact(EntityType.MORTGAGE, "m1", "mortgage.lender", text="Bank of America NA"),
        fact(EntityType.MORTGAGE, "m1", "mortgage.original_amount", parsed=Decimal(300000)),
        fact(EntityType.MORTGAGE, "m2", "mortgage.position", text="second"),
        fact(EntityType.MORTGAGE, "m2", "mortgage.lender", text="Nationstar Mortgage"),
        fact(EntityType.MORTGAGE, "m2", "mortgage.original_amount", parsed=Decimal(50000)),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    assert [m.position for m in record.mortgages] == ["1", "2"]


def test_derived_balance_uses_spec_amortization():
    facts = [
        fact(EntityType.MORTGAGE, "m1", "mortgage.position", text="1"),
        fact(EntityType.MORTGAGE, "m1", "mortgage.original_amount", parsed=Decimal(300000),
             as_of=date(2021, 1, 1)),
        fact(EntityType.MORTGAGE, "m1", "mortgage.rate", parsed=Decimal("6.5")),
        fact(EntityType.MORTGAGE, "m1", "mortgage.term_months", parsed=Decimal(360)),
        fact(EntityType.MORTGAGE, "m1", "mortgage.origination_date", when=date(2021, 1, 1)),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    mortgage = record.mortgages[0]
    assert mortgage.balance_method == "amortization_v1"
    assert mortgage.estimated_balance.source_kind == SourceKind.DERIVED
    assert mortgage.estimated_balance.is_estimated is True
    assert mortgage.rate == Decimal("0.065")  # percent normalized to a fraction
    expected = _amortized_balance(Decimal(300000), Decimal("0.065"), 360, date(2021, 1, 1), AS_OF)
    assert mortgage.estimated_balance.value == expected
    assert Decimal(270000) < mortgage.estimated_balance.value < Decimal(300000)


def test_derived_balance_without_rate_uses_historical_index():
    facts = [
        fact(EntityType.MORTGAGE, "m1", "mortgage.original_amount", parsed=Decimal(200000)),
        fact(EntityType.MORTGAGE, "m1", "mortgage.origination_date", when=date(2020, 3, 1)),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    balance = record.mortgages[0].estimated_balance
    assert balance is not None and balance.confidence == pytest.approx(0.55)
    assert balance.value < Decimal(200000)


def test_finance_estimate_balance_is_preferred_when_available(monkeypatch):
    fake = types.ModuleType("finance")
    fake.estimate_balance = lambda original, rate, term, start, as_of: Decimal("12345.67")
    monkeypatch.setitem(sys.modules, "finance", fake)
    facts = [
        fact(EntityType.MORTGAGE, "m1", "mortgage.original_amount", parsed=Decimal(200000)),
        fact(EntityType.MORTGAGE, "m1", "mortgage.rate", parsed=Decimal("0.05")),
        fact(EntityType.MORTGAGE, "m1", "mortgage.origination_date", when=date(2020, 3, 1)),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    assert record.mortgages[0].estimated_balance.value == Decimal("12345.67")


def test_finance_estimate_balance_failure_degrades_gracefully(monkeypatch):
    fake = types.ModuleType("finance")

    def boom(*args):
        raise RuntimeError("in-flight rewrite")

    fake.estimate_balance = boom
    monkeypatch.setitem(sys.modules, "finance", fake)
    facts = [
        fact(EntityType.MORTGAGE, "m1", "mortgage.original_amount", parsed=Decimal(200000)),
        fact(EntityType.MORTGAGE, "m1", "mortgage.rate", parsed=Decimal("0.05")),
        fact(EntityType.MORTGAGE, "m1", "mortgage.origination_date", when=date(2020, 3, 1)),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    balance = record.mortgages[0].estimated_balance
    assert balance is not None and balance.value < Decimal(200000)


def test_released_lien_retained_and_owner_only_basis_preserved():
    facts = base_facts() + [
        fact(EntityType.LIEN, "l1", "lien.type", text="judgment"),
        fact(EntityType.LIEN, "l1", "lien.amount", parsed=Decimal(18000)),
        fact(EntityType.LIEN, "l1", "lien.status", text="released"),
        fact(EntityType.LIEN, "l1", "lien.attachment_basis", text="recorded_against_property"),
        fact(EntityType.LIEN, "l2", "lien.type", text="federal_tax"),
        fact(EntityType.LIEN, "l2", "lien.amount", parsed=Decimal(128000)),
        fact(EntityType.LIEN, "l2", "lien.attachment_basis", text="owner_named_only"),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    by_type = {lien.lien_type: lien for lien in record.liens}
    assert by_type["judgment"].status == "released"  # retained in the record, not dropped
    assert by_type["federal_tax"].attachment_basis.value == "owner_named_only"


def test_lien_dedupe_merges_duplicates():
    facts = [
        fact(EntityType.LIEN, "l1", "lien.type", text="hoa"),
        fact(EntityType.LIEN, "l1", "lien.creditor", text="Sunset HOA"),
        fact(EntityType.LIEN, "l1", "lien.amount", parsed=Decimal(6000)),
        fact(EntityType.LIEN, "l1", "lien.recording_date", when=date(2025, 5, 1)),
        fact(EntityType.LIEN, "l2", "lien.type", text="hoa"),
        fact(EntityType.LIEN, "l2", "lien.creditor", text="Sunset  HOA"),
        fact(EntityType.LIEN, "l2", "lien.amount", parsed=Decimal("6000.00")),
        fact(EntityType.LIEN, "l2", "lien.recording_date", when=date(2025, 5, 3)),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    assert len(record.liens) == 1


def test_active_lien_without_amount_raises_missing_amount_flag():
    facts = [fact(EntityType.LIEN, "l1", "lien.type", text="mechanic")]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    assert record.liens[0].amount is None
    assert any(f.type == FlagType.MISSING_LIEN_AMOUNT for f in record.open_flags)


def test_valuation_dispersion_flag_above_ratio():
    facts = base_facts() + [
        fact(EntityType.VALUATION, "v2", "valuation.type", text="avm"),
        fact(EntityType.VALUATION, "v2", "valuation.value", parsed=Decimal(800000), as_of=AS_OF),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    assert any(f.type == FlagType.VALUATION_DISPERSION for f in record.open_flags)


def test_identity_conflict_is_gating():
    facts = base_facts() + [
        fact(EntityType.PROPERTY, "p2", "property.apn", text="APN-999", report=uuid4()),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    flags = [f for f in record.open_flags if f.type == FlagType.IDENTITY_CONFLICT]
    assert len(flags) == 1 and flags[0].is_gating is True


def test_foreclosure_merges_events_and_counts_postponements():
    facts = [
        fact(EntityType.FORECLOSURE, "f1", "foreclosure.stage", text="nod"),
        fact(EntityType.FORECLOSURE, "f1", "foreclosure.nod_date", when=date(2025, 3, 1)),
        fact(EntityType.FORECLOSURE, "f2", "foreclosure.stage", text="nts"),
        fact(EntityType.FORECLOSURE, "f2", "foreclosure.nts_date", when=date(2025, 6, 1)),
        fact(EntityType.FORECLOSURE, "f2", "foreclosure.sale_date", when=date(2025, 9, 1)),
        fact(EntityType.FORECLOSURE, "f3", "foreclosure.event_type", text="postponement",
             when=date(2025, 9, 2)),
        fact(EntityType.FORECLOSURE, "f3", "foreclosure.sale_date", when=date(2025, 10, 1)),
        fact(EntityType.FORECLOSURE, "f4", "foreclosure.event_type", text="postponement",
             when=date(2025, 10, 2)),
        fact(EntityType.FORECLOSURE, "f4", "foreclosure.sale_date", when=date(2025, 11, 1)),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    foreclosure = record.foreclosure
    assert foreclosure.stage == "nts"
    assert foreclosure.current_sale_date == date(2025, 11, 1)  # latest recorded wins
    assert foreclosure.original_sale_date == date(2025, 9, 1)
    assert foreclosure.postponement_count == 2
    assert foreclosure.is_active is True


def test_bid_mismatch_flag_above_twenty_percent():
    facts = [
        fact(EntityType.MORTGAGE, "m1", "mortgage.position", text="1"),
        fact(EntityType.MORTGAGE, "m1", "mortgage.balance", parsed=Decimal(250000), as_of=AS_OF),
        fact(EntityType.FORECLOSURE, "f1", "foreclosure.stage", text="nts"),
        fact(EntityType.FORECLOSURE, "f1", "foreclosure.published_bid", parsed=Decimal(180000),
             as_of=AS_OF),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    flags = [f for f in record.open_flags if f.type == FlagType.BID_MISMATCH]
    assert len(flags) == 1 and flags[0].financial_impact == Decimal(70000)


def test_full_property_builds_every_block():
    facts = base_facts() + [
        fact(EntityType.PROPERTY, "p1", "property.beds", parsed=Decimal(3)),
        fact(EntityType.PROPERTY, "p1", "property.baths", parsed=Decimal(2)),
        fact(EntityType.PROPERTY, "p1", "property.year_built", parsed=Decimal(1985)),
        fact(EntityType.PROPERTY, "p1", "property.owner_name", text="Jane Doe"),
        fact(EntityType.PROPERTY, "p1", "property.is_owner_occupied", flag=True),
        fact(EntityType.PROPERTY, "p1", "property.hoa_monthly_dues", parsed=Decimal(450)),
        fact(EntityType.PROPERTY, "p1", "property.hoa_arrears", parsed=Decimal(6000)),
        fact(EntityType.PROPERTY, "p1", "property.hoa_has_lien", flag=True),
        fact(EntityType.MORTGAGE, "m1", "mortgage.position", text="1"),
        fact(EntityType.MORTGAGE, "m1", "mortgage.balance", parsed=Decimal(250000), as_of=AS_OF),
        fact(EntityType.LIEN, "l1", "lien.type", text="judgment"),
        fact(EntityType.LIEN, "l1", "lien.amount", parsed=Decimal(18000)),
        fact(EntityType.LIEN, "l1", "lien.attachment_basis", text="recorded_against_property"),
        fact(EntityType.TAX, "t1", "tax.annual_taxes", parsed=Decimal(5200)),
        fact(EntityType.TAX, "t1", "tax.assessed_value", parsed=Decimal(410000)),
        fact(EntityType.RENTAL, "r1", "rental.rent", parsed=Decimal(2600)),
        fact(EntityType.RENTAL, "r1", "rental.source", text="avm"),
        fact(EntityType.CONDITION, "c1", "condition.condition", text="Moderate"),
        fact(EntityType.BANKRUPTCY, "b1", "bankruptcy.chapter", text="13"),
        fact(EntityType.BANKRUPTCY, "b1", "bankruptcy.status", text="active"),
        fact(EntityType.BANKRUPTCY, "b1", "bankruptcy.filing_date", when=date(2025, 2, 1)),
        fact(EntityType.LISTING, "ls1", "listing.list_date", when=date(2024, 4, 1)),
        fact(EntityType.LISTING, "ls1", "listing.list_price", parsed=Decimal(525000)),
        fact(EntityType.LISTING, "ls1", "listing.status", text="delisted"),
        fact(EntityType.COMP, "cp1", "comp.address", text="102 Main St"),
        fact(EntityType.COMP, "cp1", "comp.sale_price", parsed=Decimal(495000)),
        fact(EntityType.COMP, "cp1", "comp.sale_date", when=date(2025, 8, 1)),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    assert record.attributes.beds.value == Decimal(3)
    assert record.ownership.owner_names == ["Jane Doe"]
    assert record.ownership.is_owner_occupied is True
    assert record.hoa.has_lien is True and record.hoa.arrears.value == Decimal(6000)
    assert record.taxes.annual_taxes.value == Decimal(5200)
    assert record.rental.rent_estimate.value == Decimal(2600) and record.rental.source == "avm"
    assert record.condition.condition == "moderate"
    assert record.bankruptcies[0].chapter == "13"
    assert record.listings[0].status == "delisted"
    assert record.comparables[0].address == "102 Main St"
    quality = record.data_quality
    assert quality.critical_field_coverage == Decimal("0.6818")  # 15 of 22 critical fields
    assert quality.source_counts_by_field  # per-field source counts populated
    assert quality.mean_extraction_confidence == Decimal("0.9")


def test_data_quality_tracks_conflicts_and_newest_date():
    facts = base_facts() + [
        fact(EntityType.MORTGAGE, "m1", "mortgage.balance", parsed=Decimal(250000),
             conf=0.8, as_of=date(2025, 6, 1), report=UUID(int=1)),
        fact(EntityType.MORTGAGE, "m1", "mortgage.balance", parsed=Decimal(265000),
             conf=0.8, as_of=date(2025, 7, 1), report=UUID(int=2)),
    ]
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    assert record.data_quality.conflict_count == 1
    assert record.data_quality.newest_report_date == AS_OF
    assert record.data_quality.ocr_applied is False


def test_low_extraction_confidence_flag():
    facts = base_facts()
    facts.append(fact(EntityType.TAX, "t1", "tax.annual_taxes", parsed=Decimal(5200), conf=0.5))
    record = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    assert any(f.type == FlagType.LOW_EXTRACTION_CONFIDENCE for f in record.open_flags)


def test_idempotent_byte_identical_output():
    facts = base_facts() + [
        fact(EntityType.MORTGAGE, "m1", "mortgage.position", text="1"),
        fact(EntityType.MORTGAGE, "m1", "mortgage.balance", parsed=Decimal(250000), as_of=AS_OF),
        fact(EntityType.LIEN, "l1", "lien.type", text="judgment"),
        fact(EntityType.LIEN, "l1", "lien.amount", parsed=Decimal(18000)),
        fact(EntityType.LIEN, "l1", "lien.attachment_basis", text="owner_named_only"),
        fact(EntityType.TAX, "t1", "tax.annual_taxes", parsed=Decimal(5200)),
    ]
    first = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    second = resolve_facts(PROPERTY_ID, facts, as_of=AS_OF)
    assert first.model_dump_json() == second.model_dump_json()


def test_normalize_source_kind_caps_unchanged():
    assert normalize_source_kind(SourceKind.HUMAN) == 1.0
    assert normalize_source_kind(SourceKind.API) == 0.9
    assert normalize_source_kind(SourceKind.REPORT) == 0.7
    assert normalize_source_kind(SourceKind.DERIVED) == 0.5
    assert normalize_source_kind(SourceKind.PASTED) == 0.45
    assert normalize_source_kind(SourceKind.HUMAN, ocr_applied=True) == 0.8
