from decimal import Decimal
from uuid import uuid4

from contracts import (AcquisitionCosts, AddressBlock, AssumptionSet, AttachmentBasis, DataQualityBlock,
                       HoldingAssumptions, LienRecord, NormalizedProperty, PropertyAttributes,
                       RepairAssumptions, ResaleAssumptions, Scenario, StrategyAssumptions,
                       TrackedValue, SourceKind, ValuationCandidate)


def assumptions():
    return AssumptionSet(id=uuid4(), version=1, name="test", acquisition=AcquisitionCosts(closing_pct=Decimal(".01"), title_pct=Decimal(".005"), escrow_flat=Decimal("1500"), financing_points=Decimal(".02"), financing_flat=Decimal("1200"), inspection_flat=Decimal("600"), legal_flat=Decimal("1500"), acq_fee_pct=Decimal(".01")), repairs=RepairAssumptions(psf_by_condition={"moderate": Decimal("42")}, low_multiplier=Decimal(".75"), high_multiplier=Decimal("1.4"), regional_index=Decimal("1")), holding=HoldingAssumptions(insurance_pct_yr=Decimal(".0035"), utilities_monthly=Decimal("180"), maintenance_pct_yr=Decimal(".005"), acquisition_months=Decimal("2"), repair_months_by_condition={"moderate": Decimal("4")}, market_days_default=60), resale=ResaleAssumptions(commission_pct=Decimal(".05"), seller_closing_pct=Decimal(".01"), concessions_pct=Decimal(".01"), staging_flat=Decimal("3500"), misc_pct=Decimal(".0025")), strategy=StrategyAssumptions(cash_target_margin=Decimal(".2"), flip_target_margin_by_arv_band={"default": Decimal(".2")}, wholesale_investor_pct=Decimal(".7"), min_assignment_spread=Decimal("15000"), hard_money={"rate": Decimal(".1"), "points": Decimal(".02"), "ltv": Decimal(".85")}, rental={"vacancy": Decimal(".06"), "maintenance_pct": Decimal(".08"), "management_pct": Decimal(".08")}), attachment_probability={AttachmentBasis.OWNER_NAMED_ONLY: Decimal(".35"), AttachmentBasis.UNKNOWN: Decimal(".5")}, unknown_lien_medians={"judgment": Decimal("18000")}, valuation_weights={"manual": Decimal("1"),})
from finance import underwrite
from strategies import flip, offer_grid


def sample_property() -> NormalizedProperty:
    value = TrackedValue(value=Decimal("300000"), confidence=Decimal(".9"), source_kind=SourceKind.REPORT, is_estimated=False)
    return NormalizedProperty(property_id=uuid4(), address=AddressBlock(line1="1 Main St"), attributes=PropertyAttributes(),
                                  valuation_candidates=[ValuationCandidate(valuation_type="manual", value=value)],
                                  data_quality=DataQualityBlock(critical_field_coverage=Decimal(".9"), mean_extraction_confidence=Decimal(".9")), resolution_version="test")


def test_underwriting_and_strategy_are_repeatable():
    property = sample_property()
    assumption_set = assumptions()
    first = underwrite(property, assumption_set)
    second = underwrite(property, assumption_set)
    assert first.model_dump() == second.model_dump()
    result = flip(property, first, assumption_set, Decimal("150000"), Scenario.EXPECTED)
    assert result.profit is not None
    grid = offer_grid(first, property.property_id, assumption_set, Decimal("150000"))
    assert grid.points


def test_lien_attachment_split_uses_enum_members():
    property = sample_property()
    amount = TrackedValue(value=Decimal("10000"), confidence=.9, source_kind=SourceKind.REPORT, is_estimated=False)
    property.liens = [
        LienRecord(lien_type="tax", amount=amount, attachment_basis=AttachmentBasis.RECORDED_AGAINST_PROPERTY, attachment_confidence=.9),
        LienRecord(lien_type="judgment", amount=amount, attachment_basis=AttachmentBasis.OWNER_NAMED_ONLY, attachment_confidence=.9),
        LienRecord(lien_type="other", amount=amount, attachment_basis=AttachmentBasis.UNKNOWN, attachment_confidence=.5),
    ]
    result = underwrite(property, assumptions())
    assert result.liabilities.confirmed == Decimal("10000")
    assert result.liabilities.potential == Decimal("20000")
