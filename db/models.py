from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def now():
    return datetime.now(UTC)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSON)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str | None] = mapped_column(String(255))
    tag: Mapped[str | None] = mapped_column(String(255))
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="created")
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    budget_limit_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal(0))
    awaiting_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Property(Base):
    __tablename__ = "properties"
    __table_args__ = (
        Index(
            "properties_address_active_uq",
            "address_hash",
            unique=True,
            postgresql_where="merged_into_id IS NULL AND address_hash IS NOT NULL",
        ),
        Index(
            "properties_apn_active_uq",
            "apn_key",
            unique=True,
            postgresql_where="merged_into_id IS NULL AND apn_key IS NOT NULL",
        ),
        Index("properties_address_trgm_idx", "address_key"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    apn: Mapped[str | None] = mapped_column(String(120))
    apn_key: Mapped[str | None] = mapped_column(String(120))
    fips_county: Mapped[str | None] = mapped_column(String(10))
    address_line1: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(2))
    zip5: Mapped[str | None] = mapped_column(String(5))
    address_key: Mapped[str | None] = mapped_column(String(255))
    address_hash: Mapped[str | None] = mapped_column(String(64))
    lat: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    lng: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    property_type: Mapped[str | None] = mapped_column(Text)
    beds: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    baths: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    sqft: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    lot_sqft: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    year_built: Mapped[int | None] = mapped_column(Integer)
    units: Mapped[int | None] = mapped_column(Integer)
    pipeline_status: Mapped[str] = mapped_column(String(30), default="new")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    next_action: Mapped[str | None] = mapped_column(String(255))
    next_action_date: Mapped[date | None] = mapped_column(Date)
    gut_rating: Mapped[int | None] = mapped_column(Integer)
    is_watchlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    merged_into_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    underwriting_status: Mapped[str | None] = mapped_column(Text)
    last_recomputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        Index(
            "reports_sha256_original_uq",
            "sha256",
            unique=True,
            postgresql_where=text("duplicate_of IS NULL"),
            sqlite_where=text("duplicate_of IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    batch_id: Mapped[UUID | None] = mapped_column(ForeignKey("batches.id"))
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    report_type: Mapped[str | None] = mapped_column(String(60))
    vendor: Mapped[str | None] = mapped_column(String(120))
    generated_date: Mapped[date | None] = mapped_column(Date)
    file_path: Mapped[str] = mapped_column(Text)
    ocr_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    page_count: Mapped[int | None] = mapped_column(Integer)
    is_scanned: Mapped[bool] = mapped_column(Boolean, default=False)
    ocr_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of: Mapped[UUID | None] = mapped_column(ForeignKey("reports.id"))
    status: Mapped[str] = mapped_column(String(30), default="uploaded")
    classification_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    failure_reason: Mapped[str | None] = mapped_column(String(60))
    section_match_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ReportExtraction(Base):
    """Canonical whole-document extraction for one immutable report payload."""

    __tablename__ = "report_extractions"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    report_id: Mapped[UUID] = mapped_column(ForeignKey("reports.id"), unique=True, index=True)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"), index=True)
    schema_version: Mapped[str] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(120))
    raw_json: Mapped[dict | None] = mapped_column(JSON)
    normalized_json: Mapped[dict | None] = mapped_column(JSON)
    validation_issues: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(40), default="analyzing")
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ExtractionUnit(Base):
    __tablename__ = "extraction_units"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    report_id: Mapped[UUID] = mapped_column(ForeignKey("reports.id"))
    unit_type: Mapped[str] = mapped_column(String(60))
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    text_path: Mapped[str | None] = mapped_column(Text)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    model: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(80))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Flag(Base):
    __tablename__ = "flags"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID] = mapped_column(ForeignKey("properties.id"))
    flag_type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSON)
    financial_impact_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(30), default="open")
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_value: Mapped[dict | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    logical_key: Mapped[str | None] = mapped_column(String(255), index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    superseded_by: Mapped[UUID | None] = mapped_column(ForeignKey("flags.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Owner(Base):
    __tablename__ = "owners"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    full_name: Mapped[str] = mapped_column(String(255))
    name_normalized: Mapped[str] = mapped_column(String(255))
    entity_type: Mapped[str | None] = mapped_column(String(30))
    mailing_address: Mapped[str | None] = mapped_column(Text)
    is_absentee: Mapped[bool | None] = mapped_column(Boolean)
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class PropertyOwner(Base):
    __tablename__ = "property_owners"
    property_id: Mapped[UUID] = mapped_column(ForeignKey("properties.id"), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("owners.id"), primary_key=True)
    ownership_start_date: Mapped[date | None] = mapped_column(Date)
    ownership_end_date: Mapped[date | None] = mapped_column(Date)
    vesting: Mapped[str | None] = mapped_column(String(120))
    ownership_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 6))
    is_current: Mapped[bool | None] = mapped_column(Boolean)
    acquired_via: Mapped[str | None] = mapped_column(String(60))


class DocumentSignature(Base):
    __tablename__ = "document_signatures"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    pattern: Mapped[str] = mapped_column(Text)
    report_type: Mapped[str] = mapped_column(String(60))
    vendor: Mapped[str | None] = mapped_column(String(120))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ExtractedFact(Base):
    __tablename__ = "extracted_facts"
    __table_args__ = (
        Index("extracted_facts_property_field_idx", "property_id", "field_path"),
        Index("extracted_facts_report_idx", "report_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    report_id: Mapped[UUID | None] = mapped_column(ForeignKey("reports.id"))
    extraction_unit_id: Mapped[UUID | None] = mapped_column(ForeignKey("extraction_units.id"))
    entity_type: Mapped[str] = mapped_column(String(60))
    entity_local_id: Mapped[str] = mapped_column(String(120))
    field_path: Mapped[str] = mapped_column(String(255))
    value_raw: Mapped[str | None] = mapped_column(Text)
    value_parsed: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    value_text: Mapped[str | None] = mapped_column(Text)
    value_date: Mapped[date | None] = mapped_column(Date)
    value_bool: Mapped[bool | None] = mapped_column(Boolean)
    unit: Mapped[str | None] = mapped_column(String(30))
    as_of_date: Mapped[date | None] = mapped_column(Date)
    page_number: Mapped[int] = mapped_column(Integer)
    snippet: Mapped[str] = mapped_column(Text)
    extraction_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    null_reason: Mapped[str | None] = mapped_column(String(120))
    source_kind: Mapped[str] = mapped_column(String(30))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    superseded_by: Mapped[UUID | None] = mapped_column(ForeignKey("extracted_facts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FieldResolution(Base):
    __tablename__ = "field_resolutions"
    __table_args__ = (UniqueConstraint("property_id", "field_path"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    field_path: Mapped[str] = mapped_column(String(255))
    winning_fact_id: Mapped[UUID | None] = mapped_column(ForeignKey("extracted_facts.id"))
    method: Mapped[str] = mapped_column(String(60))
    score: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    candidate_fact_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid), default=list)
    has_conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    conflict_magnitude: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    verification_state: Mapped[str] = mapped_column(String(30), default="unverified")


class Mortgage(Base):
    __tablename__ = "mortgages"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    position: Mapped[str | None] = mapped_column(String(10))
    lender_raw: Mapped[str | None] = mapped_column(String(255))
    lender_normalized: Mapped[str | None] = mapped_column(String(255))
    original_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    origination_date: Mapped[date | None] = mapped_column(Date)
    recording_date: Mapped[date | None] = mapped_column(Date)
    recording_doc_number: Mapped[str | None] = mapped_column(String(120))
    term_months: Mapped[int | None] = mapped_column(Integer)
    interest_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 6))
    rate_type: Mapped[str | None] = mapped_column(String(30))
    estimated_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    balance_method: Mapped[str | None] = mapped_column(String(60))
    balance_as_of: Mapped[date | None] = mapped_column(Date)
    is_open: Mapped[bool | None] = mapped_column(Boolean)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    primary_fact_id: Mapped[UUID | None] = mapped_column(ForeignKey("extracted_facts.id"))


class Lien(Base):
    __tablename__ = "liens"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("owners.id"))
    lien_type: Mapped[str | None] = mapped_column(String(60))
    creditor_raw: Mapped[str | None] = mapped_column(String(255))
    creditor_normalized: Mapped[str | None] = mapped_column(String(255))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    amount_is_estimated: Mapped[bool | None] = mapped_column(Boolean)
    recording_date: Mapped[date | None] = mapped_column(Date)
    recording_doc_number: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str | None] = mapped_column(String(30))
    attachment_basis: Mapped[str] = mapped_column(String(60))
    attachment_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    attachment_verified_by: Mapped[str | None] = mapped_column(String(120))
    attachment_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority_position: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    primary_fact_id: Mapped[UUID | None] = mapped_column(ForeignKey("extracted_facts.id"))


class ForeclosureEvent(Base):
    __tablename__ = "foreclosure_events"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    event_type: Mapped[str | None] = mapped_column(String(30))
    event_date: Mapped[date | None] = mapped_column(Date)
    trustee_name: Mapped[str | None] = mapped_column(String(255))
    trustee_phone: Mapped[str | None] = mapped_column(String(40))
    trustee_sale_number: Mapped[str | None] = mapped_column(String(120))
    original_sale_date: Mapped[date | None] = mapped_column(Date)
    current_sale_date: Mapped[date | None] = mapped_column(Date)
    published_bid: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    default_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    default_as_of: Mapped[date | None] = mapped_column(Date)
    beneficiary: Mapped[str | None] = mapped_column(String(255))
    stage_after_event: Mapped[str | None] = mapped_column(String(30))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))


