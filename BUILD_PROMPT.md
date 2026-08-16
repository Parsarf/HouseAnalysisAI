# ACQ — Master Build Prompt

> Paste this whole file as the task brief for the agent/team continuing `Parsarf/HouseAnalysisAI`.
> It is written against the actual state of `main` as of this review, not against the spec's ideal.

---

## 0. Mission

Take the ACQ repository from its current state (a contract scaffold with placeholder engines) to a working, correct, daily-usable property acquisition analysis platform, as specified in the two documents already in the repo:

- `property-acquisition-platform-spec.md` — what the system does and the exact formulas.
- `acq-build-packages.md` — package boundaries, contracts, acceptance criteria, integration gates.

Those two documents are authoritative for *behavior*. This prompt is authoritative for *what is broken, what is missing, and the order to fix it in*. Where this prompt and the spec disagree, this prompt wins for sequencing; the spec wins for formulas.

---

## 1. Honest assessment of the current repo

**What genuinely exists and is worth keeping:**

- `contracts/models.py` — a good, near-complete frozen contract layer with `extra="forbid"` and a `TrackedValue` null-discipline validator.
- `db/schema.sql` — most of the table set, in one place.
- `jobs/postgres.py` — a correct `FOR UPDATE SKIP LOCKED` claim, dedupe on `dedupe_key`, exponential backoff, dead-lettering.
- `common/` — money quantization, error taxonomy, advisory-lock helper, JSON-safe serializer, settings.
- `auth/` — HMAC-signed session + argon2 password. Adequate for a private tool.
- Docker Compose, Alembic wiring, Makefile, `db/OWNERSHIP.md`, `CONTRIBUTING.md`.

**What is placeholder or absent:**

- `finance/`, `strategies/`, `scoring/`, `normalization/` are toy implementations that do not follow the spec's formulas and would produce wrong money on real data.
- `identity/` is an empty package. Nothing ever sets `reports.property_id`, so the pipeline is severed in the middle.
- `extraction/` has a grounding helper and nothing else — no client, prompts, schemas, routing, cost accounting, or persistence.
- `web/` is a single 27-line file.
- `fixtures/` is empty `.gitkeep` directories, so **every acceptance criterion in the build plan is currently unverifiable.**
- No OCR, no ranking, no flags persistence, no analyst, no exports, no changes, no calibration, no problems page, no cost pre-estimate.

**Do not treat the placeholder engines as a starting point to extend.** Delete and rewrite `finance/engine.py`, `strategies/engine.py`, `scoring/engine.py`, and `normalization/resolver.py` from the spec formulas. Extending them will preserve their wrong assumptions.

---

## 2. Non-negotiable rules (repeat of `CONTRIBUTING.md`, enforced in CI)

1. **The LLM extracts and classifies. It never calculates.** No dollar figure reaches the user from a model. Every one is reproducible from stored inputs + `assumption_set_id` + `engine_version`.
2. **Absence is a value.** Missing → `None` with a `null_reason`. Never a sentinel zero, never an invented default.
3. **A person-level lien is never a property lien** unless the source text ties it to the parcel. `attachment_basis` drives the confirmed/potential split everywhere.
4. `Decimal` in Python, `numeric(14,2)` in Postgres, string in JSON. No floats in `finance/`, `strategies/`, `scoring/`.
5. No cross-package imports except `contracts/` and `common/`. (Currently violated — see D6.)
6. Every package owns its tables and Alembic range per `db/OWNERSHIP.md`. No package edits another's tables.
7. CI never calls a live model. Extraction tests replay `fixtures/recorded_responses/`.

---

## 3. PHASE 0 — Fix the foundation (blocking; nothing else starts)

These are defects in code that already exists. Each is stated as: **problem → required fix → test that proves it.**

### D1. The contract layer is forked in two — highest severity

`contracts/__init__.py` does `from .models import *` then `from .extended import *`. `extended.py` redefines `OwnershipBlock`, `ValuationCandidate`, `MortgageRecord`, `ForeclosureState`, `BankruptcyRecord`, `TaxBlock`, `HoaBlock`, `RentalBlock`, `ListingRecord`, `ComparableSale`, `ConditionSignal`, `DataQualityBlock`, `FlagSummary`, `AcquisitionCosts`, `RepairAssumptions`, `HoldingAssumptions`, `ResaleAssumptions`, `StrategyAssumptions`, `AssumptionSet`, `ValueBlock`, `LiabilityBlock`, `EquityBlock`, `UnderwritingResult`, `StrategyResult`, `OfferPoint`, `OfferGrid`, `ScoreSet` — so **the star-import order silently decides which definition every package gets**, and it picks the looser `extended.py` versions (plain `BaseModel`: no `extra="forbid"`, no enum handling, defaults on everything).

