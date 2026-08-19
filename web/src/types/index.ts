// Generated from contracts.schema via json-schema-to-typescript; do not edit.
// Money values are decimal strings on the wire (never floats).
export type Value = (number | string | null)
export type Confidence = number
export type SourceKind = ("report" | "derived" | "human" | "api" | "pasted")
export type IsEstimated = boolean
export type FactId = (string | null)
export type AsOf = (string | null)
export type NullReason = ("not_present" | "illegible" | "redacted" | "conflicting_in_source")
export type ReportId = string
export type ExtractionUnitId = string
export type EntityType = ("property" | "mortgage" | "lien" | "foreclosure" | "bankruptcy" | "valuation" | "listing" | "comp" | "tax" | "rental" | "condition")
export type EntityLocalId = string
export type FieldPath = string
export type ValueRaw = (string | null)
export type ValueParsed = (number | string | null)
export type ValueText = (string | null)
export type ValueDate = (string | null)
export type ValueBool = (boolean | null)
export type Unit = (string | null)
export type AsOfDate = (string | null)
export type PageNumber = number
export type Snippet = string
export type ExtractionConfidence = number
export type SourceKind1 = ("report" | "derived" | "human" | "api" | "pasted")
export type PropertyId = string
export type Apn = (string | null)
export type Line1 = (string | null)
export type Unit1 = (string | null)
export type City = (string | null)
export type State = (string | null)
export type Zip5 = (string | null)
export type County = (string | null)
export type Fips = (string | null)
export type Lat = (number | string | null)
export type Lng = (number | string | null)
export type OwnerNames = string[]
export type EntityType1 = (string | null)
export type IsOwnerOccupied = (boolean | null)
export type IsAbsentee = (boolean | null)
export type OwnershipStartDate = (string | null)
export type YearsOwned = (number | string | null)
export type ValuationType = string
export type ValueLow = (number | string | null)
export type ValueHigh = (number | string | null)
export type AsOf1 = (string | null)
export type ReportedConfidence = (number | null)
export type WeightHint = (number | string | null)
export type ValuationCandidates = ValuationCandidate[]
export type Position = string
export type Lender = (string | null)
export type Rate = (number | string | null)
export type TermMonths = (number | null)
export type OriginationDate = (string | null)
export type BalanceMethod = string
export type IsOpen = boolean
export type Mortgages = MortgageRecord[]
export type LienType = string
export type AmountIsEstimated = boolean
export type Status = string
export type AttachmentBasis = ("recorded_against_property" | "owner_named_only" | "unknown")
export type AttachmentConfidence = number
export type RecordingDate = (string | null)
export type Priority = (number | null)
export type Liens = LienRecord[]
export type Stage = string
export type NodDate = (string | null)
export type NtsDate = (string | null)
export type OriginalSaleDate = (string | null)
export type CurrentSaleDate = (string | null)
export type PostponementCount = number
export type RescissionCount = number
export type Trustee = (string | null)
export type IsActive = boolean
export type Chapter = string
export type Status1 = string
export type FilingDate = (string | null)
export type DischargeDate = (string | null)
export type Sequence = (number | null)
export type Bankruptcies = BankruptcyRecord[]
export type DelinquentYears = (number | null)
export type HasLien = boolean
export type Source = (string | null)
export type ListDate = string
export type DelistDate = (string | null)
export type Status2 = string
export type Dom = (number | null)
export type Listings = ListingRecord[]
export type Address = string
export type SaleDate = (string | null)
export type Sqft = (number | string | null)
export type Distance = (number | string | null)
export type Similarity = (number | string | null)
export type Included = boolean
export type Comparables = ComparableSale[]
export type Condition = ("pristine" | "cosmetic" | "moderate" | "heavy" | "gut")
export type Evidence = (string | null)
export type CriticalFieldCoverage = (number | string)
export type ConflictCount = number
export type MaterialConflictCount = number
export type VerifiedFieldCount = number
export type OcrApplied = boolean
export type NewestReportDate = (string | null)
export type MeanExtractionConfidence = (number | string)
export type FlagType = ("identity_conflict" | "lien_attachment" | "conflicting_mortgage" | "foreclosure_unclear" | "missing_lien_amount" | "valuation_dispersion" | "missing_apn" | "low_extraction_confidence" | "bid_mismatch" | "range_violation" | "possible_duplicate" | "short_sale_candidate")
export type Severity = string
export type IsGating = boolean
export type FinancialImpact = (number | string | null)
export type OpenFlags = FlagSummary[]
export type ResolutionVersion = string
export type PropertyId1 = string
export type FinancialImpactUsd = (number | string | null)
export type RaisedBy = string
export type DedupeKey = string
export type LogicalKey = (string | null)
export type Fingerprint = (string | null)
export type Field = string
export type Op = ("eq" | "neq" | "gt" | "gte" | "lt" | "lte" | "in" | "between" | "contains" | "is_null")
export type Value2 = (number | string | null)
export type Confidence1 = number
export type IsEstimated1 = boolean
export type Name = string
export type DedupeKey1 = (string | null)
export type Id = string
export type Version = number
export type Name1 = string
export type ClosingPct = (number | string)
export type TitlePct = (number | string)
export type EscrowFlat = (number | string)
export type TransferTaxLookupKey = (string | null)
export type FinancingPoints = (number | string)
export type FinancingFlat = (number | string)
export type InspectionFlat = (number | string)
export type LegalFlat = (number | string)
export type AcqFeePct = (number | string)
export type LowMultiplier = (number | string)
export type HighMultiplier = (number | string)
export type RegionalIndex = (number | string)
export type InsurancePctYr = (number | string)
export type UtilitiesMonthly = (number | string)
export type MaintenancePctYr = (number | string)
export type AcquisitionMonths = (number | string)
export type MarketDaysDefault = number
export type CommissionPct = (number | string)
export type SellerClosingPct = (number | string)
export type ConcessionsPct = (number | string)
export type StagingFlat = (number | string)
export type MiscPct = (number | string)
export type CashTargetMargin = (number | string)
export type WholesaleInvestorPct = (number | string)
export type MinAssignmentSpread = (number | string)
export type PropertyId2 = string
export type AssumptionSetId = string
export type EngineVersion = string
export type Status3 = ("ok" | "insufficient_data")
export type UnavailableReason = (string | null)
export type VLow = (number | string | null)
export type VExpected = (number | string | null)
export type VHigh = (number | string | null)
export type Dispersion = (number | string | null)
export type CandidatesUsed = {
[k: string]: unknown | undefined
}[]
export type ValuationConfidence = (number | string | null)
export type Confirmed = (number | string | null)
export type Potential = (number | string | null)
export type Maximum = (number | string | null)
export type Breakdown = {
[k: string]: unknown | undefined
}[]
export type Gross = (number | string | null)
export type Adjusted = (number | string | null)
export type NetRealizable = (number | string | null)
export type EquityPct = (number | string | null)
export type Acquisition = (number | string)
export type Repairs = (number | string)
export type Holding = (number | string)
export type Resale = (number | string)
export type Financing = (number | string)
export type HoldingMonthsBase = (number | string | null)
export type DebtDataPresent = boolean
export type Confidence2 = (number | string)
export type StrategyType = ("cash" | "flip" | "wholesale" | "rental" | "subject_to" | "foreclosure")
export type Scenario = ("conservative" | "expected" | "optimistic")
export type Status4 = ("viable" | "not_viable" | "unavailable" | "requires_human_review")
export type UnavailableReason1 = (string | null)
export type Mao = (number | string | null)
export type AllInBasis = (number | string | null)
export type Profit = (number | string | null)
export type Roi = (number | string | null)
export type MarginOfSafety = (number | string | null)
export type Notices = string[]
export type PropertyId3 = string
export type OfferPrice = (number | string)
export type ConfirmedPayoffs = (number | string)
export type PotentialPayoffs = (number | string)
export type ClosingCosts = (number | string)
export type ProceedsLow = (number | string)
export type ProceedsExpected = (number | string)
export type ProceedsHigh = (number | string)
export type BuyerBasis = (number | string)
export type Profit1 = (number | string)
export type Roi1 = (number | string | null)
export type IsShortSale = boolean
export type Label = (string | null)
export type Points = OfferPoint[]
export type Interpolatable = boolean
export type PropertyId4 = string
export type ScoringConfigId = string
export type Fos = (number | string)
export type Distress = (number | string)
export type DataConfidence = (number | string)
export type Risk = (number | string)
export type Overall = (number | string)
export type GatesApplied = string[]
export type IsRankable = boolean
export type RecommendedAlternatives = StrategyType[]

