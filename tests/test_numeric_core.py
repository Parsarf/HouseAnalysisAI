from decimal import Decimal
from uuid import uuid4

from contracts import AddressBlock, AssumptionSet, DataQualityBlock, NormalizedProperty, PropertyAttributes, Scenario, TrackedValue, SourceKind, ValuationCandidate
from finance import underwrite
from strategies import flip, offer_grid


def sample_property() -> NormalizedProperty:
    value = TrackedValue(value=Decimal("300000"), confidence=Decimal(".9"), source_kind=SourceKind.REPORT, is_estimated=False)
    return NormalizedProperty(property_id=uuid4(), address=AddressBlock(line1="1 Main St"), attributes=PropertyAttributes(),
                                  valuation_candidates=[ValuationCandidate(valuation_type="manual", value=value)],
                                  data_quality=DataQualityBlock(critical_field_coverage=Decimal(".9"), mean_extraction_confidence=Decimal(".9")), resolution_version="test")


def test_underwriting_and_strategy_are_repeatable():
    property = sample_property()
    assumptions = AssumptionSet(id=uuid4(), version=1, name="test")
    first = underwrite(property, assumptions)
    second = underwrite(property, assumptions)
    assert first.model_dump() == second.model_dump()
    result = flip(first, assumptions, Decimal("150000"), Scenario.EXPECTED)
    assert result.profit is not None
    grid = offer_grid(first, property.property_id, assumptions, Decimal("150000"))
    assert grid.points
