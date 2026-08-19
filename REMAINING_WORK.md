# ACQ — What's Left (verified against `main`)

Reviewed by downloading the repo, reading every file, and running the test suite. Everything below is evidence-based, not inferred from `BUILD_STATUS.md`.

---

## 0. Historical review notes

The findings below describe an earlier scaffold state and are retained as audit
history. Use `README.md`, `BUILD_STATUS.md`, and the CI workflow for current
launch status; the API, worker, whole-PDF path, engines, and SPA now exist.

I ran `pytest`. Two hard failures before any feature work is considered:

**B1. The package doesn't import.** `contracts/__init__.py` replaced the star-import with an explicit list, and the list omits **`AssumptionSet`** and **`LienRecord`**. Every module that imports them — `finance`, `strategies`, `pipeline`, and the tests — fails at import time:

```
ImportError: cannot import name 'AssumptionSet' from 'contracts'
```

CI is therefore failing on `pytest`, `mypy`, and probably `lint-imports`. Fix: add both names to the import list in `contracts/__init__.py`. One line.

**B2. After patching B1, `test_underwriting_and_strategy_are_repeatable` fails.** The sample property has no `sqft`, so `flip()` correctly returns `unavailable`, but the test asserts `result.profit is not None`. Either give the fixture sqft or assert the unavailable path. Result after patching B1: `1 failed, 7 passed`.

**B3. Two CI steps cannot pass as written.** `npm ci` requires a `package-lock.json`, which isn't in the repo (and `package.json` pins everything to `"latest"`, so a lockfile has to be generated deliberately). `npm run build` runs `tsc`, and `web/src/main.tsx` reads `property.gut_rating` on a `Property` type that doesn't declare it — a type error. Both steps fail today.

---

## 1. Real progress since the last review

Credit where due — the Phase 0 foundation items largely landed:

| Item | Status |
|---|---|
| D1 contract fork (`contracts/extended.py`) | ✅ Deleted; single canonical `models.py`; guard test added |
| D2 `use_enum_values` bug | ✅ Removed; `.value` access now safe; a lien-split regression test exists |
| D3 broken migration path | ✅ `parents[2]` fixed; explicit `DROP TABLE` downgrade |
| D5 schema gaps | ⚠️ Partial — `flags.dedupe_key`, partial unique indexes on `apn_key`/`address_hash`, trigram index, and the budget columns landed |
| D6 import-linter | ✅ Real `root_packages` + a layered contract; `ingestion → classification` peer import removed |
| D7 upload memory | ✅ Chunked read; magic-byte check now reads 5 bytes, not the whole file |
| D8 in-memory budget | ✅ `ops/db_budget.py` does the atomic UPDATE (though nothing calls it) |
| D9 invalid flag types | ✅ Now uses `FlagType` enum members; `dedupe_key` is content-derived |
| D11 CI | ⚠️ Postgres service, alembic, mypy, lint-imports, web build all added — but two steps can't pass (B3) |
| Phase 0.5 fixtures | ⚠️ 12 `normalized/` + 3 `assumptions/` authored. **`underwriting/`, `strategies/`, `scores/`, `facts/`, `gold/` are still empty** |
| `identity/` package | ⚠️ Now exists (64 lines) but is not wired to anything |

---

## 2. The three structural blockers

### S1. Nothing is wired together. The packages are islands.

`grep` for call sites proves it:

- `identity.attach_report` / `resolve_property` — **never called.** `ingestion/worker.py` still never sets `reports.property_id`. Facts cannot be attributed to a property, so nothing downstream can run on real data.
- `ops.reserve_budget` — **never called.** Extraction can't be budget-gated because extraction doesn't call a model at all.
- `flags.collect_flags` — **never called.** No flag is ever persisted.
- `pipeline.recompute_property` — **never called by any job.** It's a pure function taking an already-built `NormalizedProperty`; it loads nothing and persists nothing.
- `pipeline/worker.py` still registers exactly one handler: `ingest_document`. No `extract_unit`, `recompute_property`, `rank_scope`, `detect_changes`, `nightly`.
- No `rank_scope` / `rankings` writer exists anywhere in the codebase.

**Consequence:** upload a PDF today and you get page text on disk and a `reports` row. Nothing else. There is no path from a document to a score.

### S2. The golden fixtures that define correctness were never computed.

`fixtures/normalized/` and `fixtures/assumptions/` exist, which is good — but `fixtures/underwriting/`, `fixtures/strategies/`, and `fixtures/scores/` are still empty `.gitkeep` directories. That means:

