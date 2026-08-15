from collections import defaultdict
from decimal import Decimal
from uuid import uuid4

from contracts import AddressBlock, ExtractedFactDraft, FullNormalizedProperty, PropertyAttributes, SourceKind


def resolve_facts(property_id, facts: list[ExtractedFactDraft]) -> FullNormalizedProperty:
    grouped = defaultdict(list)
    for fact in facts:
        grouped[fact.field_path].append(fact)
    def latest(path: str):
        values = grouped.get(path, [])
        return max(values, key=lambda item: item.extraction_confidence) if values else None
    address = AddressBlock(line1=(latest("property.address") or ExtractedFactDraft.model_construct(value_text=None)).value_text)
    return FullNormalizedProperty(property_id=property_id, apn=(latest("property.apn").value_text if latest("property.apn") else None), address=address, resolution_version="resolver-1")


def normalize_source_kind(source_kind: SourceKind, ocr_applied: bool = False) -> float:
    cap = {SourceKind.HUMAN: 1.0, SourceKind.API: .9, SourceKind.REPORT: .7, SourceKind.DERIVED: .5, SourceKind.PASTED: .45}[source_kind]
    return min(cap, .8) if ocr_applied else cap