export interface TrackedValue {
value: Value
confidence: Confidence
source_kind: SourceKind
is_estimated: IsEstimated
fact_id?: FactId
as_of?: AsOf
null_reason?: (NullReason | null)
}
export interface ExtractedFactDraft {
report_id: ReportId
extraction_unit_id: ExtractionUnitId
entity_type: EntityType
entity_local_id: EntityLocalId
field_path: FieldPath
value_raw?: ValueRaw
value_parsed?: ValueParsed
value_text?: ValueText
value_date?: ValueDate
value_bool?: ValueBool
unit?: Unit
as_of_date?: AsOfDate
page_number: PageNumber
snippet: Snippet
extraction_confidence: ExtractionConfidence
null_reason?: (NullReason | null)
source_kind?: SourceKind1
}
export interface NormalizedProperty {
property_id: PropertyId
apn?: Apn
address: AddressBlock
attributes?: PropertyAttributes
ownership?: OwnershipBlock
valuation_candidates?: ValuationCandidates
mortgages?: Mortgages
liens?: Liens
foreclosure?: (ForeclosureState | null)
bankruptcies?: Bankruptcies
taxes?: TaxBlock
hoa?: HoaBlock
rental?: RentalBlock
listings?: Listings
comparables?: Comparables
condition?: (ConditionSignal | null)
data_quality?: DataQualityBlock
open_flags?: OpenFlags
resolution_version: ResolutionVersion
}
export interface AddressBlock {
line1?: Line1
unit?: Unit1
city?: City
state?: State
zip5?: Zip5
county?: County
fips?: Fips
lat?: Lat
lng?: Lng
}
export interface PropertyAttributes {
property_type?: (TrackedValue | null)
beds?: (TrackedValue | null)
baths?: (TrackedValue | null)
sqft?: (TrackedValue | null)
lot_sqft?: (TrackedValue | null)
year_built?: (TrackedValue | null)
units?: (TrackedValue | null)
}
export interface OwnershipBlock {
owner_names?: OwnerNames
entity_type?: EntityType1
is_owner_occupied?: IsOwnerOccupied
is_absentee?: IsAbsentee
ownership_start_date?: OwnershipStartDate
purchase_price?: (TrackedValue | null)
years_owned?: YearsOwned
}
export interface ValuationCandidate {
valuation_type: ValuationType
value: TrackedValue
value_low?: ValueLow
value_high?: ValueHigh
as_of?: AsOf1
reported_confidence?: ReportedConfidence
weight_hint?: WeightHint
}
export interface MortgageRecord {
position: Position
lender?: Lender
original_amount?: (TrackedValue | null)
rate?: Rate
term_months?: TermMonths
origination_date?: OriginationDate
estimated_balance?: (TrackedValue | null)
balance_method?: BalanceMethod
is_open?: IsOpen
}
export interface LienRecord {
lien_type: LienType
amount?: (TrackedValue | null)
amount_is_estimated?: AmountIsEstimated
status?: Status
attachment_basis: AttachmentBasis
attachment_confidence: AttachmentConfidence
recording_date?: RecordingDate
priority?: Priority
}
export interface ForeclosureState {
stage: Stage
nod_date?: NodDate
nts_date?: NtsDate
original_sale_date?: OriginalSaleDate
current_sale_date?: CurrentSaleDate
published_bid?: (TrackedValue | null)
default_amount?: (TrackedValue | null)
postponement_count?: PostponementCount
rescission_count?: RescissionCount
trustee?: Trustee
is_active?: IsActive
}
export interface BankruptcyRecord {
chapter: Chapter
status: Status1
filing_date?: FilingDate
discharge_date?: DischargeDate
sequence?: Sequence
}
export interface TaxBlock {
annual_taxes?: (TrackedValue | null)
assessed_value?: (TrackedValue | null)
delinquent_amount?: (TrackedValue | null)
delinquent_years?: DelinquentYears
}
export interface HoaBlock {
monthly_dues?: (TrackedValue | null)
arrears?: (TrackedValue | null)
has_lien?: HasLien
}
export interface RentalBlock {
rent_estimate?: (TrackedValue | null)
source?: Source
}
export interface ListingRecord {
list_date: ListDate
delist_date?: DelistDate
price?: (TrackedValue | null)
status: Status2
dom?: Dom
}
export interface ComparableSale {
address: Address
sale_date?: SaleDate
price?: (TrackedValue | null)
sqft?: Sqft
distance?: Distance
similarity?: Similarity
included?: Included
}
export interface ConditionSignal {
condition: Condition
evidence?: Evidence
}
export interface DataQualityBlock {
critical_field_coverage?: CriticalFieldCoverage
source_counts_by_field?: SourceCountsByField
conflict_count?: ConflictCount
material_conflict_count?: MaterialConflictCount
verified_field_count?: VerifiedFieldCount
ocr_applied?: OcrApplied
newest_report_date?: NewestReportDate
mean_extraction_confidence?: MeanExtractionConfidence
}
export interface SourceCountsByField {
[k: string]: number | undefined
}
export interface FlagSummary {
type: FlagType
severity?: Severity
is_gating?: IsGating
financial_impact?: FinancialImpact
}
export interface FlagRequest {
property_id: PropertyId1
flag_type: FlagType
payload: Payload
financial_impact_usd?: FinancialImpactUsd
raised_by: RaisedBy
dedupe_key: DedupeKey
logical_key?: LogicalKey
fingerprint?: Fingerprint
}
export interface Payload {
[k: string]: unknown | undefined
}
export interface FilterClause {
field: Field
op: Op
value?: Value1
}
export interface Value1 {
[k: string]: unknown | undefined
}
export interface MoneyResponse {
value: Value2
confidence: Confidence1
source_kind: SourceKind
is_estimated: IsEstimated1
null_reason?: (NullReason | null)
}
export interface JobPayload {
name: Name
payload?: Payload1
dedupe_key?: DedupeKey1
}
export interface Payload1 {
[k: string]: unknown | undefined
}
export interface AssumptionSet {
id: Id
version: Version
name: Name1
acquisition: AcquisitionCosts
repairs: RepairAssumptions
holding: HoldingAssumptions
resale: ResaleAssumptions
strategy: StrategyAssumptions
attachment_probability: AttachmentProbability
unknown_lien_medians: UnknownLienMedians
valuation_weights: ValuationWeights
}
export interface AcquisitionCosts {
closing_pct: ClosingPct
title_pct: TitlePct
escrow_flat: EscrowFlat
transfer_tax_lookup_key?: TransferTaxLookupKey
financing_points: FinancingPoints
financing_flat: FinancingFlat
inspection_flat: InspectionFlat
legal_flat: LegalFlat
acq_fee_pct: AcqFeePct
}
export interface RepairAssumptions {
psf_by_condition: PsfByCondition
low_multiplier: LowMultiplier
high_multiplier: HighMultiplier
regional_index: RegionalIndex
}
export interface PsfByCondition {
[k: string]: (number | string) | undefined
}
export interface HoldingAssumptions {
insurance_pct_yr: InsurancePctYr
utilities_monthly: UtilitiesMonthly
maintenance_pct_yr: MaintenancePctYr
acquisition_months: AcquisitionMonths
repair_months_by_condition: RepairMonthsByCondition
market_days_default: MarketDaysDefault
}
export interface RepairMonthsByCondition {
[k: string]: (number | string) | undefined
}
export interface ResaleAssumptions {
commission_pct: CommissionPct
seller_closing_pct: SellerClosingPct
concessions_pct: ConcessionsPct
staging_flat: StagingFlat
misc_pct: MiscPct
}
export interface StrategyAssumptions {
cash_target_margin: CashTargetMargin
flip_target_margin_by_arv_band: FlipTargetMarginByArvBand
wholesale_investor_pct: WholesaleInvestorPct
min_assignment_spread: MinAssignmentSpread
hard_money: HardMoney
rental: Rental
}
export interface FlipTargetMarginByArvBand {
[k: string]: (number | string) | undefined
}
export interface HardMoney {
[k: string]: (number | string) | undefined
}
export interface Rental {
[k: string]: (number | string) | undefined
}
export interface AttachmentProbability {
[k: string]: (number | string) | undefined
}
export interface UnknownLienMedians {
[k: string]: (number | string) | undefined
}
export interface ValuationWeights {
[k: string]: (number | string) | undefined
}
export interface UnderwritingResult {
property_id: PropertyId2
assumption_set_id: AssumptionSetId
engine_version: EngineVersion
status: Status3
unavailable_reason?: UnavailableReason
value?: ValueBlock
liabilities?: LiabilityBlock
equity?: Equity
costs?: Costs
holding_months_base?: HoldingMonthsBase
debt_data_present?: DebtDataPresent
confidence?: Confidence2
}
export interface ValueBlock {
v_low?: VLow
v_expected?: VExpected
v_high?: VHigh
dispersion?: Dispersion
arv_by_scenario?: ArvByScenario
candidates_used?: CandidatesUsed
valuation_confidence?: ValuationConfidence
}
export interface ArvByScenario {
[k: string]: (number | string | null) | undefined
}
export interface LiabilityBlock {
confirmed?: Confirmed
potential?: Potential
maximum?: Maximum
breakdown?: Breakdown
}
export interface Equity {
[k: string]: EquityBlock | undefined
}
export interface EquityBlock {
gross?: Gross
adjusted?: Adjusted
net_realizable?: NetRealizable
equity_pct?: EquityPct
}
export interface Costs {
[k: string]: CostBlock | undefined
}
export interface CostBlock {
acquisition?: Acquisition
repairs?: Repairs
holding?: Holding
resale?: Resale
financing?: Financing
}
export interface StrategyResult {
strategy: StrategyType
scenario: Scenario
status: Status4
unavailable_reason?: UnavailableReason1
mao?: Mao
all_in_basis?: AllInBasis
profit?: Profit
roi?: Roi
margin_of_safety?: MarginOfSafety
metrics?: Metrics
inputs_echo?: InputsEcho
notices?: Notices
}
export interface Metrics {
[k: string]: (number | string | null) | undefined
}
export interface InputsEcho {
[k: string]: (number | string) | undefined
}
export interface OfferGrid {
property_id: PropertyId3
points: Points
interpolatable?: Interpolatable
}
export interface OfferPoint {
offer_price: OfferPrice
scenario: Scenario
confirmed_payoffs: ConfirmedPayoffs
potential_payoffs: PotentialPayoffs
closing_costs: ClosingCosts
proceeds_low: ProceedsLow
proceeds_expected: ProceedsExpected
proceeds_high: ProceedsHigh
buyer_basis: BuyerBasis
profit: Profit1
roi?: Roi1
is_short_sale: IsShortSale
label?: Label
}
export interface ScoreSet {
property_id: PropertyId4
scoring_config_id: ScoringConfigId
fos: Fos
distress: Distress
data_confidence: DataConfidence
risk: Risk
overall: Overall
components: Components
gates_applied: GatesApplied
is_rankable: IsRankable
recommended_strategy?: (StrategyType | null)
recommended_alternatives?: RecommendedAlternatives
}
export interface Components {
[k: string]: (number | string) | undefined
}