class BankruptcyEvent(Base):
    __tablename__ = "bankruptcy_events"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("owners.id"))
    chapter: Mapped[str | None] = mapped_column(String(10))
    case_number: Mapped[str | None] = mapped_column(String(60))
    court: Mapped[str | None] = mapped_column(String(120))
    filing_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str | None] = mapped_column(String(30))
    discharge_date: Mapped[date | None] = mapped_column(Date)
    filing_sequence: Mapped[int | None] = mapped_column(Integer)
    is_repeat: Mapped[bool | None] = mapped_column(Boolean)


class Valuation(Base):
    __tablename__ = "valuations"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    valuation_type: Mapped[str | None] = mapped_column(String(30))
    value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    value_low: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    value_high: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    confidence_reported: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    as_of_date: Mapped[date | None] = mapped_column(Date)
    source_report_id: Mapped[UUID | None] = mapped_column(ForeignKey("reports.id"))
    weight_applied: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Listing(Base):
    __tablename__ = "listings"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    list_date: Mapped[date | None] = mapped_column(Date)
    delist_date: Mapped[date | None] = mapped_column(Date)
    list_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    final_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    status: Mapped[str | None] = mapped_column(String(30))
    dom: Mapped[int | None] = mapped_column(Integer)
    mls_number: Mapped[str | None] = mapped_column(String(60))


