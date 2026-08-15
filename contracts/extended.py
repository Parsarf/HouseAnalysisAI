from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from .models import (AddressBlock, AttachmentBasis, NormalizedProperty, Scenario,
                     SourceKind, StrategyType, TrackedValue)


class OwnershipBlock(BaseModel):
    owner_names: list[str] = []
    entity_type: str | None = None
    is_owner_occupied: bool | None = None
    is_absentee: bool | None = None
    ownership_start_date: date | None = None
    purchase_price: TrackedValue | None = None
    years_owned: Decimal | None = None


class ValuationCandidate(BaseModel):
    valuation_type: str
    value: TrackedValue
    value_low: Decimal | None = None
    value_high: Decimal | None = None
    as_of: date | None = None
    reported_confidence: Decimal | None = None
    weight_hint: Decimal | None = None


class MortgageRecord(BaseModel):
    position: str
    lender: str | None = None
    original_amount: TrackedValue | None = None
    rate: Decimal | None = None
    term_months: int | None = None
    origination_date: date | None = None
    estimated_balance: TrackedValue | None = None
    balance_method: Literal["reported", "amortized", "derived"] | None = None
    is_open: bool | None = None


class ForeclosureState(BaseModel):
    stage: str | None = None
    nod_date: date | None = None
    nts_date: date | None = None
    original_sale_date: date | None = None
    current_sale_date: date | None = None
    published_bid: TrackedValue | None = None
    default_amount: TrackedValue | None = None
    postponement_count: int = 0
    rescission_count: int = 0
    trustee: str | None = None
    is_active: bool = False


class BankruptcyRecord(BaseModel):
    chapter: str | None = None
    status: str | None = None
    filing_date: date | None = None
    discharge_date: date | None = None
    sequence: int | None = None


class TaxBlock(BaseModel):
    annual_taxes: TrackedValue | None = None
    assessed_value: TrackedValue | None = None
    delinquent_amount: TrackedValue | None = None
    delinquent_years: int | None = None


class HoaBlock(BaseModel):
    monthly_dues: TrackedValue | None = None
    arrears: TrackedValue | None = None
    has_lien: bool | None = None


class RentalBlock(BaseModel):
    rent_estimate: TrackedValue | None = None
    source: str | None = None


class ListingRecord(BaseModel):
    list_date: date | None = None
    delist_date: date | None = None
    price: TrackedValue | None = None
    status: str | None = None
    dom: int | None = None


class ComparableSale(BaseModel):
    address: str
    sale_date: date | None = None
    price: TrackedValue | None = None
    sqft: Decimal | None = None
    distance: Decimal | None = None
    similarity: Decimal | None = None
    included: bool = True


class ConditionSignal(BaseModel):
    level: Literal["pristine", "cosmetic", "moderate", "heavy", "gut"]
    evidence: list[str] = []


class DataQualityBlock(BaseModel):
    critical_field_coverage: Decimal = Decimal("0")
    source_counts_by_field: dict[str, int] = {}
    conflict_count: int = 0
    material_conflict_count: int = 0
    verified_field_count: int = 0
    ocr_applied: bool = False
    newest_report_date: date | None = None
    mean_extraction_confidence: Decimal = Decimal("0")


class FlagSummary(BaseModel):
    flag_type: str
    severity: str
    is_gating: bool
    financial_impact: Decimal | None = None


class FullNormalizedProperty(NormalizedProperty):
    ownership: OwnershipBlock = OwnershipBlock()
    valuation_candidates: list[ValuationCandidate] = []
    mortgages: list[MortgageRecord] = []
    foreclosure: ForeclosureState | None = None
    bankruptcies: list[BankruptcyRecord] = []
    taxes: TaxBlock = TaxBlock()
    hoa: HoaBlock = HoaBlock()
    rental: RentalBlock = RentalBlock()
    listings: list[ListingRecord] = []
    comparables: list[ComparableSale] = []
    condition: ConditionSignal | None = None
    data_quality: DataQualityBlock = DataQualityBlock()
    open_flags: list[FlagSummary] = []


class AcquisitionCosts(BaseModel):
    closing_pct: Decimal = Decimal("0.02")
    title_pct: Decimal = Decimal("0.005")
    escrow_flat: Decimal = Decimal("750")
    transfer_tax: Decimal = Decimal("0")
    financing_points: Decimal = Decimal("0")
    financing_flat: Decimal = Decimal("0")
    inspection_flat: Decimal = Decimal("500")
    legal_flat: Decimal = Decimal("0")
    acq_fee_pct: Decimal = Decimal("0")


class RepairAssumptions(BaseModel):
    psf_by_condition: dict[str, Decimal] = {"cosmetic": Decimal("18"), "moderate": Decimal("42"), "heavy": Decimal("78"), "gut": Decimal("135")}
    low_multiplier: Decimal = Decimal(".75")
    high_multiplier: Decimal = Decimal("1.4")
    regional_index: Decimal = Decimal("1")


