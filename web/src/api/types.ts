// API wire types for the ACQ backend (api/app.py + spec §16).
// Money is always a decimal string on the wire; never use number for money here.
//
// The contract-model interfaces below mirror contracts/models.py field-for-field
// (Decimal -> string, date -> ISO string, UUID -> string). They intentionally use
// string-only money fields, which is stricter than the generated types in
// web/src/types/ (those allow `number | string` because pydantic's schema admits
// both) so UI code can rely on string formatting.

export type SourceKind = "report" | "derived" | "human" | "api" | "pasted";
export type NullReason = "not_present" | "illegible" | "redacted" | "conflicting_in_source";
export type AttachmentBasis = "recorded_against_property" | "owner_named_only" | "unknown";
export type Scenario = "conservative" | "expected" | "optimistic";
export type StrategyType = "cash" | "flip" | "wholesale" | "rental" | "subject_to" | "foreclosure";

export const SCENARIOS: readonly Scenario[] = ["conservative", "expected", "optimistic"];

export type FilterOp = "eq" | "neq" | "gt" | "gte" | "lt" | "lte" | "in" | "between" | "contains" | "is_null";

export const FILTER_OPS: readonly FilterOp[] = ["eq", "neq", "gt", "gte", "lt", "lte", "in", "between", "contains", "is_null"];

export interface FilterClause {
  field: string;
  op: FilterOp;
  value?: unknown;
}

/** Decimal-string money/value envelope. Never a float. */
export interface TrackedValue {
  value: string | null;
  confidence: number; // 0..1
  source_kind: SourceKind;
  is_estimated: boolean;
  fact_id?: string | null;
  as_of?: string | null;
  null_reason?: NullReason | null;
}

export interface AddressBlock {
  line1?: string | null;
  unit?: string | null;
  city?: string | null;
  state?: string | null;
  zip5?: string | null;
  county?: string | null;
  fips?: string | null;
  lat?: string | null;
  lng?: string | null;
}

export interface PropertyAttributes {
  property_type?: TrackedValue | null;
  beds?: TrackedValue | null;
  baths?: TrackedValue | null;
  sqft?: TrackedValue | null;
  lot_sqft?: TrackedValue | null;
  year_built?: TrackedValue | null;
  units?: TrackedValue | null;
}

export interface ValuationCandidate {
  valuation_type: string;
  value: TrackedValue;
  value_low?: string | null;
  value_high?: string | null;
  as_of?: string | null;
  reported_confidence?: number | null;
  weight_hint?: string | null;
}

export interface MortgageRecord {
  position: string;
  lender?: string | null;
  original_amount?: TrackedValue | null;
  rate?: string | null;
  term_months?: number | null;
  origination_date?: string | null;
  estimated_balance?: TrackedValue | null;
  balance_method: string;
  is_open: boolean;
}

export interface LienRecord {
  lien_type: string;
  amount?: TrackedValue | null;
  amount_is_estimated: boolean;
  status: string;
  attachment_basis: AttachmentBasis;
  attachment_confidence: number;
  recording_date?: string | null;
  priority?: number | null;
}

export interface ForeclosureState {
  stage: string;
  nod_date?: string | null;
  nts_date?: string | null;
  original_sale_date?: string | null;
  current_sale_date?: string | null;
  published_bid?: TrackedValue | null;
  default_amount?: TrackedValue | null;
  postponement_count: number;
  rescission_count: number;
  trustee?: string | null;
  is_active: boolean;
}

export interface FlagSummary {
  type: string;
  severity: string;
  is_gating: boolean;
  financial_impact?: string | null;
}

export interface NormalizedProperty {
  property_id: string;
  apn?: string | null;
  address: AddressBlock;
  attributes?: PropertyAttributes;
  valuation_candidates: ValuationCandidate[];
  mortgages: MortgageRecord[];
  liens: LienRecord[];
  foreclosure?: ForeclosureState | null;
  open_flags: FlagSummary[];
  resolution_version: string;
  // Remaining blocks from contracts.NormalizedProperty (ownership, taxes, hoa,
  // rental, listings, comparables, condition, data_quality, bankruptcies) are
  // not yet rendered by the deal page; add them here when they are.
}

export interface ValueBlock {
  v_low?: string | null;
  v_expected?: string | null;
  v_high?: string | null;
  dispersion?: string | null;
  arv_by_scenario?: Partial<Record<Scenario, string | null>>;
  candidates_used?: Array<Record<string, unknown>>;
  valuation_confidence?: string | null;
}

export interface LiabilityBlock {
  confirmed: string | null;
  potential: string | null;
  maximum: string | null;
  breakdown: Array<Record<string, unknown>>;
}

export interface EquityBlock {
  gross?: string | null;
  adjusted?: string | null;
  net_realizable?: string | null;
  equity_pct?: string | null;
}

