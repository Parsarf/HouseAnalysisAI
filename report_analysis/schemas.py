"""One strict, nullable canonical schema for whole-property reports."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "property-report-v1"
OWNER_SCHEMA_VERSION = "owner-profile-v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PropertyIdentity(StrictModel):
    address_line1: str | None
    city: str | None
    state: str | None
    zip5: str | None
    full_address: str | None
    apn: str | None
    county: str | None
    fips: str | None


class PropertyDetails(StrictModel):
    property_type: str | None
    beds: float | None
    baths: float | None
    sq_ft: float | None
    lot_sq_ft: float | None
    lot_acres: float | None
    year_built: int | None
    units: int | None
    garage_spaces: float | None
    zoning: str | None
    subdivision: str | None
    legal_description: str | None


class Ownership(StrictModel):
    owner_names: list[str]
    mailing_address: str | None
    transfer_date: str | None
    purchase_amount: float | None
    transfer_type: str | None
    owner_occupied: bool | None


class Valuation(StrictModel):
    estimated_value: float | None
    estimated_value_as_of: str | None
    estimated_value_confidence: float | None = Field(ge=0, le=1)
    assessed_value: float | None
    land_value: float | None
    improvement_value: float | None
    comparable_sales_value: float | None
    comparable_listing_value: float | None
    reported_equity: float | None


class Tax(StrictModel):
    annual_taxes: float | None
    tax_rate: float | None
    tax_year: int | None
    tax_rate_area: str | None


class Loan(StrictModel):
    position: int | None
    original_amount: float | None
    estimated_balance: float | None
    recorded_date: str | None
    document_number: str | None
    lender: str | None
    status: str | None
    source_page: int | None
    confidence: float | None = Field(ge=0, le=1)


class Lien(StrictModel):
    type: str | None
    amount: float | None
    recorded_date: str | None
    document_number: str | None
    holder: str | None
    status: str | None
    source_page: int | None
    confidence: float | None = Field(ge=0, le=1)


class Foreclosure(StrictModel):
    in_foreclosure: bool | None
    stage: str | None
    trustee_sale_number: str | None
    current_sale_date: str | None
    original_sale_date: str | None
    sale_time: str | None
    sale_place: str | None
    published_bid: float | None
    opening_bid: float | None
    winning_bid: float | None
    default_amount: float | None
    trustee: str | None
    trustee_phone: str | None
    source_page: int | None
    confidence: float | None = Field(ge=0, le=1)


class TransactionEvent(StrictModel):
    type: str | None
    date: str | None
    document_number: str | None
    party_names: list[str]
    amount: float | None
    source_page: int | None
    confidence: float | None = Field(ge=0, le=1)


class ListingEvent(StrictModel):
    type: str | None
    status: str | None
    as_of: str | None
    dom: int | None
    price: float | None
    source_page: int | None
    confidence: float | None = Field(ge=0, le=1)


class Rental(StrictModel):
    estimated_rent: float | None
    rent_per_sq_ft: float | None


class AdditionalFact(StrictModel):
    category: str | None
    label: str
    value: str | None
    numeric_value: float | None
    date_value: str | None
    source_page: int | None
    confidence: float | None = Field(ge=0, le=1)


class SourceReference(StrictModel):
    field_path: str
    source_page: int | None
    confidence: float | None = Field(ge=0, le=1)
    evidence: str | None


class PropertyReportExtraction(StrictModel):
    property_identity: PropertyIdentity
    property_details: PropertyDetails
    ownership: Ownership
    valuation: Valuation
    tax: Tax
    loans: list[Loan]
    liens: list[Lien]
    foreclosure: Foreclosure
    transaction_history: list[TransactionEvent]
    listing_history: list[ListingEvent]
    rental: Rental
    additional_facts: list[AdditionalFact]
    source_references: list[SourceReference]


class OwnerPerson(StrictModel):
    full_name: str = Field(min_length=1)
    age: int | None = Field(ge=0, le=130)
    gender: str | None
    mailing_address: str | None


class OwnerContact(StrictModel):
    kind: Literal["phone", "email"]
    value: str = Field(min_length=1)
    rank: int | None
    source: str | None
    confidence: float | None = Field(ge=0, le=1)


class OwnerBankruptcy(StrictModel):
    chapter: str | None
    case_number: str | None
    court: str | None
    filing_date: date | None
    status: str | None
    discharge_date: date | None


class OwnerLien(StrictModel):
    type: str | None
    amount: float | None
    recorded_date: date | None
    document_number: str | None
    holder: str | None
    status: str | None
    confidence: float | None = Field(ge=0, le=1)


class OwnerProfileExtraction(StrictModel):
    person: OwnerPerson
    contacts: list[OwnerContact]
    bankruptcies: list[OwnerBankruptcy]
    liens: list[OwnerLien]
    source_references: list[SourceReference]


def canonical_schema() -> dict:
    """OpenAI strict schema: every object key required, extras forbidden."""
    return PropertyReportExtraction.model_json_schema()


def owner_schema() -> dict:
    return OwnerProfileExtraction.model_json_schema()
