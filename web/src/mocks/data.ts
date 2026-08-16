// Seed data for the MSW mock API. Money values are decimal strings, matching the
// backend wire format (spec §16). Shapes are checked against the generated
// contract types at build time.
import type {
  AnalysisResponse,
  BatchStatus,
  FlagRecord,
  NormalizedProperty,
  OfferGrid,
  OfferPoint,
  PropertyListItem,
  ScoreSet,
  StrategyResult,
  TrackedValue,
  UnderwritingResult,
} from "../api/types";

const PROP_CLEAN = "00000000-0000-0000-0000-000000000001";
const PROP_THIN = "00000000-0000-0000-0000-000000000005";

function tracked(value: string, confidence = 0.9, isEstimated = false): TrackedValue {
  return { value, confidence, source_kind: isEstimated ? "derived" : "report", is_estimated: isEstimated };
}

const cleanNormalized: NormalizedProperty = {
  property_id: PROP_CLEAN,
  apn: "APN-001",
  address: { line1: "1 Main St", city: "Los Angeles", state: "CA", zip5: "90001" },
  attributes: { sqft: tracked("1800", 0.95), beds: tracked("3"), baths: tracked("2") },
  valuation_candidates: [
    { valuation_type: "comp", value: tracked("500000", 0.9), weight_hint: "1" },
    { valuation_type: "avm", value: tracked("492000", 0.8, true), weight_hint: "0.6" },
  ],
  mortgages: [
    { position: "1", lender: "ABC BANK", estimated_balance: tracked("180000", 0.85), rate: "4.25", balance_method: "reported", is_open: true },
  ],
  liens: [],
  open_flags: [],
  resolution_version: "mock-1",
};

const thinNormalized: NormalizedProperty = {
  property_id: PROP_THIN,
  address: { line1: "5 Main St", city: "Pasadena", state: "CA", zip5: "91101" },
  attributes: { beds: tracked("3", 0.85), sqft: tracked("1500", 0.85) },
  valuation_candidates: [],
  mortgages: [],
  liens: [
    { lien_type: "hoa", amount: tracked("4200", 0.6, true), amount_is_estimated: true, status: "open", attachment_basis: "recorded_against_property", attachment_confidence: 0.6 },
  ],
  open_flags: [{ type: "missing_apn", severity: "warning", is_gating: false }],
  resolution_version: "mock-1",
};

const cleanUnderwriting: UnderwritingResult = {
  property_id: PROP_CLEAN,
  assumption_set_id: "00000000-0000-0000-0000-00000000a001",
  engine_version: "mock-1",
  status: "ok",
  value: { v_low: "470000", v_expected: "496000", v_high: "520000", valuation_confidence: "0.85" },
  liabilities: { confirmed: "180000", potential: "0", maximum: "180000", breakdown: [] },
  equity: {
    expected: { gross: "316000", adjusted: "316000", net_realizable: "286000", equity_pct: "0.6371" },
  },
  costs: { expected: { acquisition: "12500", repairs: "36000", holding: "8200", resale: "29760", financing: "0" } },
  debt_data_present: true,
  confidence: "0.85",
};

const cleanStrategies: StrategyResult[] = [
  {
    strategy: "cash", scenario: "expected", status: "viable",
    mao: "215000", all_in_basis: "263700", profit: "222300", roi: "0.843", margin_of_safety: "0.3",
    metrics: {}, inputs_echo: {}, notices: [],
  },
  {
    strategy: "flip", scenario: "expected", status: "viable",
    mao: "230000", all_in_basis: "278000", profit: "188000", roi: "0.676", margin_of_safety: "0.25",
    metrics: {}, inputs_echo: {}, notices: [],
  },
];