- No engine output is checked against a hand-verified expectation.
- The entire "reproduce fixtures exactly" acceptance criterion is unverified for WP-6/7/8.
- The numeric core has **two** tests total, one of which fails.

The 12 normalized fixtures are also thin — fixture #2 has one valuation candidate, one lien, and nothing else. They don't exercise mortgages, foreclosure timelines, taxes, HOA, condition, or data-quality inputs, so most of each engine is untested even in principle.

### S3. `normalization/` was never rewritten.

Still the 21-line placeholder: `max(extraction_confidence)` per field, returns a record containing only an address and APN, with an `ExtractedFactDraft.model_construct(value_text=None)` hack. No precedence formula, no entity building, no dedupe, no derived balances, no conflict detection, no `data_quality` computation. `BUILD_STATUS.md` marks it BLOCKED on fact fixtures — but authoring 4 `fixtures/facts/*.jsonl` files is a few hours of work and unblocks it immediately.

---

## 3. Per-package gap list

### WP-6 `finance/` — rewritten, but ~40% of the spec is missing

What's right: three-bucket liabilities driven by `attachment_basis`; dispersion clamp `[0.04, 0.30]`; single-candidate forced 0.15 dispersion and confidence cap; unknown-lien medians; released liens excluded; `insufficient_data` when there are no candidates.

What's still wrong or absent:

1. **`attachment_probability` is dead.** The scenario weighting hardcodes `1 / 0.5 / 0` instead of reading `assumptions.attachment_probability` (0.35 owner-only, 0.50 unknown) per lien. The assumption field is parsed and ignored.
2. **`arv_by_scenario` is set to the as-is value.** ARV is the after-repair value. Every consumer (cash, flip, wholesale) therefore uses as-is value as ARV *and* subtracts repairs — understating flip economics on every property.
3. **Acquisition costs = `escrow_flat + inspection_flat` only.** `closing_pct`, `title_pct`, transfer tax, `legal_flat`, `acq_fee_pct`, financing points are all ignored.
4. **Holding costs = `utilities_monthly × acquisition_months` only.** No property taxes, insurance, maintenance, HOA, loan interest, repair duration, or market time. On a $1.2M property this understates holding by roughly an order of magnitude.
5. **Resale omits `staging_flat` and `misc_pct`.** `CostBlock.financing` is always zero.
6. **No amortization-derived mortgage balance.** `estimate_balance()` doesn't exist anywhere in the repo; `historical_rate_index` isn't in the schema. Any property whose report gives an original amount but no current balance silently contributes $0 of debt.
7. **No published-bid reconciliation**, no `bid_mismatch` detection, no undrawn-HELOC handling.
8. **Missing sqft silently yields $0 repairs** rather than an unavailable/flagged result — so `cash` returns a confident, wrong profit.
9. `v_low`/`v_high` ignore `comp_range_low/high` clamps; `valuation_weights` from the assumption set is ignored in favour of per-candidate `weight_hint`; no recency decay, no comp-quality adjustment.

### WP-7 `strategies/` — 4 of 6 strategies, each partial

1. **`cash`**: resale cost = `value × seller_closing_pct` (1%) only — **omits the 5% commission**, overstating profit by ~6% of value on every cash deal. Also ignores the already-computed `costs.resale`.
2. **`flip`**: financing = `purchase × points`, with no interest accrued over the holding period. `metrics["coc"] = profit / purchase_price`, which is not cash-on-cash.
3. **`wholesale`**: gates on `underwriting.confidence >= 0.6` (a 0–1 extraction confidence) where the spec says Data Confidence ≥ 60. Different quantity, coincidentally similar-looking.
4. **`rental`**: **double-counts vacancy** — `opex` includes vacancy *and* `noi = annual_rent × (1 − vacancy) − opex`. OpEx omits taxes, insurance, HOA, and reserves entirely. `cash_flow = noi` with no debt service; no DSCR; no leveraged return.
5. **`subject_to` and `foreclosure` are stubs** — both return a flat `requires_human_review` with no detection conditions and no math. The entire foreclosure strategy (published bid, total obligations, spread, title/occupancy/postponement risk flags, the DCS ≥ 75 cap) is absent.
6. **`offer_grid`**: 9 points at 0.60–1.00 × V is correct. But `buyer_basis = offer` (excludes acquisition, repairs, holding); `profit = scenario_value − offer` (ignores every cost); the expected-proceeds weighting is a hardcoded 0.5 instead of `attachment_probability`; MAO markers are never injected; and there is no linearity test — so the frontend is not yet licensed to interpolate.