export interface CostBlock {
  acquisition: string;
  repairs: string;
  holding: string;
  resale: string;
  financing: string;
}

export interface UnderwritingResult {
  property_id: string;
  assumption_set_id: string;
  engine_version: string;
  status: "ok" | "insufficient_data";
  unavailable_reason?: string | null;
  value: ValueBlock;
  liabilities: LiabilityBlock;
  equity: Partial<Record<Scenario, EquityBlock>>;
  costs: Partial<Record<Scenario, CostBlock>>;
  debt_data_present: boolean;
  confidence: string;
}

export interface StrategyResult {
  strategy: StrategyType;
  scenario: Scenario;
  status: "viable" | "not_viable" | "unavailable" | "requires_human_review";
  unavailable_reason?: string | null;
  mao?: string | null;
  all_in_basis?: string | null;
  profit?: string | null;
  roi?: string | null;
  margin_of_safety?: string | null;
  metrics: Record<string, string | null>;
  inputs_echo: Record<string, string>;
  notices: string[];
}

export interface OfferPoint {
  offer_price: string;
  scenario: Scenario;
  confirmed_payoffs: string;
  potential_payoffs: string;
  closing_costs: string;
  proceeds_low: string;
  proceeds_expected: string;
  proceeds_high: string;
  buyer_basis: string;
  profit: string;
  roi?: string | null;
  is_short_sale: boolean;
  label?: string | null;
}

export interface OfferGrid {
  property_id: string;
  points: OfferPoint[];
  interpolatable: boolean;
}

export interface ScoreSet {
  property_id: string;
  scoring_config_id: string;
  fos: string;
  distress: string;
  data_confidence: string;
  risk: string;
  overall: string;
  components: Record<string, string>;
  gates_applied: string[];
  is_rankable: boolean;
  recommended_strategy?: StrategyType | null;
  recommended_alternatives: StrategyType[];
}

export interface TimelineEvent {
  date?: string | null;
  kind?: string;
  event_date?: string | null;
  event_type?: string;
  label: string;
  amount?: TrackedValue | null;
  source?: string | null;
  details?: Record<string, unknown>;
}

/** One round-trip payload for the deal page (spec §16, WP-11). */
export interface AnalysisPayload {
  property_id: string;
  scenario: Scenario;
  normalized: NormalizedProperty | null;
  underwriting: UnderwritingResult | null;
  strategies: StrategyResult[];
  offers: OfferGrid | null;
  scores: ScoreSet | null;
  flags: FlagRecord[];
  timeline: TimelineEvent[];
}

/** Alias kept for the first cut of the client/mocks. */
export type AnalysisResponse = AnalysisPayload;

/** A competing or resolved value behind one field, for the evidence drawer. */
export interface EvidenceCandidate {
  fact_id?: string | null;
  value_raw?: string | null;
  value_parsed?: string | null;
  value_text?: string | null;
  source_kind: SourceKind;
  extraction_confidence: number;
  page_number?: number | null;
  snippet?: string | null;
  report_id?: string | null;
  is_resolved?: boolean;
  is_winner?: boolean;
  score?: string | null;
}

export interface EvidenceOverride {
  action: "approve" | "reject" | "replace" | "dismiss";
  actor: string;
  at: string;
  value?: string | null;
}

export interface EvidenceResponse {
  property_id?: string;
  field_path: string;
  resolved?: TrackedValue | null;
  method?: string | null;
  resolution?: {
    method?: string | null;
    score?: string | null;
    has_conflict?: boolean;
    verification_state?: string | null;
    winning_fact_id?: string | null;
  } | null;
  candidates: EvidenceCandidate[];
  overrides?: EvidenceOverride[];
}

export interface PropertyListItem {
  id: string;
  address?: string | null;
  address_line1?: string | null;
  apn?: string | null;
  city?: string | null;
  state?: string | null;
  zip5?: string | null;
  status?: string;
  pipeline_status?: string;
  tags?: string[];
  gut_rating?: number | null;
  next_action?: string | null;
  next_action_date?: string | null;
  is_watchlisted?: boolean;
  open_flags?: number;
  lat?: string | null;
  lng?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  latest_valuation?: TrackedValue | null;
  // Present once WP-11 serves ranking/underwriting rollups on the list endpoint.
  rank?: number | null;
  rank_total?: number | null;
  overall_score?: string | null;
  value?: TrackedValue | null;
  equity?: TrackedValue | null;
}

export interface PropertyListResponse {
  items: PropertyListItem[];
  next_cursor: string | null;
}

// ---------------------------------------------------------------------------
// API envelopes and non-contract DTOs
// ---------------------------------------------------------------------------

export interface ApiErrorBody {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.details = body.details ?? {};
  }
}

export interface MeResponse {
  id: string;
  read_only: boolean;
}

