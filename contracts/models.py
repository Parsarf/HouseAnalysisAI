"""Frozen cross-package contracts for ACQ WP-0."""
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceKind(StrEnum):
    REPORT="report"; DERIVED="derived"; HUMAN="human"; API="api"; PASTED="pasted"
class NullReason(StrEnum):
    NOT_PRESENT="not_present"; ILLEGIBLE="illegible"; REDACTED="redacted"; CONFLICTING_IN_SOURCE="conflicting_in_source"
class AttachmentBasis(StrEnum):
    RECORDED_AGAINST_PROPERTY="recorded_against_property"; OWNER_NAMED_ONLY="owner_named_only"; UNKNOWN="unknown"
class Scenario(StrEnum): CONSERVATIVE="conservative"; EXPECTED="expected"; OPTIMISTIC="optimistic"
class EntityType(StrEnum):
    PROPERTY="property"; MORTGAGE="mortgage"; LIEN="lien"; FORECLOSURE="foreclosure"; BANKRUPTCY="bankruptcy"; VALUATION="valuation"; LISTING="listing"; COMP="comp"; TAX="tax"; RENTAL="rental"; CONDITION="condition"
class StrategyType(StrEnum): CASH="cash"; FLIP="flip"; WHOLESALE="wholesale"; RENTAL="rental"; SUBJECT_TO="subject_to"; FORECLOSURE="foreclosure"
class FlagType(StrEnum):
    IDENTITY_CONFLICT="identity_conflict"; LIEN_ATTACHMENT="lien_attachment"; CONFLICTING_MORTGAGE="conflicting_mortgage"; FORECLOSURE_UNCLEAR="foreclosure_unclear"; MISSING_LIEN_AMOUNT="missing_lien_amount"; VALUATION_DISPERSION="valuation_dispersion"; MISSING_APN="missing_apn"; LOW_EXTRACTION_CONFIDENCE="low_extraction_confidence"; BID_MISMATCH="bid_mismatch"; RANGE_VIOLATION="range_violation"; POSSIBLE_DUPLICATE="possible_duplicate"; SHORT_SALE_CANDIDATE="short_sale_candidate"
class FailureCode(StrEnum):
    ENCRYPTED="encrypted"; CORRUPT="corrupt"; NOT_PDF="not_pdf"; PARTIAL_OCR="partial_ocr"; OCR_TIMEOUT="ocr_timeout"; EXTRACTION_FAILED="extraction_failed"; GROUNDING_FAILED="grounding_failed"; UNCLASSIFIED="unclassified"; STUCK_JOB="stuck_job"; BUDGET_PAUSED="budget_paused"; IDENTITY_UNRESOLVED="identity_unresolved"
class ReportStatus(StrEnum):
    UPLOADED="uploaded"; TEXT_EXTRACTED="text_extracted"; OCR_PENDING="ocr_pending"; CLASSIFIED="classified"; EXTRACTING="extracting"; EXTRACTED="extracted"; READY="ready"; FAILED="failed"; ENCRYPTED="encrypted"; CORRUPT="corrupt"

class ContractModel(BaseModel):
    model_config=ConfigDict(extra="forbid")
class TrackedValue(ContractModel):
    value: Decimal|None; confidence: float=Field(ge=0,le=1); source_kind: SourceKind; is_estimated: bool; fact_id: UUID|None=None; as_of: date|None=None; null_reason: NullReason|None=None
    @model_validator(mode="after")
    def null_discipline(self):
        if (self.value is None)!=(self.null_reason is not None): raise ValueError("null values require null_reason and non-null values forbid it")
        return self
class ExtractedFactDraft(ContractModel):
    report_id: UUID; extraction_unit_id: UUID; entity_type: EntityType; entity_local_id: str; field_path: str; value_raw: str|None=None; value_parsed: Decimal|None=None; value_text: str|None=None; value_date: date|None=None; value_bool: bool|None=None; unit: str|None=None; as_of_date: date|None=None; page_number: int=Field(ge=1); snippet: str=Field(max_length=200); extraction_confidence: float=Field(ge=0,le=1); null_reason: NullReason|None=None; source_kind: SourceKind=SourceKind.REPORT