They also disagree semantically:
- `ConditionSignal.condition` (models) vs `.level` (extended); `evidence: str` vs `list[str]`.
- `FlagSummary.type` (models) vs `.flag_type` (extended).
- `AssumptionSet` in models requires every cost field; in extended everything has a default — so an incomplete assumption set silently validates.
- `StrategyAssumptions` in extended is missing `hard_money` and `rental` entirely, which the rental and flip strategies need.
- `UnderwritingResult.costs` is `dict[Scenario, CostBlock]` in models and `dict[Scenario, dict[str, Decimal]]` in extended.
- `FullNormalizedProperty` subclasses `NormalizedProperty` (which inherits `ContractModel`'s `extra="forbid"` + `use_enum_values=True`) but overrides fields with plain-`BaseModel` types — a mixed-config model.

**Fix:** collapse to **one** canonical module. Keep `contracts/models.py` as the single source; delete `contracts/extended.py`; move `RecordedResponse` into `models.py`; delete `FullNormalizedProperty` and use `NormalizedProperty` everywhere (update `finance/`, `scoring/`, `normalization/`, `flags/`, `pipeline/`, and tests). Replace the star-imports in `contracts/__init__.py` with an explicit `__all__` export list.

**Test:** `tests/test_contracts.py::test_no_duplicate_contract_names` — walk `contracts/` modules and assert no name is defined twice; assert `contracts.AssumptionSet is contracts.models.AssumptionSet`.

### D2. `use_enum_values=True` breaks the equity path at runtime

`ContractModel` sets `use_enum_values=True`, so after validation `LienRecord.attachment_basis` is a plain `str`. `finance/engine.py` calls `lien.attachment_basis.value` → `AttributeError` for **any property that has a lien**. CI is green only because no test fixture has liens.

**Fix:** remove `use_enum_values=True` from `ContractModel` (keep real enum members; they serialize correctly in `model_dump(mode="json")`), then grep the codebase for `.value` on enum-typed fields and fix accordingly. Alternatively keep the flag and remove every `.value` — but the first option is safer because comparisons against enum members stay unambiguous.

**Test:** a numeric-core test whose property has three liens, one of each `attachment_basis`; assert the confirmed/potential split is correct. This test must exist before any finance work starts.

### D3. The initial migration is broken

`db/migrations/versions/0001_initial.py` reads `Path(__file__).parents[3] / "schema.sql"` — that resolves to `<repo_root>/schema.sql`, but the file is at `db/schema.sql`. `alembic upgrade head` fails on a clean database. The `downgrade()` also runs `DROP SCHEMA public CASCADE`, which is a footgun on a real machine.

**Fix:** the deeper problem is two sources of truth (`db/schema.sql` and `db/models.py`). Choose ORM-first: complete `db/models.py` so it covers **every** table in `db/schema.sql`, delete `schema.sql`, and generate `0001_initial` with `alembic revision --autogenerate`. Make `downgrade()` drop tables explicitly, not the schema.

**Test:** CI job that spins up Postgres, runs `alembic upgrade head`, then `alembic downgrade base`, then `upgrade head` again; plus an autogenerate-diff check asserting no pending model/migration drift.

### D4. `db/models.py` covers 5 of ~30 tables, and diverges from `schema.sql`

Only `jobs`, `batches`, `properties`, `reports`, `extraction_units` have ORM models. `Property` in the ORM is missing `fips_county`, `address_hash`, `lat`, `lng`, `underwriting_status`, `last_recomputed_at` — all present in `schema.sql`. The unused `Timestamped` mixin should be applied or deleted.

**Fix:** write ORM models for every table, with the mixin applied consistently.

### D5. Schema gaps that will cause real bugs

Add in the WP-0 migration:

- **`flags.dedupe_key text UNIQUE NOT NULL`** — `FlagRequest` carries a `dedupe_key` but the table has no such column, so duplicate flags on every recompute are guaranteed today.
- **`UNIQUE(apn_key)` and `UNIQUE(address_hash)` on `properties`** (partial, `WHERE merged_into_id IS NULL`) — the identity race's unique-index backstop does not exist.
- **`pg_trgm` GIN index** on the normalized address for fuzzy matching.
- **`extracted_facts(report_id)`** index; **`scores`**: add `engine_version`, `resolution_version`; **`deal_scenarios`**: `UNIQUE(property_id, strategy, scenario, assumption_set_id, engine_version)`; **`rankings`**: `UNIQUE(scope_type, scope_id, property_id, ranked_at)`.
- **Missing config tables the engines require:** `lender_aliases`, `historical_rate_index` (year × loan type → rate), `regional_cost_index`, `transfer_tax_rates`, `watchlists` (or a boolean already on `properties`), `prompt_versions`.
- **`batches`**: `budget_limit_usd`, `spent_usd`, `awaiting_confirmation` state support.
- **`reports`**: `failure_reason` (enum-backed text), `generated_date` already present, add `section_match_rate`.

### D6. Import boundaries are not actually enforced

`importlinter.ini` sets `root_package = acq`, but there is no `acq` package — the config targets nothing. Meanwhile `ingestion/worker.py` imports `classification`, which is exactly the peer-import the rule forbids. CI runs only `pytest` and `ruff`.

**Fix:** set `root_packages` to the real top-level package list; add a layered contract expressing the true dependency order (`contracts` < `common` < leaf packages < `pipeline` < `api`); move the ingestion→classification call into `pipeline/` (the orchestrator is the only package allowed to compose others); add `lint-imports` and `mypy` to the Makefile **and** to CI.

### D7. API defects

- `api/app.py` `upload()` does `await upload_file.read()` into memory, and `ingestion.store_pdf` does `source.read_bytes()[:5]` — both load entire files into RAM. With the documented 300 MB limit and multi-file batches this OOMs. **Fix:** stream uploads to a temp file in chunks; read only the first 5 bytes for the magic check.
- The global `@app.exception_handler(Exception)` returns `str(exc)` to the client — leaks internals. **Fix:** log the detail, return the error code and a generic message; keep detail only in dev.
- No sha256 pre-check before insert, so a duplicate upload raises `IntegrityError` and kills the job instead of linking `duplicate_of`.
- Session cookie is `secure=False` unconditionally; make it configurable and default to secure when not on localhost.
- `/api/properties` has no cursor pagination, no filter grammar, no sorting.

### D8. Budget is per-process and in-memory

`ops/budget.py` holds `reserved` on an instance. With a worker and an API process, and restarts, this enforces nothing.

**Fix:** move reservation into Postgres — `UPDATE batches SET spent_usd = spent_usd + :cost WHERE id = :id AND spent_usd + :cost <= budget_limit_usd RETURNING spent_usd` — a single atomic statement. Return `Allowed | Paused`, and have `Paused` transition the batch to `paused_budget`.

### D9. `flags/service.py` emits invalid flag types

It constructs `FlagRequest(flag_type="unverified_lien_attachment")` and `"material_conflict"`, neither of which is in the `FlagType` enum (`LIEN_ATTACHMENT`, `CONFLICTING_MORTGAGE`, …). These raise validation errors at runtime. `dedupe_key` is also index-based (`lien-attachment:{index}`), so list reordering creates duplicates.

**Fix:** use enum members; make `dedupe_key` content-derived (e.g. `sha1(flag_type + lien recording_doc_number or creditor+amount+date)`).

### D10. Two different `ExtractionUnit` classes

`classification/sectioning.py` defines a dataclass and `db/models.py` defines an ORM model with the same name. Rename the dataclass to `SectionedUnit` to stop the inevitable confusion.

### D11. CI is too thin to protect parallel work

Add to `.github/workflows/ci.yml`: a Postgres service; `alembic upgrade head`; `pytest -m "not integration"`; `pytest -m integration` (once fixtures land); `ruff`; `mypy` across all packages, not just three; `lint-imports`; `cd web && npm ci && npm run build`; and a **no-float check** over `finance/`, `strategies/`, `scoring/` (AST scan rejecting `float(`, float literals in money expressions, and `/` on money without `Decimal`).

**Phase 0 exit criteria:** one canonical contract module; `alembic upgrade head` works from empty; all tables have ORM models; the schema additions in D5 exist; import boundaries enforced and violations removed; CI runs the full gate list; the three named runtime bugs (D2, D7 memory, D9) have regression tests.

---

## 4. PHASE 0.5 — Author the fixtures now (do not wait for PDFs)

`fixtures/recorded_responses/README.md` says `HUMAN_FIXTURE_INPUT_REQUIRED` and `tests/integration/test_launch_gates.py` skips everything. That is correct for Gates A and B — real vendor PDFs are needed for ingestion, classification, and extraction accuracy.

**It is not correct for everything else, and treating it as such is currently blocking 60% of the project.** The numeric core is a pure function of `NormalizedProperty` + `AssumptionSet`. Those fixtures can be hand-authored today, and they define correctness for finance, strategies, scoring, flags, the API payloads, and the entire frontend.

**Do this first, before writing any engine code:**

1. Hand-author the **12 `fixtures/normalized/*.json`** records listed in `acq-build-packages.md` §6. They must include: clean high-equity · owner-only federal tax lien · conflicting mortgage balances · active NTS with two postponements · no value data · no debt data · negative equity · active bankruptcy · OCR-sourced low confidence · released lien · missing APN · condo with HOA arrears.
2. Author **3 `fixtures/assumptions/*.json`** (default, aggressive, conservative), complete — every field, no reliance on defaults.
3. Compute the expected `underwriting/`, `strategies/`, `scores/` outputs **by hand in a spreadsheet**, commit the spreadsheet as CSV next to them, and commit the JSON. The worked example in `property-acquisition-platform-spec.md` §23 gives you one complete, arithmetically consistent case to start from — reproduce it exactly as fixture #1.
4. Author `fixtures/facts/*.jsonl` for at least 4 of the 12, so normalization is testable without extraction.
5. Build an **MSW mock server** in `web/src/mocks/` seeded from these fixtures, so the frontend can be built to completion with no backend.

Only Gate A (real PDFs → units) and Gate B (units → facts, with recorded responses) remain blocked on the owner supplying documents. Everything else becomes testable immediately.

---

## 5. PHASE 1 — Rewrite the numeric core

Delete and rewrite from the spec. This is the part where wrong code costs money, so it gets hand-verified fixtures and 100-run determinism tests.

### WP-6 `finance/` — rewrite `underwrite()`

Current placeholder violates the spec in at least eight ways: it takes the **median** of raw candidates instead of a source-weighted expectation; computes dispersion as `(high−low)/expected` instead of clamped weighted stdev; sets `ARV = scenario value` (ARV is *after repairs*, and only exists when repairs are specified); emits **no cost blocks at all**; computes `adjusted equity` using full `maximum` instead of `potential × attachment_probability`; ignores delinquent taxes and HOA arrears; never derives a mortgage balance by amortization; has no single-candidate dispersion floor; and never returns `insufficient_data` for anything but a total absence of candidates.

Implement per spec §7:
- §7.2 weighted value candidates with recency decay and comp-quality adjustment; `disp = clamp(weighted_stdev/V_exp, 0.04, 0.30)`; single candidate → forced `disp = 0.15` and `valuation_confidence ≤ 0.5`.
- §7.3 three liability buckets driven by `attachment_basis`; unknown-amount liens at type medians from `AssumptionSet.unknown_lien_medians` with `is_estimated=true`; undrawn HELOC is *potential*; published-bid reconciliation with a `bid_mismatch` flag above 20% divergence.
- §6.5 amortization-derived balances with the `historical_rate_index` fallback and ±150bps scenario widening.
- §7.4 gross / adjusted / net-realizable equity per scenario.
- §7.5 full cost models — acquisition, repairs (from the condition enum only, never a model-supplied dollar figure), holding, resale.
- §7.7 failure cases: no value → `insufficient_data` and **no** equity or cost numbers; no debt records → `confirmed=0` **and** `debt_data_present=false`.

**Acceptance:** all 12 `fixtures/underwriting/` reproduced exactly across 3 assumption sets; determinism ×100; no-float AST check; hand-computed spreadsheet cross-check committed for 3 properties.

### WP-7 `strategies/` — rewrite entirely

Current placeholder has only `flip`, hardcodes repairs as `psf × 1000`, adds `liabilities.maximum` into the buyer's basis (a buyer does not pay both the purchase price and the seller's liens in a normal purchase), and its `offer_grid` computes "proceeds" as `value − offer − confirmed − closing`, which is not seller proceeds under any definition. `proceeds_high == proceeds_expected`, `profit = proceeds_expected`, and the grid is 5 points at ±20% of an arbitrary center rather than 9 points spanning 0.60–1.00 × V with MAO markers.

