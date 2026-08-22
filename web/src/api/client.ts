// Typed fetch client for the ACQ REST API (spec §16). One error envelope:
// {error: {code, message, details}} — surfaced as ApiError.
import {
  AnalysisPayload,
  AssumptionSetRecord,
  ApiError,
  ApiErrorBody,
  BatchEstimate,
  BatchStatus,
  ChangeEvent,
  DashboardResponse,
  EvidenceResponse,
  FactSubmission,
  FilterClause,
  FlagListResponse,
  MeResponse,
  MergeRequest,
  NoteRecord,
  OfferPoint,
  OfferRequest,
  ProblemsResponse,
  PropertyListItem,
  PropertyListResponse,
  PropertyPatch,
  QuickAddRequest,
  RankingsResponse,
  ReportRecord,
  ResolveFlagRequest,
  ResolveFlagResponse,
  SavedView,
  Scenario,
  TimelineEvent,
  UnderwritingResult,
  UploadResponse,
} from "./types";

const API_ROOT = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
const BASE = `${API_ROOT}/api`;

let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { credentials: "include", ...init });
  if (!response.ok) {
    let body: ApiErrorBody = { code: "internal", message: response.statusText };
    try {
      const parsed = await response.json();
      if (parsed?.error) body = parsed.error;
    } catch {
      // Non-JSON error body; keep the fallback.
    }
    const apiError = new ApiError(response.status, body);
    if (response.status === 401) unauthorizedHandler?.();
    throw apiError;
  }
  return response.json() as Promise<T>;
}

function json<T>(path: string, method: string, payload?: unknown): Promise<T> {
  return request<T>(`${BASE}${path}`, {
    method,
    headers: payload === undefined ? undefined : { "Content-Type": "application/json" },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
}

function get<T>(path: string): Promise<T> {
  return request<T>(`${BASE}${path}`);
}

export function healthz(): Promise<{ status: string }> {
  return request(`${API_ROOT}/healthz`);
}

export function login(password: string, readOnly = false): Promise<{ ok: boolean }> {
  const body = new FormData();
  body.append("password", password);
  body.append("read_only", String(readOnly));
  return request(`${BASE}/auth/login`, { method: "POST", body });
}

export function me(): Promise<MeResponse> {
  return get("/me");
}

export function validateFilter(filters: FilterClause[]): Promise<{ filters: FilterClause[] }> {
  return json("/filter/validate", "POST", filters);
}

export function uploadReports(files: File[], batchName?: string): Promise<UploadResponse> {
  const body = new FormData();
  files.forEach((file) => body.append("files", file));
  if (batchName) body.append("batch_name", batchName);
  return request(`${BASE}/uploads`, { method: "POST", body });
}

export function getBatch(batchId: string): Promise<BatchStatus> {
  return get(`/batches/${batchId}`);
}

export function estimateBatch(batchId: string): Promise<BatchEstimate> {
  return json(`/batches/${encodeURIComponent(batchId)}/estimate`, "POST");
}

export function startBatch(batchId: string): Promise<BatchStatus> {
  return json(`/batches/${encodeURIComponent(batchId)}/start`, "POST");
}

export function ingestPaste(text: string, batchName?: string): Promise<UploadResponse> {
  return json("/ingest/paste", "POST", { text, batch_name: batchName ?? null });
}

export interface ListPropertiesParams {
  sort?: string;
  order?: "asc" | "desc";
  filters?: FilterClause[];
  cursor?: string | null;
  limit?: number;
  showArchived?: boolean;
}

export function listProperties(params: ListPropertiesParams = {}): Promise<PropertyListResponse> {
  const search = new URLSearchParams();
  if (params.sort) search.set("sort", `${params.order === "desc" ? "-" : ""}${params.sort}`);
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.cursor) search.set("cursor", params.cursor);
  if (params.filters && params.filters.length > 0) search.set("filters", JSON.stringify(params.filters));
  if (params.showArchived) search.set("show_archived", "true");
  const qs = search.toString();
  return get(`/properties${qs ? `?${qs}` : ""}`);
}

export function getProperty(propertyId: string): Promise<PropertyListItem> {
  return get(`/properties/${encodeURIComponent(propertyId)}`);
}

export function updateProperty(propertyId: string, changes: PropertyPatch): Promise<PropertyListItem> {
  return json(`/properties/${encodeURIComponent(propertyId)}`, "PATCH", changes);
}

export function archiveProperty(propertyId: string): Promise<{ property_id: string; archived_at: string }> {
  return json(`/properties/${encodeURIComponent(propertyId)}/archive`, "POST");
}

export function restoreProperty(propertyId: string): Promise<{ property_id: string; archived_at: null }> {
  return json(`/properties/${encodeURIComponent(propertyId)}/restore`, "POST");
}

export interface OwnerLinkCandidate {
  owner_id: string; owner_name: string | null; confidence: "high" | "moderate" | "low";
  reasons: string[]; property_ids: string[];
}

export interface UnlinkedOwnerProfile {
  report_id: string; file_name: string; owner_id: string; owner_name: string | null;
  link_candidates: OwnerLinkCandidate[];
}

export function listUnlinkedOwnerProfiles(): Promise<{ items: UnlinkedOwnerProfile[] }> {
  return get("/owner-profiles/unlinked");
}

export function confirmOwnerProfileLink(reportId: string, ownerId: string): Promise<{ linked: boolean }> {
  return json(`/owner-profiles/${encodeURIComponent(reportId)}/link`, "POST", { owner_id: ownerId });
}

export interface OutreachDraft {
  draft_id: string; subject: string; body: string; offer_price: string;
  recipients: Array<{ id: string; value: string; source: string; confidence: string | null; association_warning?: string | null }>;
  mailing_addresses: string[]; recipient_selected: null; disclosure: string;
}

export function createOutreachDraft(propertyId: string, payload: Record<string, unknown> = {}): Promise<OutreachDraft> {
  return json(`/properties/${encodeURIComponent(propertyId)}/outreach-draft`, "POST", payload);
}

export function updateOutreachDraft(
  propertyId: string, draftId: string,
  payload: { subject: string; body: string; recipient?: string | null; status?: "draft" | "sent" },
): Promise<Record<string, unknown>> {
  return json(`/properties/${encodeURIComponent(propertyId)}/outreach-drafts/${encodeURIComponent(draftId)}`, "PATCH", payload);
}

export async function streamChat(
  messages: Array<{ role: "user" | "assistant"; content: string }>, propertyIds: string[],
  onDelta: (delta: string) => void, sessionId?: string | null,
): Promise<string> {
  const response = await fetch(`${BASE}/chat`, {
    method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages, property_ids: propertyIds, session_id: sessionId ?? null }),
  });
  if (!response.ok || !response.body) throw new Error(`Chat request failed (${response.status})`);
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
  let returnedSessionId = sessionId ?? "";
  while (true) {
    const { done, value } = await reader.read(); if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n"); buffer = events.pop() ?? "";
    for (const event of events) {
      const line = event.split("\n").find((item) => item.startsWith("data: "));
      if (!line) continue;
      const payload = JSON.parse(line.slice(6)) as { delta?: string; session_id?: string };
      if (payload.delta) onDelta(payload.delta);
      if (payload.session_id) returnedSessionId = payload.session_id;
    }
  }
  if (!returnedSessionId) throw new Error("Chat response omitted its session id");
  return returnedSessionId;
}