class AddressBlock(ContractModel): line1: str|None=None; unit: str|None=None; city: str|None=None; state: str|None=None; zip5: str|None=None; county: str|None=None; fips: str|None=None; lat: Decimal|None=None; lng: Decimal|None=None
class PropertyAttributes(ContractModel): property_type: TrackedValue|None=None; beds: TrackedValue|None=None; baths: TrackedValue|None=None; sqft: TrackedValue|None=None; lot_sqft: TrackedValue|None=None; year_built: TrackedValue|None=None; units: TrackedValue|None=None
class OwnershipBlock(ContractModel): owner_names: list[str]=[]; entity_type: str|None=None; is_owner_occupied: bool|None=None; is_absentee: bool|None=None; ownership_start_date: date|None=None; purchase_price: TrackedValue|None=None; years_owned: Decimal|None=None
class ValuationCandidate(ContractModel): valuation_type: str; value: TrackedValue; value_low: Decimal|None=None; value_high: Decimal|None=None; as_of: date|None=None; reported_confidence: float|None=Field(default=None,ge=0,le=1); weight_hint: Decimal|None=None
class MortgageRecord(ContractModel): position: str; lender: str|None=None; original_amount: TrackedValue|None=None; rate: Decimal|None=None; term_months: int|None=None; origination_date: date|None=None; estimated_balance: TrackedValue|None=None; balance_method: str="reported"; is_open: bool=True
class LienRecord(ContractModel): lien_type: str; amount: TrackedValue|None=None; amount_is_estimated: bool=False; status: str="unknown"; attachment_basis: AttachmentBasis; attachment_confidence: float=Field(ge=0,le=1); recording_date: date|None=None; priority: int|None=None
class ForeclosureState(ContractModel): stage: str; nod_date: date|None=None; nts_date: date|None=None; original_sale_date: date|None=None; current_sale_date: date|None=None; published_bid: TrackedValue|None=None; default_amount: TrackedValue|None=None; postponement_count: int=0; rescission_count: int=0; trustee: str|None=None; is_active: bool=False
class BankruptcyRecord(ContractModel): chapter: str; status: str; filing_date: date|None=None; discharge_date: date|None=None; sequence: int|None=None
class TaxBlock(ContractModel): annual_taxes: TrackedValue|None=None; assessed_value: TrackedValue|None=None; delinquent_amount: TrackedValue|None=None; delinquent_years: int|None=None
class HoaBlock(ContractModel): monthly_dues: TrackedValue|None=None; arrears: TrackedValue|None=None; has_lien: bool=False
class RentalBlock(ContractModel): rent_estimate: TrackedValue|None=None; source: str|None=None
class ListingRecord(ContractModel): list_date: date; delist_date: date|None=None; price: TrackedValue|None=None; status: str; dom: int|None=None
class ComparableSale(ContractModel): address: str; sale_date: date|None=None; price: TrackedValue|None=None; sqft: Decimal|None=None; distance: Decimal|None=None; similarity: Decimal|None=None; included: bool=True
class ConditionSignal(ContractModel): condition: Literal["pristine","cosmetic","moderate","heavy","gut"]; evidence: str|None=None
class FlagSummary(ContractModel): type: FlagType; severity: str="warning"; is_gating: bool=False; financial_impact: Decimal|None=None
class DataQualityBlock(ContractModel): critical_field_coverage: Decimal=Decimal(0); source_counts_by_field: dict[str,int]={}; conflict_count: int=0; material_conflict_count: int=0; verified_field_count: int=0; ocr_applied: bool=False; newest_report_date: date|None=None; mean_extraction_confidence: Decimal=Decimal(0)
class NormalizedProperty(ContractModel):
    property_id: UUID; apn: str|None=None; address: AddressBlock; attributes: PropertyAttributes=Field(default_factory=PropertyAttributes); ownership: OwnershipBlock=Field(default_factory=OwnershipBlock); valuation_candidates: list[ValuationCandidate]=[]; mortgages: list[MortgageRecord]=[]; liens: list[LienRecord]=[]; foreclosure: ForeclosureState|None=None; bankruptcies: list[BankruptcyRecord]=[]; taxes: TaxBlock=Field(default_factory=TaxBlock); hoa: HoaBlock=Field(default_factory=HoaBlock); rental: RentalBlock=Field(default_factory=RentalBlock); listings: list[ListingRecord]=[]; comparables: list[ComparableSale]=[]; condition: ConditionSignal|None=None; data_quality: DataQualityBlock=Field(default_factory=DataQualityBlock); open_flags: list[FlagSummary]=[]; resolution_version: str
