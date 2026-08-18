"""Validation and deterministic normalization for canonical report extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from contracts import (
    AddressBlock,
    AttachmentBasis,
    DataQualityBlock,
    ForeclosureState,
    LiabilityBlock,
    LienRecord,
    ListingRecord,
    MortgageRecord,
    NormalizedProperty,
    OwnershipBlock,
    PropertyAttributes,
    RentalBlock,
    SourceKind,
    TaxBlock,
    TrackedValue,
    UnderwritingResult,
    ValuationCandidate,
)
from identity.service import normalize_address

from .schemas import PropertyReportExtraction

RESOLUTION_VERSION = "whole-pdf-v1"

_STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE",
    "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
    "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
}

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y")

_NONNEGATIVE_PATHS = {
    "property_details.beds", "property_details.baths", "property_details.sq_ft",
    "property_details.lot_sq_ft", "property_details.lot_acres", "property_details.units",
    "property_details.garage_spaces", "ownership.purchase_amount",
    "valuation.estimated_value", "valuation.assessed_value", "valuation.land_value",
    "valuation.improvement_value", "valuation.comparable_sales_value",
    "valuation.comparable_listing_value", "tax.annual_taxes", "loans.original_amount",
    "loans.estimated_balance", "liens.amount", "foreclosure.published_bid",
    "foreclosure.opening_bid", "foreclosure.winning_bid", "foreclosure.default_amount",
    "transaction_history.amount", "listing_history.price", "rental.estimated_rent",
    "rental.rent_per_sq_ft",
}


@dataclass(frozen=True)
class CanonicalValidation:
    extraction: PropertyReportExtraction
    normalized_source: dict
    issues: list[dict[str, Any]]


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    clean = re.sub(r"\s+", " ", value).strip()
    return clean or None


def _date_value(value: str | None, path: str, issues: list[dict]) -> str | None:
    value = _clean_text(value)
    if value is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC).date().isoformat()
        except ValueError:
            pass
    issues.append({"code": "invalid_date", "path": path, "value": value})
    return None


def _normalize_state(value: str | None, issues: list[dict]) -> str | None:
    value = _clean_text(value)
    if value is None:
        return None
    normalized = _STATE_NAMES.get(value.upper(), value.upper())
    if not re.fullmatch(r"[A-Z]{2}", normalized):
        issues.append({"code": "invalid_state", "path": "property_identity.state", "value": value})
        return None
    return normalized


def _normalize_zip(value: str | None, issues: list[dict]) -> str | None:
    value = _clean_text(value)
    if value is None:
        return None
    match = re.search(r"\b(\d{5})(?:-\d{4})?\b", value)
    if match is None:
        issues.append({"code": "invalid_zip", "path": "property_identity.zip5", "value": value})
        return None
    return match.group(1)


def _normalize_dates(payload: dict, issues: list[dict]) -> None:
    scalar_dates = (
        ("ownership", "transfer_date"),
        ("valuation", "estimated_value_as_of"),
        ("foreclosure", "current_sale_date"),
        ("foreclosure", "original_sale_date"),
    )
    for block, key in scalar_dates:
        payload[block][key] = _date_value(payload[block].get(key), f"{block}.{key}", issues)
    for collection, key in (
        ("loans", "recorded_date"),
        ("liens", "recorded_date"),
        ("transaction_history", "date"),
        ("listing_history", "as_of"),
        ("additional_facts", "date_value"),
    ):
        for index, row in enumerate(payload[collection]):
            row[key] = _date_value(row.get(key), f"{collection}.{index}.{key}", issues)


def _check_nonnegative(payload: dict, issues: list[dict]) -> None:
    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            collection_path = path
            for index, child in enumerate(value):
                visit(child, f"{collection_path}.{index}")
        elif isinstance(value, (int, float)):
            comparable = re.sub(r"\.\d+\.", ".", path)
            if value < 0 and comparable in _NONNEGATIVE_PATHS:
                issues.append({"code": "negative_value_rejected", "path": path, "value": value})
                parent = payload
                parts = path.split(".")
                for part in parts[:-1]:
                    parent = parent[int(part)] if part.isdigit() else parent[part]
                parent[parts[-1]] = None

    visit(payload, "")


def validate_and_normalize(payload: dict) -> CanonicalValidation:
    """Strict shape validation followed by conservative source normalization."""
    extraction = PropertyReportExtraction.model_validate(payload)
    normalized = extraction.model_dump(mode="json")
    issues: list[dict[str, Any]] = []
    identity = normalized["property_identity"]
    for key in identity:
        identity[key] = _clean_text(identity[key])
    identity["state"] = _normalize_state(identity["state"], issues)
    identity["zip5"] = _normalize_zip(identity["zip5"], issues)
    if identity["apn"] is not None:
        identity["apn"] = identity["apn"].strip()
    _normalize_dates(normalized, issues)
    _check_nonnegative(normalized, issues)
    return CanonicalValidation(
        PropertyReportExtraction.model_validate(normalized), normalized, issues,
    )


def identity_address(extraction: PropertyReportExtraction) -> str | None:
    identity = extraction.property_identity
    address = _clean_text(identity.address_line1) or _clean_text(identity.full_address)
    if address is None:
        return None
    normalized = normalize_address(address, identity.zip5)
    street = normalized.address_key.split("|", 3)[1]
    if not normalized.house_number or not street or street == "PO BOX":
        return None
    return address


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _tracked(
    value: float | None, *, confidence: float = 0.85,
    as_of: date | None = None, estimated: bool = False,
) -> TrackedValue | None:
    amount = _decimal(value)
    if amount is None:
        return None
    return TrackedValue(
        value=amount, confidence=confidence, source_kind=SourceKind.REPORT,
        is_estimated=estimated, as_of=as_of,
    )


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _confidence(value: float | None, fallback: float = 0.85) -> float:
    return fallback if value is None else value


def canonical_to_normalized(
    extraction: PropertyReportExtraction, property_id: UUID,
) -> NormalizedProperty:
    """Map source facts into the existing deterministic analysis contract."""
    identity = extraction.property_identity
    details = extraction.property_details
    valuation = extraction.valuation
    address = AddressBlock(
        line1=identity.address_line1 or identity.full_address,
        city=identity.city, state=identity.state, zip5=identity.zip5,
        county=identity.county, fips=identity.fips,
    )
    attributes = PropertyAttributes(
        beds=_tracked(details.beds), baths=_tracked(details.baths),
        sqft=_tracked(details.sq_ft), lot_sqft=_tracked(details.lot_sq_ft),
        year_built=_tracked(details.year_built), units=_tracked(details.units),
    )
    ownership = OwnershipBlock(
        owner_names=extraction.ownership.owner_names,
        is_owner_occupied=extraction.ownership.owner_occupied,
        ownership_start_date=_date(extraction.ownership.transfer_date),
        purchase_price=_tracked(extraction.ownership.purchase_amount),
    )
    candidates: list[ValuationCandidate] = []
    for kind, value, estimated in (
        ("avm", valuation.estimated_value, True),
        ("assessed", valuation.assessed_value, False),
        ("comp", valuation.comparable_sales_value, False),
        ("comp_listing", valuation.comparable_listing_value, False),
    ):
        tracked = _tracked(
            value,
            confidence=_confidence(valuation.estimated_value_confidence)
            if kind == "avm" else 0.8,
            as_of=_date(valuation.estimated_value_as_of) if kind == "avm" else None,
            estimated=estimated,
        )
        if tracked is not None:
            candidates.append(ValuationCandidate(
                valuation_type=kind, value=tracked, as_of=tracked.as_of,
                reported_confidence=tracked.confidence,
            ))
    mortgages = [MortgageRecord(
        position=str(loan.position) if loan.position is not None else "unknown",
        lender=loan.lender,
        original_amount=_tracked(loan.original_amount, confidence=_confidence(loan.confidence)),
        origination_date=_date(loan.recorded_date),
        estimated_balance=_tracked(
            loan.estimated_balance, confidence=_confidence(loan.confidence),
        ),
        balance_method="reported",
        is_open=(loan.status or "unknown").casefold() not in {
            "closed", "paid", "released", "satisfied",
        },
    ) for loan in extraction.loans]
    liens = [LienRecord(
        lien_type=(lien.type or "other").casefold(),
        amount=_tracked(lien.amount, confidence=_confidence(lien.confidence)),
        status=(lien.status or "unknown").casefold(),
        attachment_basis=AttachmentBasis.RECORDED_AGAINST_PROPERTY,
        attachment_confidence=_confidence(lien.confidence),
        recording_date=_date(lien.recorded_date),
    ) for lien in extraction.liens]
    foreclosure = None
    source_foreclosure = extraction.foreclosure
    if any(value is not None for value in source_foreclosure.model_dump().values()):
        stage = source_foreclosure.stage or "unknown"
        active = source_foreclosure.in_foreclosure
        if active is None:
            active = stage.casefold() not in {"unknown", "none", "rescinded", "cancelled", "sold"}
        foreclosure = ForeclosureState(
            stage=stage.casefold(),
            original_sale_date=_date(source_foreclosure.original_sale_date),
            current_sale_date=_date(source_foreclosure.current_sale_date),
            published_bid=_tracked(
                source_foreclosure.published_bid,
                confidence=_confidence(source_foreclosure.confidence),
            ),
            default_amount=_tracked(
                source_foreclosure.default_amount,
                confidence=_confidence(source_foreclosure.confidence),
            ),
            trustee=source_foreclosure.trustee,
            is_active=active,
        )
    listings = [ListingRecord(
        list_date=_date(row.as_of), price=_tracked(row.price, confidence=_confidence(row.confidence)),
        status=row.status or row.type or "unknown", dom=row.dom,
    ) for row in extraction.listing_history if row.as_of]
    taxes = TaxBlock(
        annual_taxes=_tracked(extraction.tax.annual_taxes),
        assessed_value=_tracked(valuation.assessed_value),
    )
    rental = RentalBlock(rent_estimate=_tracked(extraction.rental.estimated_rent), source="report")

    presence = {
        "apn": identity.apn is not None,
        "address": address.line1 is not None,
        "sqft": details.sq_ft is not None,
        "beds": details.beds is not None,
        "baths": details.baths is not None,
        "year_built": details.year_built is not None,
        "owner": bool(extraction.ownership.owner_names),
        "purchase_price": extraction.ownership.purchase_amount is not None,
        "valuation": bool(candidates),
        "annual_taxes": extraction.tax.annual_taxes is not None,
        "mortgage": bool(mortgages),
        "mortgage_balance": any(m.estimated_balance is not None for m in mortgages),
        "foreclosure": foreclosure is not None,
        "liens": bool(liens),
        "rent": extraction.rental.estimated_rent is not None,
    }
    confidences = [
        value for value in (
            valuation.estimated_value_confidence,
            source_foreclosure.confidence,
            *(loan.confidence for loan in extraction.loans),
            *(lien.confidence for lien in extraction.liens),
            *(ref.confidence for ref in extraction.source_references),
        ) if value is not None
    ]
    relevant_dates = [
        value for value in (
            _date(extraction.ownership.transfer_date),
            _date(valuation.estimated_value_as_of),
            _date(source_foreclosure.current_sale_date),
            _date(source_foreclosure.original_sale_date),
            *(_date(row.date) for row in extraction.transaction_history),
        ) if value is not None
    ]
    quality = DataQualityBlock(
        critical_field_coverage=(
            Decimal(sum(presence.values())) / Decimal(len(presence))
        ).quantize(Decimal("0.0001")),
        source_counts_by_field={key: 1 for key, present in presence.items() if present},
        newest_report_date=max(relevant_dates, default=None),
        mean_extraction_confidence=Decimal(str(sum(confidences) / len(confidences))).quantize(
            Decimal("0.0001")
        ) if confidences else Decimal("0.5000"),
    )
    return NormalizedProperty(
        property_id=property_id, apn=identity.apn, address=address,
        attributes=attributes, ownership=ownership, valuation_candidates=candidates,
        mortgages=mortgages, liens=liens, foreclosure=foreclosure, taxes=taxes,
        rental=rental, listings=listings, data_quality=quality,
        resolution_version=RESOLUTION_VERSION,
    )


def has_grounded_debt(record: NormalizedProperty) -> bool:
    """Whether the source supplied a usable debt/payoff amount.

    An empty collection means no supported debt evidence was found; it does
    not prove that the property has zero debt.
    """
    for mortgage in record.mortgages:
        if mortgage.estimated_balance and mortgage.estimated_balance.value is not None:
            return True
        if (mortgage.original_amount and mortgage.original_amount.value is not None
                and mortgage.origination_date is not None):
            return True
    if any(lien.amount and lien.amount.value is not None for lien in record.liens):
        return True
    foreclosure = record.foreclosure
    return bool(
        foreclosure and (
            (foreclosure.published_bid and foreclosure.published_bid.value is not None)
            or (foreclosure.default_amount and foreclosure.default_amount.value is not None)
        )
    )


def underwrite_canonical(record: NormalizedProperty, assumptions) -> UnderwritingResult:
    """Run legacy finance math with a null-safe debt boundary for canonical data."""
    from finance import underwrite

    result = underwrite(record, assumptions)
    if has_grounded_debt(record) or result.status != "ok":
        return result
    return result.model_copy(update={
        "status": "insufficient_data",
        "unavailable_reason": "missing_debt_data",
        "liabilities": LiabilityBlock(
            confirmed=None, potential=None, maximum=None, breakdown=[],
        ),
        "equity": {},
    })