Implement all six strategies per spec §8 and the offer/proceeds engine per §9:
- `cash`, `flip`, `wholesale`, `rental`, `subject_to` (detection only, templated notices, `requires_human_review`, excluded from scoring), `foreclosure` (forces *high* repairs when interior is unknown; risk flags as booleans; capped at 70 unless DCS ≥ 75).
- Seller proceeds = `offer − confirmed payoffs − property-attached liens − delinquent taxes − seller closing`, emitted as low/expected/high. If potential liabilities > 0, a single-number proceeds result must be **unrepresentable in the type**.
- `is_short_sale` when `proceeds_low < 0`, plus a `SHORT_SALE_CANDIDATE` flag.
- Every uncomputable strategy returns `unavailable` + reason. Missing sqft kills flip/rental only. Missing rent → `no_rent_data`; **never infer rent from value.**

**Acceptance:** `fixtures/strategies/` reproduced exactly; **interpolation-equals-computation test** across every grid segment (this is what licenses the frontend slider to interpolate); determinism ×100.

### WP-8 `scoring/` — rewrite entirely

Current placeholder is unrelated to the spec: `fos = confidence × 100`, `distress = 60 or 0`, no DCS formula, no risk formula, no gates, no config table, and it never looks at strategy results.

Implement spec §10 exactly: FOS (5 weighted terms with configurable bounds), Distress (the full point table with `0.5^(months/18)` decay), DCS (6 terms over the 22 critical fields), Risk (the additive penalty formula), Overall with `0.50/0.20/0.20/−0.25` weights, then the **gates**: `DCS < 40 → cap 45 + needs_review`; `insufficient_data → unranked`; open gating flag → unranked. Every sub-term goes into `components` with a stable name — WP-14's comparison route reads those names, so treat them as a contract. All weights/bounds/points come from a `scoring_configs` row, changeable with no deploy.