class HoldingAssumptions(BaseModel):
    insurance_pct_yr: Decimal = Decimal(".005")
    utilities_monthly: Decimal = Decimal("250")
    maintenance_pct_yr: Decimal = Decimal(".01")
    acquisition_months: int = 1
    repair_months_by_condition: dict[str, int] = {"cosmetic": 2, "moderate": 4, "heavy": 6, "gut": 9}
    market_days_default: int = 90


class ResaleAssumptions(BaseModel):
    commission_pct: Decimal = Decimal(".05")
    seller_closing_pct: Decimal = Decimal(".01")
    concessions_pct: Decimal = Decimal("0")
    staging_flat: Decimal = Decimal("0")
    misc_pct: Decimal = Decimal(".01")


class StrategyAssumptions(BaseModel):
    cash_target_margin: Decimal = Decimal(".15")
    flip_target_margin_by_arv_band: dict[str, Decimal] = {"default": Decimal(".2")}
    wholesale_investor_pct: Decimal = Decimal(".7")
    min_assignment_spread: Decimal = Decimal("5000")


class AssumptionSet(BaseModel):
    id: UUID
    version: int
    name: str
    acquisition: AcquisitionCosts = AcquisitionCosts()
    repairs: RepairAssumptions = RepairAssumptions()
    holding: HoldingAssumptions = HoldingAssumptions()
    resale: ResaleAssumptions = ResaleAssumptions()
    strategy: StrategyAssumptions = StrategyAssumptions()
    attachment_probability: dict[AttachmentBasis, Decimal] = {AttachmentBasis.OWNER_NAMED_ONLY: Decimal(".35"), AttachmentBasis.UNKNOWN: Decimal(".5")}
    unknown_lien_medians: dict[str, Decimal] = {}
    valuation_weights: dict[str, Decimal] = {}


class ValueBlock(BaseModel):
    v_low: Decimal | None = None
    v_expected: Decimal | None = None
    v_high: Decimal | None = None
    dispersion: Decimal | None = None
    arv_by_scenario: dict[Scenario, Decimal | None] = {}
    candidates_used: list[dict[str, object]] = []
    valuation_confidence: Decimal = Decimal("0")


class LiabilityBlock(BaseModel):
    confirmed: Decimal = Decimal("0")
    potential: Decimal = Decimal("0")
    maximum: Decimal = Decimal("0")
    breakdown: list[dict[str, object]] = []


class EquityBlock(BaseModel):
    gross: Decimal | None = None
    adjusted: Decimal | None = None
    net_realizable: Decimal | None = None
    equity_pct: Decimal | None = None


class UnderwritingResult(BaseModel):
    property_id: UUID
    assumption_set_id: UUID
    engine_version: str
    status: Literal["ok", "insufficient_data"]
    unavailable_reason: str | None = None
    value: ValueBlock
    liabilities: LiabilityBlock
    equity: dict[Scenario, EquityBlock] = {}
    costs: dict[Scenario, dict[str, Decimal]] = {}
    debt_data_present: bool
    confidence: Decimal


class StrategyResult(BaseModel):
    strategy: StrategyType
    scenario: Scenario
    status: Literal["viable", "not_viable", "unavailable", "requires_human_review"]
    unavailable_reason: str | None = None
    mao: Decimal | None = None
    all_in_basis: Decimal | None = None
    profit: Decimal | None = None
    roi: Decimal | None = None
    margin_of_safety: Decimal | None = None
    metrics: dict[str, Decimal | None] = {}
    inputs_echo: dict[str, Decimal] = {}
    notices: list[str] = []


class OfferPoint(BaseModel):
    offer_price: Decimal
    scenario: Scenario
    confirmed_payoffs: Decimal
    potential_payoffs: Decimal
    closing_costs: Decimal
    proceeds_low: Decimal
    proceeds_expected: Decimal
    proceeds_high: Decimal
    buyer_basis: Decimal
    profit: Decimal
    roi: Decimal | None = None
    is_short_sale: bool = False
    label: str | None = None


class OfferGrid(BaseModel):
    property_id: UUID
    points: list[OfferPoint]
    interpolatable: bool = True


class ScoreSet(BaseModel):
    property_id: UUID
    scoring_config_id: UUID
    fos: Decimal
    distress: Decimal
    data_confidence: Decimal
    risk: Decimal
    overall: Decimal
    components: dict[str, Decimal] = {}
    gates_applied: list[str] = []
    is_rankable: bool
    recommended_strategy: StrategyType | None = None
    recommended_alternatives: list[StrategyType] = []


class RecordedResponse(BaseModel):
    response_id: str
    model: str
    prompt_version: str
    input_hash: str
    response: dict