class ComparableSale(Base):
    __tablename__ = "comparable_sales"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    comp_address: Mapped[str | None] = mapped_column(String(255))
    sale_date: Mapped[date | None] = mapped_column(Date)
    sale_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    sqft: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    beds: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    baths: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    distance_miles: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    price_per_sqft: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    similarity_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    included: Mapped[bool | None] = mapped_column(Boolean)
    exclusion_reason: Mapped[str | None] = mapped_column(Text)


class AssumptionSet(Base):
    __tablename__ = "assumption_sets"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    params: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer)
    effective_from: Mapped[date] = mapped_column(Date)


class DealScenario(Base):
    __tablename__ = "deal_scenarios"
    __table_args__ = (
        UniqueConstraint("property_id", "strategy", "scenario", "assumption_set_id", "engine_version", name="deal_scenarios_uq"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    strategy: Mapped[str | None] = mapped_column(String(30))
    scenario: Mapped[str | None] = mapped_column(String(30))
    assumption_set_id: Mapped[UUID | None] = mapped_column(ForeignKey("assumption_sets.id"))
    engine_version: Mapped[str | None] = mapped_column(String(60))
    purchase_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    arv: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    repairs: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    holding: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    financing: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    resale: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    all_in_basis: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    profit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    roi: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    margin_of_safety: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    cap_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    cash_flow: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    coc: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    mao: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    status: Mapped[str | None] = mapped_column(String(30))
    unavailable_reason: Mapped[str | None] = mapped_column(Text)
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OfferScenario(Base):
    __tablename__ = "offer_scenarios"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    offer_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    scenario: Mapped[str | None] = mapped_column(String(30))
    confirmed_payoffs: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    potential_payoffs: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    closing_costs: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    proceeds_low: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    proceeds_expected: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    proceeds_high: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    buyer_basis: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    profit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    roi: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    is_short_sale: Mapped[bool | None] = mapped_column(Boolean)


class ScoringConfig(Base):
    __tablename__ = "scoring_configs"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    weights: Mapped[dict | None] = mapped_column(JSON)
    bounds: Mapped[dict | None] = mapped_column(JSON)
    distress_points: Mapped[dict | None] = mapped_column(JSON)
    gates: Mapped[dict | None] = mapped_column(JSON)
    version: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool | None] = mapped_column(Boolean)


class Score(Base):
    __tablename__ = "scores"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    scoring_config_id: Mapped[UUID | None] = mapped_column(ForeignKey("scoring_configs.id"))
    fos: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    distress: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    data_confidence: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    risk: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    overall: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    components: Mapped[dict | None] = mapped_column(JSON)
    gates_applied: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    engine_version: Mapped[str | None] = mapped_column(String(60))
    resolution_version: Mapped[str | None] = mapped_column(String(60))
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Ranking(Base):
    __tablename__ = "rankings"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", "property_id", "ranked_at", name="rankings_uq"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scope_type: Mapped[str | None] = mapped_column(String(30))
    scope_id: Mapped[UUID | None] = mapped_column(Uuid)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    rank: Mapped[int | None] = mapped_column(Integer)
    prev_rank: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    ranked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChangeEvent(Base):
    __tablename__ = "change_events"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    change_type: Mapped[str | None] = mapped_column(String(60))
    field_path: Mapped[str | None] = mapped_column(String(255))
    old_value: Mapped[dict | None] = mapped_column(JSON)
    new_value: Mapped[dict | None] = mapped_column(JSON)
    source_report_id: Mapped[UUID | None] = mapped_column(ForeignKey("reports.id"))
    score_delta: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=now)