Then the ranking job: one `RANK() OVER (PARTITION BY scope ORDER BY overall DESC, property_id)` statement writing `rankings` with `prev_rank` carried forward.

**Acceptance:** `fixtures/scores/` exact; a test per gate proving it fires; excellent financials + DCS 35 cannot exceed 45; 50k synthetic rows rank in <10s with correct `prev_rank`; determinism ×100.

### WP-5 `normalization/` — rewrite `resolve_facts()`

Current version picks `max(extraction_confidence)` per field and returns a record containing only an address and APN. It has no precedence formula, builds no entities, does no dedupe, detects no conflicts, derives no balances, and never computes `data_quality` — which WP-8 depends on and must not recompute.

Implement spec §6: the `0.40·source_rank + 0.30·recency + 0.20·specificity + 0.10·corroboration` resolver with conservative tie-breaking (highest liability, lowest asset value); entity dedupe for mortgages/liens/foreclosure events; derived balances; conflict detection with per-field tolerances emitting flags; full `data_quality` computation; **idempotent** entity upsert via stable natural-key hashes.

**Acceptance:** all 12 `fixtures/normalized/` reproduced from `fixtures/facts/`; running twice gives byte-identical output and no duplicate rows; a `source_kind=human` fact always wins; an owner-only lien never appears in a property-attached position anywhere in the output.