/** Full analysis payload; the scenario toggle and offer slider work from this without refetching. */
export function getAnalysis(propertyId: string, scenario?: Scenario): Promise<AnalysisPayload> {
  const qs = scenario ? `?scenario=${encodeURIComponent(scenario)}` : "";
  return get(`/properties/${encodeURIComponent(propertyId)}/analysis${qs}`);
}

export function getTimeline(propertyId: string): Promise<{ items: TimelineEvent[] }> {
  return get(`/properties/${encodeURIComponent(propertyId)}/timeline`);
}

/** Authoritative off-grid offer evaluation (spec §9.3). */
export function postOffer(propertyId: string, offerPrice: string, scenario?: Scenario): Promise<OfferPoint> {
  const body: OfferRequest = { offer_price: offerPrice, scenario };
  return json(`/properties/${encodeURIComponent(propertyId)}/offers`, "POST", body);
}

/** Evidence behind one resolved field (spec §11.7 item 7). */
export function getEvidence(propertyId: string, fieldPath: string): Promise<EvidenceResponse> {
  return get(`/properties/${encodeURIComponent(propertyId)}/evidence/${encodeURIComponent(fieldPath)}`);
}

export function recomputeProperty(propertyId: string, reason?: string): Promise<{ enqueued: boolean }> {
  return json(`/properties/${encodeURIComponent(propertyId)}/recompute`, "POST", { reason: reason ?? null });
}

export function submitFact(propertyId: string, fact: FactSubmission): Promise<{ fact_id: string }> {
  return json(`/properties/${encodeURIComponent(propertyId)}/facts`, "POST", fact);
}