const cleanOfferPoints: OfferPoint[] = ["180000", "200000", "220000", "240000", "260000", "280000", "300000", "320000", "340000"].map(
  (price, index) => {
    const offer = Number(price);
    const proceeds = offer - 180000 - Math.round(offer * 0.02);
    return {
      offer_price: price,
      scenario: "expected" as const,
      confirmed_payoffs: "180000",
      potential_payoffs: "0",
      closing_costs: String(Math.round(offer * 0.02)),
      proceeds_low: String(proceeds - 15000),
      proceeds_expected: String(proceeds),
      proceeds_high: String(proceeds + 15000),
      buyer_basis: String(offer + 36000 + 8200),
      profit: String(496000 - (offer + 36000 + 8200 + 29760)),
      roi: "0.5",
      is_short_sale: false,
      label: index === 4 ? "MAO (cash)" : null,
    };
  },
);

const cleanOffers: OfferGrid = { property_id: PROP_CLEAN, points: cleanOfferPoints, interpolatable: true };

const cleanScores: ScoreSet = {
  property_id: PROP_CLEAN,
  scoring_config_id: "00000000-0000-0000-0000-00000000c001",
  fos: "72", distress: "30", data_confidence: "85", risk: "18", overall: "68",
  components: { equity_pct: "0.64", foreclosure_pressure: "0" },
  gates_applied: [], is_rankable: true,
  recommended_strategy: "cash", recommended_alternatives: ["flip"],
};

export const properties: PropertyListItem[] = [
  { id: PROP_CLEAN, address: "1 Main St", address_line1: "1 Main St", apn: "APN-001", city: "Los Angeles", state: "CA", zip5: "90001", status: "pursue", pipeline_status: "pursue", tags: ["priority", "probate"], gut_rating: 4, is_watchlisted: true, next_action: "Call trustee", next_action_date: "2026-08-19", rank: 1, overall_score: "68", open_flags: 0 },
  { id: PROP_THIN, address: "5 Main St", address_line1: "5 Main St", city: "Pasadena", state: "CA", zip5: "91101", status: "reviewing", pipeline_status: "reviewing", tags: ["title-review"], gut_rating: 3, is_watchlisted: false, next_action: "Verify APN", next_action_date: "2026-08-17", rank: 2, overall_score: "44", open_flags: 1 },
];

export const analyses: Record<string, AnalysisResponse> = {
  [PROP_CLEAN]: {
    property_id: PROP_CLEAN,
    scenario: "expected",
    normalized: cleanNormalized,
    underwriting: cleanUnderwriting,
    strategies: cleanStrategies,
    offers: cleanOffers,
    scores: cleanScores,
    flags: [],
    timeline: [
      { date: "2024-03-01", kind: "listing", label: "Listed at $510,000" },
      { date: "2024-05-15", kind: "listing", label: "Delisted" },
    ],
  },
  [PROP_THIN]: {
    property_id: PROP_THIN,
    scenario: "expected",
    normalized: thinNormalized,
    underwriting: null,
    strategies: [],
    offers: null,
    scores: null,
    flags: [{
      id: "00000000-0000-0000-0000-00000000f001",
      property_id: PROP_THIN,
      flag_type: "missing_apn",
      payload: { address: "5 Main St", zip5: "90001" },
      financial_impact_usd: null,
      status: "open",
    }],
    timeline: [],
  },
};

export const batches: Record<string, BatchStatus> = {
  "00000000-0000-0000-0000-00000000ba70": {
    id: "00000000-0000-0000-0000-00000000ba70",
    status: "uploaded", total: 2, completed: 1, failed: 0, estimated_cost_usd: "0.42",
  },
};

export const flags: FlagRecord[] = [
  {
    id: "00000000-0000-0000-0000-00000000f001",
    property_id: PROP_THIN,
    flag_type: "missing_apn",
    severity: "warning",
    is_gating: false,
    payload: { address: "5 Main St", zip5: "90001" },
    financial_impact_usd: null,
    raised_by: "flags",
    status: "open",
    created_at: "2024-06-01T00:00:00Z",
  },
];
