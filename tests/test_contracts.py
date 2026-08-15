from decimal import Decimal
from uuid import uuid4

import pytest

from contracts import (AddressBlock, AttachmentBasis, ExtractedFactDraft,
                       EntityType, NullReason, SourceKind, TrackedValue)


def test_null_tracked_value_requires_reason():
    with pytest.raises(ValueError):
        TrackedValue(value=None, confidence=0, source_kind=SourceKind.REPORT, is_estimated=False)


def test_fact_preserves_report_provenance():
    fact = ExtractedFactDraft(
        report_id=uuid4(), extraction_unit_id=uuid4(), entity_type=EntityType.LIEN,
        entity_local_id="lien-1", field_path="liens[0].amount", value_raw="$10,000",
        value_parsed=Decimal("10000"), page_number=2, snippet="recorded amount $10,000",
        extraction_confidence=.9,
    )
    assert fact.source_kind == SourceKind.REPORT


def test_owner_only_attachment_is_distinct():
    assert AttachmentBasis.OWNER_NAMED_ONLY != AttachmentBasis.RECORDED_AGAINST_PROPERTY