---

## 6. PHASE 2 — Complete the document path

### WP-1 `ingestion/` — finish it

Present: sha256, magic-byte check, ZIP guard, a naive `ingest_document` worker. Missing:

- **`get_page_text(report_id, page)` and `get_all_page_text(report_id)`** — the public accessor every other package must use. Nothing else may touch the filesystem. Add it first; it is a cross-package contract.
- **OCR path.** Currently `is_scanned` is computed and then ignored. Wire OCRmyPDF/Tesseract at 300 DPI writing `documents/{report_id}/ocr.pdf`, 10-minute timeout, first-15-pages fallback with `PARTIAL_OCR`, and cap every fact from an OCR'd report at `confidence ≤ 0.80`.
- **Duplicate handling before insert** — look up sha256, link `duplicate_of`, skip processing. Do not rely on the unique constraint throwing.
- **Failure codes**: encrypted, corrupt, not-a-PDF, OCR timeout — mapped to `reports.status` + `failure_reason`, never an unhandled exception.
- **Memory**: `document.close()` on every PyMuPDF handle; stream uploads (D7); a 400-file batch must show flat RSS.
- **Watched folder** (`./inbox/`) and **paste ingestion** (single-page pseudo-report, `vendor='pasted'`, facts capped at 0.7).
- Remove the `classification` import from `ingestion/worker.py`; the orchestrator composes them (D6).

### WP-2 `classification/` — replace the regex stubs

Current `RULES` are eight generic patterns matched against the whole first page, returning confidence `0.9` for the first hit — a mortgage report mentioning "lien" once misclassifies. `section_pages()` has an off-by-one fallback (`range(0, len, fallback_size-1)`) and estimates tokens as `len(text)//4`.