export function quickAddProperty(requestBody: QuickAddRequest): Promise<PropertyListItem> {
  return json("/properties/quick-add", "POST", requestBody);
}

export function mergeProperties(requestBody: MergeRequest): Promise<PropertyListItem> {
  return json("/properties/merge", "POST", requestBody);
}

export function unmergeProperties(requestBody: MergeRequest): Promise<{ unmerged: boolean }> {
  return json("/properties/unmerge", "POST", requestBody);
}

export function listFlags(status: "open" | "resolved" = "open"): Promise<FlagListResponse> {
  return get(`/flags?status=${encodeURIComponent(status)}`);
}

export function listPropertyFlags(propertyId: string, status: "open" | "resolved" | "all" = "open"): Promise<FlagListResponse> {
  return get(`/flags?status=${encodeURIComponent(status)}&property_id=${encodeURIComponent(propertyId)}`);
}

export function resolveFlag(flagId: string, resolution: ResolveFlagRequest): Promise<ResolveFlagResponse> {
  return json(`/flags/${encodeURIComponent(flagId)}/resolve`, "POST", resolution);
}

export function getDashboard(): Promise<DashboardResponse> { return get("/dashboard"); }
export function getRankings(scopeType = "portfolio"): Promise<RankingsResponse> {
  return get(`/rankings?scope_type=${encodeURIComponent(scopeType)}`);
}
export function getChanges(limit = 100): Promise<{ items: ChangeEvent[] }> { return get(`/changes?limit=${limit}`); }
export function getProblems(): Promise<ProblemsResponse> { return get("/problems"); }
export function listSavedViews(): Promise<{ items: SavedView[] }> { return get("/saved-views"); }
export function createSavedView(name: string, filters: FilterClause[]): Promise<SavedView> {
  return json("/saved-views", "POST", { name, filters, columns: {} });
}
export function deleteSavedView(id: string): Promise<{ deleted: string }> {
  return request(`${BASE}/saved-views/${encodeURIComponent(id)}`, { method: "DELETE" });
}
export function listAssumptionSets(): Promise<{ items: AssumptionSetRecord[] }> { return get("/assumption-sets"); }
export function previewAssumptionSet(name: string, params: Record<string, unknown>, propertyId?: string): Promise<{ valid: boolean; underwriting: UnderwritingResult | null }> {
  return json("/assumption-sets/preview", "POST", { name, params, property_id: propertyId ?? null });
}
export function createAssumptionSet(name: string, params: Record<string, unknown>, isDefault: boolean): Promise<{ id: string; name: string; version: number }> {
  return json("/assumption-sets", "POST", { name, params, is_default: isDefault });
}
export function listNotes(propertyId: string): Promise<{ items: NoteRecord[] }> { return get(`/properties/${encodeURIComponent(propertyId)}/notes`); }
export function createNote(propertyId: string, body: string): Promise<NoteRecord> {
  return json(`/properties/${encodeURIComponent(propertyId)}/notes`, "POST", { body });
}
export function listReports(propertyId: string): Promise<{ items: ReportRecord[] }> { return get(`/properties/${encodeURIComponent(propertyId)}/reports`); }
export function createRealizedDeal(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  return json("/realized-deals", "POST", payload);
}
export function exportCsvUrl(filters: FilterClause[], columns: string[]): string {
  const search = new URLSearchParams();
  if (filters.length) search.set("filters", JSON.stringify(filters));
  if (columns.length) search.set("columns", columns.join(","));
  return `${BASE}/exports/csv?${search.toString()}`;
}

async function openHtml(path: string, payload?: unknown): Promise<void> {
  const response = await fetch(`${BASE}${path}`, {
    method: "POST", credentials: "include",
    headers: payload === undefined ? undefined : { "Content-Type": "application/json" },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  if (!response.ok) {
    let body: ApiErrorBody = { code: "internal", message: response.statusText };
    try { const parsed = await response.json(); if (parsed?.error) body = parsed.error; } catch { /* HTML failure */ }
    if (response.status === 401) unauthorizedHandler?.();
    throw new ApiError(response.status, body);
  }
  const url = URL.createObjectURL(new Blob([await response.text()], { type: "text/html" }));
  window.open(url, "_blank", "noopener,noreferrer");
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export function openDealSheet(propertyId: string): Promise<void> {
  return openHtml(`/properties/${encodeURIComponent(propertyId)}/exports/deal-sheet`);
}
export function openNetSheet(propertyId: string, offerPrice: string, scenario: Scenario): Promise<void> {
  return openHtml(`/properties/${encodeURIComponent(propertyId)}/exports/net-sheet`, { offer_price: offerPrice, scenario });
}
