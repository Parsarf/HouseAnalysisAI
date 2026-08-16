// MSW handlers covering the implemented endpoints in api/app.py plus the
// properties/analysis/offers/flags endpoints from spec §16 that the UI builds
// against before the backend exists.
import { http, HttpResponse } from "msw";
import { analyses, batches, flags, properties } from "./data";
import type { OfferRequest, PropertyPatch, ResolveFlagRequest } from "../api/types";
import type { OfferPoint } from "../types";

// Relative paths work in the browser; Node tests need an absolute origin,
// so handlers are built by createHandlers(origin) — see server.ts.

function error(status: number, code: string, message: string) {
  return HttpResponse.json({ error: { code, message, details: {} } }, { status });
}

function notFound(what: string) {
  return error(404, "not_found", `${what} not found`);
}

export function createHandlers(origin = "") {
  const BASE = `${origin}/api`;
  return [
    http.get("/healthz", () => HttpResponse.json({ status: "ok" })),

    http.post(`${BASE}/auth/login`, async ({ request }) => {
    const form = await request.formData();
    if (form.get("password") !== "mock-password") {
      return error(401, "invalid_input", "invalid credentials");
    }
    return HttpResponse.json({ ok: true });
  }),

    http.get(`${BASE}/me`, () => HttpResponse.json({ id: "owner", read_only: false })),

    http.post(`${BASE}/filter/validate`, async ({ request }) => {
    const filters = await request.json();
    return HttpResponse.json({ filters });
  }),

    http.post(`${BASE}/uploads`, async ({ request }) => {
    const form = await request.formData();
    const files = form.getAll("files");
    const batchId = crypto.randomUUID();
    batches[batchId] = {
      id: batchId, status: "uploaded", total: files.length, completed: 0, failed: 0, estimated_cost_usd: null,
    };
    return HttpResponse.json({
      batch_id: batchId,
      report_ids: files.map(() => crypto.randomUUID()),
      count: files.length,
    });
  }),

    http.get(`${BASE}/batches/:batchId`, ({ params }) => {
    const batch = batches[String(params.batchId)];
    return batch ? HttpResponse.json(batch) : notFound("batch");
  }),

    http.get(`${BASE}/properties`, () => HttpResponse.json({ items: properties, next_cursor: null })),

    http.get(`${BASE}/properties/:propertyId`, ({ params }) => {
    const property = properties.find((item) => item.id === params.propertyId);
    return property ? HttpResponse.json(property) : notFound("property");
  }),

    http.patch(`${BASE}/properties/:propertyId`, async ({ params, request }) => {
    const property = properties.find((item) => item.id === params.propertyId);
    if (!property) return notFound("property");
    const changes = (await request.json()) as PropertyPatch;
    if (changes.pipeline_status !== undefined) property.status = changes.pipeline_status;
    if (changes.tags !== undefined) property.tags = changes.tags;
    if (changes.gut_rating !== undefined) property.gut_rating = changes.gut_rating;
    return HttpResponse.json(property);
  }),

    http.get(`${BASE}/properties/:propertyId/analysis`, ({ params, request }) => {
    const analysis = analyses[String(params.propertyId)];
    if (!analysis) return notFound("analysis");
    const scenario = new URL(request.url).searchParams.get("scenario") ?? "expected";
    return HttpResponse.json({ ...analysis, scenario });
  }),

    http.get(`${BASE}/properties/:propertyId/timeline`, ({ params }) => {
    const analysis = analyses[String(params.propertyId)];
    if (!analysis) return notFound("property");
    return HttpResponse.json({ items: analysis.timeline });
  }),

    http.get(`${BASE}/properties/:propertyId/evidence/:fieldPath`, ({ params }) => {
    const analysis = analyses[String(params.propertyId)];
    if (!analysis) return notFound("property");
    const fieldPath = String(params.fieldPath);
    return HttpResponse.json({
      property_id: analysis.property_id,
      field_path: fieldPath,
      resolved: { value: "500000", confidence: 0.9, source_kind: "report", is_estimated: false },
      method: "priority",
      candidates: [
        {
          fact_id: crypto.randomUUID(), value_raw: "$500,000", value_parsed: "500000",
          source_kind: "report", extraction_confidence: 0.9, page_number: 3,
          snippet: `estimated value of ${fieldPath}`, report_id: crypto.randomUUID(), is_resolved: true, score: "0.91",
        },
      ],
      overrides: [],
    });
  }),

    http.post(`${BASE}/properties/:propertyId/offers`, async ({ params, request }) => {
    const analysis = analyses[String(params.propertyId)];
    if (!analysis) return notFound("property");
    const offer = (await request.json()) as OfferRequest;
    const scenario = offer.scenario ?? "expected";
    // Canned authoritative math for mocks only — the real engine lives server-side.
    const grid = analysis.offers;
    const nearest = grid?.points.reduce((best, point) =>
      Math.abs(Number(point.offer_price) - Number(offer.offer_price)) <
      Math.abs(Number(best.offer_price) - Number(offer.offer_price)) ? point : best,
    );
    const point: OfferPoint = nearest
      ? { ...nearest, offer_price: offer.offer_price, scenario }
      : {
          offer_price: offer.offer_price, scenario,
          confirmed_payoffs: "0", potential_payoffs: "0", closing_costs: "0",
          proceeds_low: "0", proceeds_expected: "0", proceeds_high: "0",
          buyer_basis: offer.offer_price, profit: "0", roi: null,
          is_short_sale: false,
        };
    return HttpResponse.json(point);
  }),

    http.post(`${BASE}/properties/:propertyId/recompute`, ({ params }) => {
    if (!analyses[String(params.propertyId)]) return notFound("property");
    return HttpResponse.json({ enqueued: true });
  }),

    http.post(`${BASE}/properties/:propertyId/facts`, ({ params }) => {
    if (!analyses[String(params.propertyId)]) return notFound("property");
    return HttpResponse.json({ fact_id: crypto.randomUUID() });
  }),

    http.post(`${BASE}/properties/quick-add`, async ({ request }) => {
    const body = (await request.json()) as { address_line1: string; city?: string; state?: string; zip5?: string };
    const property = {
      id: crypto.randomUUID(),
      address: body.address_line1,
      city: body.city ?? null,
      state: body.state ?? null,
      zip5: body.zip5 ?? null,
      status: "new",
      tags: [],
      gut_rating: null,
    };
    properties.push(property);
    return HttpResponse.json(property, { status: 201 });
  }),

    http.post(`${BASE}/properties/merge`, async ({ request }) => {
    const { source_id, target_id } = (await request.json()) as { source_id: string; target_id: string };
    const target = properties.find((item) => item.id === target_id);
    const sourceIndex = properties.findIndex((item) => item.id === source_id);
    if (!target || sourceIndex < 0) return notFound("property");
    properties.splice(sourceIndex, 1);
    return HttpResponse.json(target);
  }),

    http.post(`${BASE}/properties/unmerge`, () => HttpResponse.json({ unmerged: true })),

    http.get(`${BASE}/flags`, ({ request }) => {
    const status = new URL(request.url).searchParams.get("status") ?? "open";
    return HttpResponse.json({ items: flags.filter((flag) => flag.status === status), next_cursor: null });
  }),

    http.post(`${BASE}/flags/:flagId/resolve`, async ({ params, request }) => {
    const flag = flags.find((item) => item.id === params.flagId);
    if (!flag) return notFound("flag");
    const body = (await request.json()) as ResolveFlagRequest;
    if (body.resolution !== "accept" && body.resolution !== "reject") {
      return error(422, "invalid_input", "resolution must be accept or reject");
    }
    flag.status = "resolved";
    return HttpResponse.json({ id: flag.id, status: "resolved", score_delta: "0", rank_delta: 0 });
  }),
  ];
}

export const handlers = createHandlers();
