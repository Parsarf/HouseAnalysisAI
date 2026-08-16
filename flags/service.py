from decimal import Decimal

from contracts import AttachmentBasis, FlagRequest, FlagType, NormalizedProperty


def collect_flags(property: NormalizedProperty) -> list[FlagRequest]:
    flags = []
    for index, lien in enumerate(property.liens):
        amount = lien.amount.value if lien.amount and lien.amount.value is not None else Decimal("0")
        if lien.attachment_basis != AttachmentBasis.RECORDED_AGAINST_PROPERTY and amount >= Decimal("10000"):
            flags.append(FlagRequest(property_id=property.property_id, flag_type=FlagType.LIEN_ATTACHMENT, payload={"index": index, "basis": lien.attachment_basis.value}, financial_impact_usd=amount, raised_by="flags", dedupe_key=f"lien-attachment:{lien.lien_type}:{amount}"))
    if property.data_quality.material_conflict_count:
        flags.append(FlagRequest(property_id=property.property_id, flag_type=FlagType.CONFLICTING_MORTGAGE, payload={"count": property.data_quality.material_conflict_count}, financial_impact_usd=None, raised_by="flags", dedupe_key="material-conflict"))
    return flags