class PropertyNote(Base):
    __tablename__ = "property_notes"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RealizedDeal(Base):
    __tablename__ = "realized_deals"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[UUID | None] = mapped_column(ForeignKey("properties.id"))
    purchase_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    actual_repairs: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    actual_holding_days: Mapped[int | None] = mapped_column(Integer)
    sale_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    actual_costs: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    outcome: Mapped[str | None] = mapped_column(String(30))
    notes: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[date | None] = mapped_column(Date)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class History(Base):
    __tablename__ = "history"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[UUID | None] = mapped_column(Uuid)
    action: Mapped[str | None] = mapped_column(String(60))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=now)


class SavedView(Base):
    __tablename__ = "saved_views"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str | None] = mapped_column(String(120))
    filters: Mapped[dict | None] = mapped_column(JSON)
    columns: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LenderAlias(Base):
    __tablename__ = "lender_aliases"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    alias: Mapped[str] = mapped_column(String(255), unique=True)
    canonical_name: Mapped[str] = mapped_column(String(255))


class HistoricalRateIndex(Base):
    __tablename__ = "historical_rate_index"
    __table_args__ = (UniqueConstraint("year", "loan_type"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    year: Mapped[int] = mapped_column(Integer)
    loan_type: Mapped[str] = mapped_column(String(60))
    rate: Mapped[Decimal] = mapped_column(Numeric(7, 6))


class RegionalCostIndex(Base):
    __tablename__ = "regional_cost_index"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    region_key: Mapped[str] = mapped_column(String(120), unique=True)
    index_value: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    effective_from: Mapped[date | None] = mapped_column(Date)


class TransferTaxRate(Base):
    __tablename__ = "transfer_tax_rates"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    lookup_key: Mapped[str] = mapped_column(String(120), unique=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    flat_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    notes: Mapped[str | None] = mapped_column(Text)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    version: Mapped[str] = mapped_column(String(80), unique=True)
    unit_type: Mapped[str | None] = mapped_column(String(60))
    prompt_path: Mapped[str | None] = mapped_column(Text)
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