- Move signatures into the **`document_signatures` table** (pattern, report_type, vendor, priority, is_active), matched against page-1 header text and filename, seeded from the real vendor set once PDFs arrive. Editable at runtime with no deploy — prove it with a test.
- LLM fallback only for unmatched documents, using the cheap model.
- Real header-based sectioning per vendor; correct 3-page/1-page-overlap fallback.
- **Token estimates within ±10% of a real tokenizer** — the cost pre-estimate depends on this; `len//4` is not acceptable.
- Emit `section_match_rate` per document for the Problems page.

### WP-3 `identity/` — build it from nothing

This package is empty, and it is the reason the pipeline is currently severed: nothing sets `reports.property_id`, so no facts can ever be attributed to a property. Implement spec §4.5 in full:

- APN normalization → `apn_key = fips + alphanumerics`, address parsing (`usaddress`) → USPS normalization → `address_key`/`address_hash`, then trigram fuzzy ≥ 0.92 within the same ZIP and same house number.
- **APN matches but ZIP or house number differs → do not merge**, raise `IDENTITY_CONFLICT` (gating).
- `merge()` / `unmerge()` as public functions; soft `merged_into_id`; unmerge re-parents reports and enqueues recompute for both.
- Postgres advisory lock (`common/locks.py` already has the helper) around lookup-or-create, plus the unique indexes from D5, plus retry on unique violation.
- Optional geocoding into `lat`/`lng`, cached, **never blocking resolution**.

**Acceptance:** a 60-case address normalization table; 200 fixture report headers collapse to the exact expected property count; **50 concurrent resolutions of the same address create exactly one property**; merge→unmerge→merge round-trips to identical state.

### WP-4 `extraction/` — build it from nothing

Only `validate_grounding()` exists (and it correctly implements the single most valuable check in the system — keep it). Everything else is missing:

- **Prompts** in git files, hashed into `prompt_version`; the system prompt is given verbatim in spec §18 — use it as written, including rule 4 on lien attachment.
- **One JSON Schema per unit type** (`property_core`, `ownership`, `mortgages`, `liens`, `foreclosure`, `bankruptcy`, `valuation`, `comparables`, `listings`, `tax`, `rental`, `condition_signals`). The lien schema is given in full in spec §18; the rest follow its shape. `condition_signals` returns **enums only, no dollar amounts**.
- **Model routing:** cheap model for comps/listings/tax/rental/classification, frontier for liens/mortgages/foreclosure/bankruptcy. Temperature 0, structured-output/tool mode.
- **Full validation gauntlet in order** (spec §5.3): schema (one repair retry) → grounding (never retried, always dropped) → parse consistency → range sanity → cross-field logic → null-reason discipline.
- **Cost accounting** per call into `extraction_units.cost_usd`, with `budget.check_and_reserve()` (the DB-backed version from D8) called *before* each request.
- **Retry/backoff** on 429/5xx, 5 attempts, then `extraction_failed` surfaced on the Problems page.
- **`reextract(report_ids, prompt_version)`** reading stored page text — no re-upload, no re-OCR. New facts supersede; nothing is deleted.
- **`make eval`** against `fixtures/gold/` printing per-field accuracy and grounding-failure rate. Replace the current stub print.
- Persist `ExtractedFactDraft` → `extracted_facts` with `property_id` inherited from the report.

**Acceptance:** ≥97% on the 22 critical fields, ≥99% on lien `attachment_basis`, <2% grounding failures; a deliberately poisoned mocked response (fabricated snippet + out-of-range value + null without reason) stores **zero** facts; cost accounting within 1% of provider usage.

---

## 7. PHASE 3 — Wire it together

### WP-10 `pipeline/` — the real orchestrator

`recompute_property()` is currently a pure function that takes an already-built `NormalizedProperty` and returns objects. It loads nothing, persists nothing, enqueues nothing. `pipeline/worker.py` handles exactly one job type.

Build the real thing per spec §17:

```
ingest_document(report_id)   → ingestion → classification → identity → enqueue extract_unit per unit
extract_unit(unit_id)        → extraction (budget-checked) → on last unit for a property: recompute
recompute_property(id, reason) → normalize → underwrite → strategies+offers → score
                               → persist all → emit flags → mark rank_dirty
rank_scope(scope) · detect_changes(property_id) · nightly()
```

Requirements that are easy to get wrong and must have dedicated tests:
- **`recompute_property` is the single re-entry point** downstream of the ledger: idempotent, concurrent-safe across properties, serialized per property by advisory lock. Run it 3× → identical DB state.
- **Fan-in:** extraction units finish out of order. Use a per-property outstanding-unit counter with `SELECT … FOR UPDATE`, not "last job wins." Test: 20 units finishing simultaneously trigger exactly one recompute.
- **Batch state machine:** `uploading → ingesting → estimating → awaiting_confirmation → extracting → computing → complete | paused_budget | failed`. `awaiting_confirmation` is where the cost pre-estimate is shown; nothing hits the model API before confirmation.
- **Crash safety:** kill the worker mid-batch, restart, resume with no duplicate work and no lost documents.
- **Bulk recompute** (assumption or scoring-config change) in chunks of 500 with a pollable progress row.