class AcquisitionCosts(ContractModel): closing_pct: Decimal; title_pct: Decimal; escrow_flat: Decimal; transfer_tax_lookup_key: str|None=None; financing_points: Decimal; financing_flat: Decimal; inspection_flat: Decimal; legal_flat: Decimal; acq_fee_pct: Decimal
class RepairAssumptions(ContractModel): psf_by_condition: dict[str,Decimal]; low_multiplier: Decimal; high_multiplier: Decimal; regional_index: Decimal
class HoldingAssumptions(ContractModel): insurance_pct_yr: Decimal; utilities_monthly: Decimal; maintenance_pct_yr: Decimal; acquisition_months: Decimal; repair_months_by_condition: dict[str,Decimal]; market_days_default: int
class ResaleAssumptions(ContractModel): commission_pct: Decimal; seller_closing_pct: Decimal; concessions_pct: Decimal; staging_flat: Decimal; misc_pct: Decimal
class StrategyAssumptions(ContractModel): cash_target_margin: Decimal; flip_target_margin_by_arv_band: dict[str,Decimal]; wholesale_investor_pct: Decimal; min_assignment_spread: Decimal; hard_money: dict[str,Decimal]; rental: dict[str,Decimal]
class AssumptionSet(ContractModel): id: UUID; version: int; name: str; acquisition: AcquisitionCosts; repairs: RepairAssumptions; holding: HoldingAssumptions; resale: ResaleAssumptions; strategy: StrategyAssumptions; attachment_probability: dict[AttachmentBasis,Decimal]; unknown_lien_medians: dict[str,Decimal]; valuation_weights: dict[str,Decimal]
class ValueBlock(ContractModel): v_low: Decimal|None=None; v_expected: Decimal|None=None; v_high: Decimal|None=None; dispersion: Decimal|None=None; arv_by_scenario: dict[Scenario,Decimal|None]={}; candidates_used: list[dict[str,Any]]=[]; valuation_confidence: Decimal|None=None
class LiabilityBlock(ContractModel): confirmed: Decimal=Decimal(0); potential: Decimal=Decimal(0); maximum: Decimal=Decimal(0); breakdown: list[dict[str,Any]]=[]
class EquityBlock(ContractModel): gross: Decimal|None=None; adjusted: Decimal|None=None; net_realizable: Decimal|None=None; equity_pct: Decimal|None=None
class CostBlock(ContractModel): acquisition: Decimal=Decimal(0); repairs: Decimal=Decimal(0); holding: Decimal=Decimal(0); resale: Decimal=Decimal(0); financing: Decimal=Decimal(0)
class UnderwritingResult(ContractModel): property_id: UUID; assumption_set_id: UUID; engine_version: str; status: Literal["ok","insufficient_data"]; unavailable_reason: str|None=None; value: ValueBlock=Field(default_factory=ValueBlock); liabilities: LiabilityBlock=Field(default_factory=LiabilityBlock); equity: dict[Scenario,EquityBlock]={}; costs: dict[Scenario,CostBlock]={}; debt_data_present: bool=False; confidence: Decimal=Decimal(0)
class StrategyResult(ContractModel): strategy: StrategyType; scenario: Scenario; status: Literal["viable","not_viable","unavailable","requires_human_review"]; unavailable_reason: str|None=None; mao: Decimal|None=None; all_in_basis: Decimal|None=None; profit: Decimal|None=None; roi: Decimal|None=None; margin_of_safety: Decimal|None=None; metrics: dict[str,Decimal|None]={}; inputs_echo: dict[str,Decimal]={}; notices: list[str]=[]
class OfferPoint(ContractModel): offer_price: Decimal; scenario: Scenario; confirmed_payoffs: Decimal; potential_payoffs: Decimal; closing_costs: Decimal; proceeds_low: Decimal; proceeds_expected: Decimal; proceeds_high: Decimal; buyer_basis: Decimal; profit: Decimal; roi: Decimal|None=None; is_short_sale: bool; label: str|None=None
class OfferGrid(ContractModel): property_id: UUID; points: list[OfferPoint]; interpolatable: bool=True
class ScoreSet(ContractModel): property_id: UUID; scoring_config_id: UUID; fos: Decimal; distress: Decimal; data_confidence: Decimal; risk: Decimal; overall: Decimal; components: dict[str,Decimal]; gates_applied: list[str]; is_rankable: bool; recommended_strategy: StrategyType|None=None; recommended_alternatives: list[StrategyType]=[]
class FlagRequest(ContractModel): property_id: UUID; flag_type: FlagType; payload: dict[str,Any]; financial_impact_usd: Decimal|None=None; raised_by: str; dedupe_key: str
class FilterClause(ContractModel): field: str; op: Literal["eq","neq","gt","gte","lt","lte","in","between","contains","is_null"]; value: Any=None
class MoneyResponse(ContractModel): value: Decimal|None; confidence: float=Field(ge=0,le=1); source_kind: SourceKind; is_estimated: bool; null_reason: NullReason|None=None
class JobPayload(ContractModel): name: str; payload: dict[str,Any]={}; dedupe_key: str|None=None