### WP-8 `scoring/` — structurally right, numerically not per spec

1. **`equity_pct` reads an arbitrary scenario** — `next(iter(underwriting.equity))` returns whichever key was inserted first (conservative), not expected.
2. **DCS formula is wrong**: it double-counts `verified_field_count` (in two terms) and omits recency and source corroboration entirely.
3. **Distress is partial**: no recency decay (`0.5^(months/18)`), no NTS-within-30-days distinction (it proxies off "does `current_sale_date` exist"), no repeat-filing bonus, no listing failures, no high-equity+distress bonus, no per-category caps — bankruptcies score 12 each, uncapped.
4. **Risk** omits title flags, owner-occupancy, and the federal-tax-lien redemption term.
5. **`components` holds only the 5 FOS inputs.** WP-14's "why is A above B" reads these names as a contract and needs every sub-term.
6. **`scoring_configs` is not used** — all weights, bounds, and point values are hardcoded, so retuning requires a deploy.
7. **`is_rankable` excludes `needs_review`.** The spec says DCS < 40 *caps* the score at 45 and marks it for review; it should still be ranked. As written, those properties disappear from the list entirely.
8. **No ranking job exists** — no `rank_scope`, no writer for the `rankings` table, no `prev_rank` carry-forward.

### WP-3 `identity/` — good start, one real bug, not wired

- **SQL bug:** when `apn_key` is `None`, `or_(Property.apn_key == apn_key, …)` compiles to `apn_key IS NULL OR address_hash = :x` in SQLAlchemy — so a property with no APN matches **any** APN-less property. Guard the clause.
- No trigram/fuzzy tier (0.80–0.92 band), so `possible_duplicate` is never raised despite the index being in place.
- `identity_conflict` raises a bare `ValueError` instead of emitting a `FlagRequest`; the conflict check only compares ZIP, not house number.
- No `merge()` / `unmerge()`.
- Address normalization is a 9-entry regex replacement table, not `usaddress` + USPS rules — no unit designators, no suffixes beyond ST/AVE/RD/BLVD/DR.
- **Not called from anywhere.** See S1.
- No concurrency test (50 parallel resolutions → 1 property).

### WP-4 `extraction/` — replay path only

Present: the system prompt (verbatim from the spec, good), `prompt_version` hashing, grounding validation, a replay-from-recorded-response path.

Absent: the actual provider client, all 12 per-unit JSON schemas, model routing, temperature/tool-mode config, retry/backoff, range sanity checks, cross-field logic, null-reason enforcement, cost accounting, the budget call, **persistence to `extracted_facts`**, `reextract()`, and a real `make eval` (still `print("...not populated yet")`).

### WP-1 `ingestion/` — no OCR, no dedupe check, no failure codes