### WP-9 `flags/` — persistence and resolution

Beyond fixing D9: persist to `flags` with the new `dedupe_key` column; compute `financial_impact_usd` by calling `underwrite()` twice (accept vs. reject the disputed value) — that is the queue's sort key; implement the four resolutions (approve / reject / replace / dismiss); `apply_override()` writes a `source_kind='human'` fact at precedence 1.0, writes `history`, enqueues recompute, and returns the before/after score so the UI can show the delta. Expose `is_gating` for WP-8.

### WP-11 `api/` — build out from the 8 stub endpoints

Beyond the D7 fixes, implement the full endpoint list in spec §16. The three that matter most:

- **`GET /properties/{id}/analysis`** currently returns a hardcoded empty shape. It must return, in **one round trip**: normalized record + underwriting + all 18 strategy results + the full offer grid (9 × 3) + scores + flags + timeline. The scenario toggle and offer slider then need zero further requests.
- **The filter grammar** (`FilterClause` already exists in contracts) translated to SQLAlchemy in exactly one place with an allowlist of filterable fields — WP-12, WP-14, and WP-15 all emit it.
- **Money envelope**: every money field serializes as `{value: "123456.78", confidence, source_kind, is_estimated}`. Add an automated response scan in tests asserting no endpoint returns a bare number for money.

Plus: cursor pagination on `(sort_key, id)`; saved views; notes/tags/status/next-action; batch estimate + confirm; flags list and resolve; exports; SPA static serving.

---

## 8. PHASE 4 — The frontend (currently 27 lines)

`web/` needs to be built essentially from scratch against the MSW mocks from Phase 0.5. Note the existing file already has a type error (`property.gut_rating` used but absent from the `Property` type) and the web build is not in CI.

**WP-12 — shell, portfolio, dashboard.** Ship the shared layer in the first three days because WP-13 consumes it: API client, TanStack Query setup, UI primitives, and the **money display component** — which takes the `{value, confidence, source_kind, is_estimated}` object and renders estimated values distinctly. No raw number rendering anywhere in either frontend package; add a lint rule or grep test. Then: virtualized table with server-side sort/filter/pagination, filters, saved views, keyboard triage (`j/k/space/enter/p/x/f/t/1–5/?`), compare view (2–4 properties), Leaflet map, Problems page. **Every aggregate tile must display its exclusion count** — bake it into the tile component so it cannot be forgotten.