class RecordedResponse(ContractModel):
    response_id: str
    model: str
    prompt_version: str
    input_hash: str
    response: dict[str, Any]

# --- WP-11 API payload contracts (spec §16) -----------------------------------
class ErrorDetail(ContractModel):
    code: str; message: str; details: dict[str, Any] = {}
class ErrorEnvelope(ContractModel):
    error: ErrorDetail
class PropertySummary(ContractModel):
    id: UUID; apn: str|None=None; address_line1: str|None=None; city: str|None=None
    state: str|None=None; zip5: str|None=None; pipeline_status: str="new"
    tags: list[str]=[]; next_action: str|None=None; next_action_date: date|None=None
    gut_rating: int|None=None; is_watchlisted: bool=False
    overall_score: Decimal|None=None; rank: int|None=None; open_flags: int=0
class PropertyDetail(PropertySummary):
    lat: Decimal|None=None; lng: Decimal|None=None
    created_at: datetime|None=None; updated_at: datetime|None=None
    latest_valuation: MoneyResponse|None=None
class PropertyListPage(ContractModel):
    items: list[PropertySummary]; next_cursor: str|None=None
class FlagRecord(ContractModel):
    id: UUID; property_id: UUID; flag_type: FlagType; payload: dict[str, Any]={}
    financial_impact_usd: Decimal|None=None; status: str="open"
    resolution: str|None=None; resolved_value: dict[str, Any]|None=None
    note: str|None=None; dedupe_key: str=""; resolved_at: datetime|None=None
class FlagResolution(ContractModel):
    resolution: Literal["approve", "reject", "replace", "dismiss"]
    note: str|None=None; resolved_value: dict[str, Any]|None=None
class TimelineEvent(ContractModel):
    event_type: str; event_date: date|None=None; label: str; details: dict[str, Any]={}
class NoteCreate(ContractModel):
    body: str = Field(min_length=1)
class NoteRecord(ContractModel):
    id: UUID; property_id: UUID; body: str; created_at: datetime|None=None
class SavedViewCreate(ContractModel):
    name: str = Field(min_length=1); filters: list[FilterClause]=[]
    columns: dict[str, Any]={}
class SavedViewRecord(ContractModel):
    id: UUID; name: str; filters: list[FilterClause]=[]; columns: dict[str, Any]={}
    created_at: datetime|None=None
class OfferRequest(ContractModel):
    offer_price: Decimal = Field(gt=0); scenario: Scenario=Scenario.EXPECTED; label: str|None=None
class BatchEstimate(ContractModel):
    batch_id: UUID; report_count: int; total_tokens: int
    estimated_cost_usd: Decimal; awaiting_confirmation: bool=True
class RankingEntry(ContractModel):
    property_id: UUID; rank: int; prev_rank: int|None=None; score: Decimal|None=None
class AnalysisPayload(ContractModel):
    """One-round-trip deal-page payload (spec §16): the scenario toggle and the
    offer slider work entirely from this response."""
    property_id: UUID; scenario: Scenario=Scenario.EXPECTED
    normalized: NormalizedProperty|None=None; underwriting: UnderwritingResult|None=None
    strategies: list[StrategyResult]=[]; offers: OfferGrid|None=None
    scores: ScoreSet|None=None; flags: list[FlagRecord]=[]; timeline: list[TimelineEvent]=[]