`get_page_text`/`get_all_page_text` now exist (good, and they take a path rather than a `report_id` — consider whether that's the interface you want other packages coupled to). Still missing: the entire OCR path (`is_scanned` is computed and then ignored), sha256 pre-check before insert (a duplicate upload still raises `IntegrityError` and kills the job), failure-code mapping for encrypted/corrupt/timeout, `document.close()` on PyMuPDF handles, the watched folder, paste ingestion, and the classification/identity calls that used to be there.

### WP-2 `classification/` — unchanged since the first review

Still 8 hardcoded regexes matched against the whole first page, returning 0.9 confidence on first hit; `document_signatures` table exists but is never read; token estimate is still `len(text)//4` (the cost pre-estimate depends on this being within ±10% of a real tokenizer); the fallback windowing off-by-one (`range(0, len, fallback_size-1)`) is still there.

### WP-9 `flags/` — collector only

`collect_flags` produces valid `FlagRequest`s now, but there is no persistence, no `financial_impact_usd` computation (it uses the raw lien amount instead of the equity delta between accepting and rejecting), no resolution workflow, no `apply_override()`, no recompute trigger, and only 2 of the 10 flag types are generated.

### WP-10 `pipeline/` — a pure function, not an orchestrator

No DB loading, no persistence of `deal_scenarios` / `offer_scenarios` / `scores`, no job handlers beyond `ingest_document`, no fan-in counter, no per-property advisory lock, no batch state machine (`awaiting_confirmation` / `paused_budget` exist as columns but nothing drives them), no bulk recompute, no crash-resume test.

### WP-11 `api/` — 8 endpoints of ~30

`/api/properties/{id}/analysis` still returns a hardcoded `{normalized: null, underwriting: null, strategies: [], …}`. No filter-grammar translation (the endpoint just echoes the clauses back), no cursor pagination, no sorting, no offers, no flags, no saved views, no notes, no exports, no batch estimate/confirm, no money envelope, no SPA serving. The global exception handler still returns `str(exc)` to the client.

### WP-12/13 `web/` — effectively not started

27 lines: an upload input and an unstyled list. No router, no table, no filters, no deal page, no evidence drawer, no offer slider, no MSW mocks. `scripts/generate_types.py` still maps every non-primitive to `unknown`, so the generated TypeScript is unusable — replace it with `json-schema-to-typescript`.

### WP-14/15/16/17/18 — as before

`analyst/` and `calibration/` are empty `__init__.py` files. `exports/` has a CSV streamer only (no deal sheet, no net sheet, no full export). `changes/diff.py` still diffs flat dicts and guesses change types from field-name prefixes. `ops/` has the budget primitive and two untested shell scripts — `restore.sh` still untars into the current working directory with no target check, and no CI job exercises backup → wipe → restore.

### Schema items still missing

`lender_aliases`, `historical_rate_index`, `regional_cost_index`, `transfer_tax_rates`, `prompt_versions`; `reports.failure_reason` and `section_match_rate`; `scores.engine_version` / `resolution_version`; the unique constraints on `deal_scenarios` and `rankings`; the `extracted_facts(report_id)` index. Also `db/models.py` covers 6 of ~30 tables while `db/schema.sql` defines all of them — still two sources of truth, so `alembic revision --autogenerate` would produce a large spurious diff.

---

## 4. Honest completion estimate

Roughly **15–20% complete by effort**, and the remaining 80% contains all of the hard parts (normalization, extraction, orchestration, frontend).

| Area | Remaining |
|---|---|
| Fix the red build (B1–B3) | 0.5 days |
| Fixtures + goldens (facts, underwriting, strategies, scores) | 4–5 days |
| Finish WP-6/7/8 to spec | 10–12 days |
| WP-5 normalization (from scratch) | 10–12 days |
| WP-3 identity finish + wiring | 4–5 days |
| WP-1 ingestion (OCR, dedupe, failures) | 5–6 days |
| WP-2 classification (DB signatures, tokenizer) | 4 days |
| WP-4 extraction (client, schemas, gauntlet, persistence) | 12–14 days |
| WP-10 pipeline orchestration | 8–10 days |
| WP-9 flags workflow | 5 days |
| WP-11 API | 9–10 days |
| WP-12/13 frontend | 25–30 days |
| WP-15/18 exports, ops, backups | 8–10 days |
| WP-14/16/17 analyst, changes, calibration | 12–14 days |

**≈ 115–135 dev-days.** Solo: ~6 months. Three devs on the wave plan: ~10–12 weeks.

---

## 5. The next ten commits, in order

1. Add `AssumptionSet` and `LienRecord` to `contracts/__init__.py`; fix the flip test. **The build is red until this lands.**
2. Generate `web/package-lock.json`, pin versions, fix the `gut_rating` type error. CI green.
3. Author `fixtures/facts/*.jsonl` for 4 properties, and hand-compute `fixtures/underwriting|strategies|scores` for all 12 — with the spreadsheet committed alongside. Reproduce the spec §23 worked example exactly as fixture #1.
4. Finish `finance/`: attachment probability, real ARV, full acquisition/holding/resale cost models, amortized balances, missing-sqft handling. Make the fixtures pass.
5. Finish `strategies/`: cash commission fix, rental vacancy double-count fix, real foreclosure and subject-to logic, offer-grid basis/profit fix, add the linearity test.
6. Finish `scoring/`: expected-scenario equity, correct DCS, decayed distress, config-driven weights, full `components`, plus `rank_scope` writing `rankings`.
7. Rewrite `normalization/` against the fact fixtures — resolver precedence, entity dedupe, derived balances, conflicts, `data_quality`.
8. Wire `identity` into `ingest_document` (and fix the `apn_key IS NULL` match), so `reports.property_id` is finally set.
9. Build `pipeline.recompute_property` for real: load → normalize → underwrite → strategize → score → persist → emit flags, with the per-property advisory lock, the fan-in counter, and job handlers registered.
10. Make `/api/properties/{id}/analysis` return the real payload from those persisted rows — at which point the frontend has something to build against.

After #10 you have a working vertical slice: PDF → facts → normalized record → scores → an API payload. Everything after that is breadth, not risk.
