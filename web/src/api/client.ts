// Typed fetch client for the ACQ REST API (spec §16). One error envelope:
// {error: {code, message, details}} — surfaced as ApiError.
import {
  AnalysisPayload,
  ApiError,
  ApiErrorBody,
  BatchStatus,
  EvidenceResponse,
  FactSubmission,
  FilterClause,
  FlagListResponse,
  MeResponse,
  MergeRequest,
  OfferPoint,
  OfferRequest,
  PropertyListItem,
  PropertyListResponse,
  PropertyPatch,
  QuickAddRequest,
  ResolveFlagRequest,
  ResolveFlagResponse,
  Scenario,
  TimelineEvent,
  UploadResponse,
} from "./types";

const API_ROOT = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
const BASE = `${API_ROOT}/api`;

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
    throw new ApiError(response.status, body);
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

export interface ListPropertiesParams {
  sort?: string;
  order?: "asc" | "desc";
  filters?: FilterClause[];
  cursor?: string | null;
  limit?: number;
}

export function listProperties(params: ListPropertiesParams = {}): Promise<PropertyListResponse> {
  const search = new URLSearchParams();
  if (params.sort) search.set("sort", params.sort);
  if (params.order) search.set("order", params.order);
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.cursor) search.set("cursor", params.cursor);
  if (params.filters && params.filters.length > 0) search.set("filters", JSON.stringify(params.filters));
  const qs = search.toString();
  return get(`/properties${qs ? `?${qs}` : ""}`);
}

export function getProperty(propertyId: string): Promise<PropertyListItem> {
  return get(`/properties/${encodeURIComponent(propertyId)}`);
}

export function updateProperty(propertyId: string, changes: PropertyPatch): Promise<PropertyListItem> {
  return json(`/properties/${encodeURIComponent(propertyId)}`, "PATCH", changes);
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

export function resolveFlag(flagId: string, resolution: ResolveFlagRequest): Promise<ResolveFlagResponse> {
  return json(`/flags/${encodeURIComponent(flagId)}/resolve`, "POST", resolution);
}
