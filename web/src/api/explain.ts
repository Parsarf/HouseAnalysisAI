/** ExplanationTrace API types + client calls (audit-trace system). */
import type { Scenario, StrategyType } from "./types";

export interface ExplanationInput {
  name: string;
  value: unknown;
  display_value: string | null;
  note: string | null;
  source_fact_id: string | null;
}

export interface ExplanationStep {
  order: number;
  label: string;
  formula: string | null;
  substitution: string | null;
  result: unknown;
  display_result: string | null;
}

export interface ExplanationAssumption {
  name: string;
  value: unknown;
  display_value: string | null;
  assumption_set_id: string | null;
  note: string | null;
}

export interface ExplanationSource {
  fact_id: string | null;
  report_id: string | null;
  report_name: string | null;
  vendor: string | null;
  report_type: string | null;
  source_kind: string | null;
  page_number: number | null;
  snippet: string | null;
  value_raw: string | null;
  value_parsed: string | null;
  extraction_confidence: number | null;
  extraction_unit_id: string | null;
  ocr_applied: boolean;
  is_active: boolean;
  is_superseded: boolean;
  is_winner: boolean;
  prompt_version: string | null;
  model: string | null;
  field_path: string | null;
  source_url: string | null;
}

export interface ExplanationCandidate {
  value: unknown;
  display_value: string | null;
  confidence: string | number | null;
  source_kind: string | null;
  origin: "reported" | "extracted" | "derived" | "estimated" | "manual" | null;
  derivation_inputs: ExplanationInput[];
  is_winner: boolean;
  reason: string | null;
  source: ExplanationSource | null;
}

export interface ExplanationResolution {
  method: string | null;
  winner_description: string | null;
  reason: string | null;
  resolution_version?: string | null;
}

export interface ExplanationConflict {
  description: string;
  magnitude: string | null;
  fields: string[];
  flag_type: string | null;
}

export interface ExplanationSensitivity {
  question: string;
  effect: string;
  delta: unknown;
}

export type ValueKind = "reported" | "extracted" | "manual" | "resolved" | "derived" | "calculated" | "estimated";

export interface ExplanationTrace {
  key: string;
  title: string;
  description: string;
  value: unknown;
  display_value: string | null;
  value_kind: ValueKind;
  confidence: string | number | null;
  data_confidence: string | number | null;
  engine: string | null;
  engine_version: string | null;
  formula: string | null;
  formula_display: string | null;
  inputs: ExplanationInput[];
  steps: ExplanationStep[];
  assumptions: ExplanationAssumption[];
  source_facts: ExplanationSource[];
  candidates: ExplanationCandidate[];
  resolution: ExplanationResolution | null;
  warnings: string[];
  unresolved_dependencies: string[];
  conflicts: ExplanationConflict[];
  sensitivity: ExplanationSensitivity[];
  assumption_set_id: string | null;
  scoring_config_id: string | null;
  computed_at: string | null;
  children: ExplanationTrace[];
}

export interface ReportSourcePage {
  report_id: string;
  page: number;
  page_count: number;
  text: string;
  vendor: string | null;
  report_type: string | null;
  snippet?: string | null;
}

import { get, json as postJson } from "./client";
export type { Scenario, StrategyType };

export function getExplanation(propertyId: string, key: string): Promise<ExplanationTrace> {
  return get(`/properties/${propertyId}/explain/${key}`);
}

export function getExplainKeys(): Promise<{ keys: string[] }> {
  return get("/explain/keys");
}

export function explainBatch(propertyId: string, keys: string[]): Promise<{ property_id: string; traces: ExplanationTrace[]; missing_keys: string[] }> {
  return postJson(`/properties/${propertyId}/explain/batch`, "POST", { keys });
}

export function getReportSourcePage(reportId: string, page: number): Promise<ReportSourcePage> {
  return get(`/reports/${reportId}/source?page=${page}`);
}