export interface BatchStatus {
  id: string;
  name?: string | null;
  status: string;
  total: number;
  completed: number;
  failed: number;
  estimated_cost_usd: string | null;
  actual_cost_usd?: string | null;
  awaiting_confirmation?: boolean;
  property_ids?: string[];
  results?: BatchPropertyResult[];
  unresolved_reports?: BatchUnresolvedReport[];
}

export interface BatchPropertyResult {
  property_id: string;
  report_ids: string[];
  address_line1: string | null;
  city: string | null;
  state: string | null;
  zip5: string | null;
  apn: string | null;
}

export interface BatchUnresolvedReport {
  report_id: string;
  reason: string;
  identity?: {
    address_line1?: string | null;
    full_address?: string | null;
    city?: string | null;
    state?: string | null;
    zip5?: string | null;
    apn?: string | null;
  } | null;
}

export interface BatchEstimate {
  batch_id: string;
  report_count: number;
  total_tokens: number;
  estimated_cost_usd: string;
  awaiting_confirmation: boolean;
}

export interface UploadResponse {
  batch_id: string;
  report_ids: string[];
  count: number;
}

export interface PropertyPatch {
  pipeline_status?: string;
  tags?: string[];
  next_action?: string | null;
  next_action_date?: string | null;
  gut_rating?: number | null;
  is_watchlisted?: boolean;
}

export interface OfferRequest {
  /** Decimal string, e.g. "245000.00". */
  offer_price: string;
  scenario?: Scenario;
}

/** Authoritative per-offer math computed server-side (spec §9.2). */
export type OfferResponse = OfferPoint;

export interface FactSubmission {
  report_id: string;
  extraction_unit_id: string;
  entity_type: "property" | "mortgage" | "lien" | "foreclosure" | "listing" | "comparable" | "bankruptcy";
  entity_local_id: string;
  field_path: string;
  value_raw?: string | null;
  value_parsed?: string | null;
  value_text?: string | null;
  value_date?: string | null;
  value_bool?: boolean | null;
  unit?: string | null;
  as_of_date?: string | null;
  page_number: number;
  snippet: string;
  extraction_confidence: number;
  source_kind?: SourceKind;
  note?: string | null;
}

export interface FlagRecord {
  id: string;
  property_id: string;
  flag_type: string;
  severity?: string;
  is_gating?: boolean;
  payload: Record<string, unknown>;
  financial_impact_usd: string | null;
  raised_by?: string;
  status: "open" | "resolved";
  created_at?: string;
  resolution?: string | null;
  resolved_value?: Record<string, unknown> | null;
  note?: string | null;
  resolved_at?: string | null;
}

export interface FlagListResponse {
  items: FlagRecord[];
  next_cursor: string | null;
}

export interface ResolveFlagRequest {
  resolution: "approve" | "reject" | "replace" | "dismiss";
  note?: string | null;
  resolved_value?: Record<string, unknown> | null;
}

export interface ResolveFlagResponse {
  flag?: FlagRecord;
  recompute_enqueued?: boolean;
  id?: string;
  status?: "resolved";
  score_delta?: string | null;
  rank_delta?: number | null;
}

export interface QuickAddRequest {
  address_line1: string;
  city?: string | null;
  state?: string | null;
  zip5?: string | null;
  apn?: string | null;
}

export interface MergeRequest {
  source_id: string;
  target_id: string;
}

export interface DashboardResponse {
  total_properties: number;
  by_status: Record<string, number>;
  open_flags: number;
  failed_reports: number;
  missing_valuation_count: number;
  watchlisted: number;
}

export interface RankingEntry {
  property_id: string;
  rank: number;
  prev_rank: number | null;
  score: string | null;
}

export interface RankingsResponse {
  items: RankingEntry[];
  ranked_at: string | null;
}

export interface SavedView {
  id: string;
  name: string;
  filters: FilterClause[];
  columns: Record<string, unknown>;
  created_at?: string | null;
}

export interface ChangeEvent {
  id: string;
  property_id: string;
  change_type: string;
  field_path?: string | null;
  old_value?: unknown;
  new_value?: unknown;
  score_delta?: string | null;
  detected_at?: string | null;
}

export interface FailedReport {
  id: string;
  batch_id?: string | null;
  failure_reason?: string | null;
  file_path?: string | null;
}

export interface ProblemsResponse {
  gating_flags: FlagRecord[];
  failed_reports: FailedReport[];
}

export interface AssumptionSetRecord {
  id: string;
  name: string;
  version: number;
  is_default: boolean;
  effective_from?: string | null;
  params: Record<string, unknown>;
}

export interface NoteRecord {
  id: string;
  property_id: string;
  body: string;
  created_at?: string | null;
}

export interface ReportRecord {
  id: string;
  report_type?: string | null;
  vendor?: string | null;
  generated_date?: string | null;
  status: string;
  failure_reason?: string | null;
  page_count?: number | null;
  ocr_applied: boolean;
  created_at?: string | null;
}