**WP-13 — deal page.** Executive summary card, scenario toggle (zero network requests), financial breakdown waterfall, strategy tabs, **offer slider that snaps to grid points and linearly interpolates** (licensed by WP-7's linearity test — do not reimplement any formula in TypeScript), distress timeline, evidence drawer (pdf.js at the cited page, snippet highlighted, competing candidates with sources and scores), inline flag resolution with score-delta toast, notes, history.

**Hard UI rule:** a property with potential liabilities must be unable to render a single seller-proceeds figure. Enforce it in the component's prop types, not by convention.

Also replace `"latest"` in `web/package.json` with pinned versions, add TanStack Query/Table, Tailwind, Recharts, pdf.js, Leaflet, MSW, Playwright — and fix `scripts/generate_types.py`, which currently maps every non-string type to `unknown` and every string-ish type to `string` (so `Decimal`, enums, nested objects, and arrays all come out wrong). Use `json-schema-to-typescript` against the exported schemas instead.

---

## 9. PHASE 5 — The remaining packages

Build in this order; each is small and self-contained.

1. **WP-15 `exports/`** — deal sheet PDF and seller net sheet PDF (WeasyPrint from HTML templates consuming the same `/analysis` payload as the deal page, so there is one source of truth). The deal sheet footer lists which figures are estimated, pulled from `is_estimated`, not hand-maintained. The net sheet states which obligations are unverified and shows a range. Extend `exports/csv.py` (which is a reasonable streaming skeleton) with filter-grammar support and column selection; add the full data export.
2. **WP-14 `analyst/`** — empty package. Three routes per spec §13: text-to-SQL over curated read-only views with a dedicated read-only Postgres role, `sqlglot` SELECT-only parse, forced `LIMIT 500`, 5s timeout; **deterministic** comparison via `ScoreSet.components` diff with the model used only for phrasing (validated to introduce no new numbers); simulation that calls the offer engine and narrates. 30-question benchmark suite plus 15 adversarial prompts, all committed.
3. **WP-16 `changes/`** — `changes/diff.py` is a reasonable start but diffs flat dicts and infers change types from field-name prefixes. Rewrite to diff at the entity level (so "new lien" and "lien amount corrected" are genuinely distinguishable), key the closed change-type enum, wire it into `recompute_property`, persist `change_events`, and build the What Changed page. Watchlist auto-add at `OVERALL ≥ 70`. Daily digest only.
4. **WP-17 `calibration/`** — empty package. Actuals entry, four analyses (repairs by condition, value candidate weights, holding period, gut-rating vs. score correlation), minimum sample of 5 before suggesting, suggestions as **proposals** creating a new `AssumptionSet` version with a before/after preview and one-click rollback.
5. **WP-18 `ops/`** — DB-backed budget (D8), cost pre-estimate and live meter, Problems page query over every failure source, and **backup/restore that is actually verified**: `scripts/backup.sh` and `restore.sh` exist but are untested, `restore.sh` untars into the current directory without a target check, and neither is exercised in CI. Add a CI job doing backup → wipe → restore → integrity check. An untested restore is not a backup.

---

## 10. Integration gates

Run these in `tests/integration/`, replacing the current skip-stubs as each becomes possible.

- **Gate A** (WP-1+2+3): 20 fixture PDFs → committed unit set and exact property count. *Blocked on owner-supplied PDFs.*
- **Gate B** (Gate A + WP-4): fixture units → committed facts, using **recorded** model responses. CI never calls a live model. *Blocked on owner-supplied PDFs and reviewed responses.*
- **Gate C** (WP-5/6/7/8/9): fixture facts → committed `ScoreSet`s and flags. **Not blocked — buildable today from Phase 0.5 fixtures.** This is the gate that proves the money is right; make it the most heavily tested thing in the repo.
- **Gate D** (Gate C + WP-10/11/12/13): Playwright — upload, confirm cost, wait, verify ranking, open deal page, drag slider, resolve a flag, see rank move, export a deal sheet.
- **Gate E** (WP-14/15/16/17/18) against a populated database from Gate D.

---

## 11. Working agreement for parallel agents

- One `wp-*` branch per package; merge to `develop` only when that package's acceptance criteria are green; `main` always deployable.
- **Contracts are frozen after Phase 0.** Changes require a PR that also regenerates the TypeScript types and notifies dependents. Expect 2–3 legitimate changes total across the project.
- Do not write to another package's tables. Do not use another package's Alembic revision range. Read `db/OWNERSHIP.md` first.
- Do not import a peer package's implementation. If you need something from another package, you need it from a contract type or through `pipeline/`.
- Develop against `fixtures/`, not against a running instance of someone else's code.
- If a fixture seems wrong, fix the fixture in a separate PR with the recomputed spreadsheet attached — never quietly change an engine to match a bad expectation.

---

## 12. Definition of done

1. Every package green on the acceptance criteria in `acq-build-packages.md`.
2. Gates A–E green in CI.
3. Extraction gold set: ≥97% on the 22 critical fields, ≥99% on lien attachment basis, <2% grounding failures.
4. Determinism suites green (finance, strategies, scoring — 100 runs each, byte-identical).
5. A 2,000-document batch processes end to end within budget and within ±20% of the pre-flight time estimate.
6. Backup → wipe → restore verified in CI.
7. Cold deploy from an empty machine in under 15 minutes, documented.
8. No float in any money path; no endpoint returning a bare number for money; no rendered value bypassing the shared money component.
9. **The two rules hold, verifiably:** no dollar figure originates in a model (traceable via `source_kind` + `engine_version` on every stored number), and no owner-only lien appears in confirmed liabilities anywhere in the system.

---

## 13. Suggested order for a single agent

Phase 0 (D1→D11, in that order) → Phase 0.5 fixtures → WP-6 → WP-7 → WP-8 → WP-5 → Gate C → WP-3 → WP-1 → WP-2 → WP-4 → Gate B → WP-9 → WP-10 → WP-11 → WP-12 → WP-13 → Gate D → WP-18 → WP-15 → WP-14 → WP-16 → WP-17 → Gate E.

Rationale: the numeric core is buildable and fully verifiable today with hand-authored fixtures, it is where correctness matters most, and it unblocks the API payloads and the entire frontend. The document path depends on owner-supplied PDFs and should not gate everything else.
