# ACQ — Parallel Build Plan
### Work packages for independent development and later integration

**Companion document:** `property-acquisition-platform-spec.md` (the functional spec). This document says *who builds what, against which contracts, and how it all connects*. Section references like "spec §7.3" point to that document.

**Premise:** every package below can be built by a different developer, in a different repo folder, without running anyone else's code. They develop against **frozen type contracts and golden fixture files**, not against each other's services. Integration is then a wiring exercise, not a rewrite.

---

## 1. How to use this document

1. **WP-0 must be completed and frozen before anything else starts.** It is the contract layer. One person, 5–8 days. Nothing parallel happens before it lands.
2. Every other package has a fixed template: purpose · depends on · inputs · outputs · owns · must not touch · implementation notes · fixtures · acceptance criteria · effort.
3. A package is **done** when its acceptance criteria pass against fixtures — not when it "works in the app." Integration gates (§8) verify the seams separately.
4. Assign whole packages to people, never halves. The seams between packages are contracts; the seams *inside* a package are conversations.

---

## 2. Ground rules for parallel work

These exist because they are the specific things that break parallel builds.

**R1 — Contracts are frozen after WP-0.**
All shared types live in one package (`contracts/`). Changing a contract requires a PR to the WP-0 owner, a version bump, and regeneration of the TypeScript types. Additive changes (new optional field) are cheap; renames and type changes require notifying every dependent package owner. Assume 2–3 legitimate contract changes total; if you're getting more, WP-0 was rushed.

**R2 — No cross-package imports except `contracts/` and `common/`.**
`finance/` may not import from `extraction/`. If package A needs something from package B, it needs it from a *contract type*, and B fills that type. Enforced by an import-linter rule in CI.

**R3 — Migration numbers are allocated, not chosen.**
Every package gets a reserved Alembic revision range and a fixed set of tables it owns. Nobody writes a migration touching another package's tables. Merge conflicts in migration chains are the single most common cause of lost days in parallel DB work.

**R4 — Everyone develops against fixtures.**
`fixtures/` is part of WP-0 and is version-controlled. It contains real (anonymized) PDFs, expected extraction outputs, expected normalized records, and expected calculation results. A package that can't be developed against fixtures has a badly drawn boundary — say so early.

**R5 — Determinism where determinism is claimed.**
Anything in the finance/strategy/scoring path must produce byte-identical output for identical input. Every one of those packages ships a determinism test that runs the same input 100× and asserts equality. No wall-clock reads, no `random`, no dict-ordering dependence, `Decimal` only.

**R6 — Money is `Decimal`, always.**
`numeric(14,2)` in Postgres, `Decimal` in Python, string-serialized in JSON (never a float). A float in a money path is a review rejection, not a discussion.

**R7 — Nothing invents values.**
Missing → `None` plus a reason code. This is a testable rule, and every package that produces data has a test for it.

**R8 — Feature branches per package, integration branch per gate.**
`wp-04-extraction` → merges to `develop` only when its acceptance suite is green. `main` is always deployable.

---

## 3. Repository layout

Single repo, package-per-folder. This gives independence without the overhead of eight repos and eight release processes.

```
acq/
├── contracts/            # WP-0. Pydantic models, enums, JSON Schemas, generated TS types.
├── common/               # WP-0. config, logging, errors, Decimal helpers, date utils, db session
├── db/                   # WP-0. SQLAlchemy models + Alembic. Table ownership documented per package.
├── jobs/                 # WP-0 primitive; WP-10 owns the pipeline definitions
├── ingestion/            # WP-1
├── classification/       # WP-2
├── identity/             # WP-3
├── extraction/           # WP-4
├── normalization/        # WP-5
├── finance/              # WP-6   (pure library — no DB, no IO)
├── strategies/           # WP-7   (pure library — no DB, no IO)
├── scoring/              # WP-8   (pure library — no DB, no IO)
├── flags/                # WP-9
├── pipeline/             # WP-10  (orchestration; the only place that calls many packages)
├── api/                  # WP-11  FastAPI
├── analyst/              # WP-14
├── exports/              # WP-15
├── changes/              # WP-16
├── calibration/          # WP-17
├── ops/                  # WP-18  backup, deploy, settings, budget
├── web/                  # WP-12 + WP-13 (Vite/React; split by route folder, see below)
├── fixtures/             # WP-0. Golden inputs and expected outputs. Sacred.
└── tests/integration/    # Integration gates (§8)
```

**Frontend split:** `web/src/features/portfolio/*` (WP-12) and `web/src/features/property/*` (WP-13). Shared `web/src/api/` client and `web/src/components/ui/` are WP-12's responsibility and WP-13 consumes them — the one place two frontend devs must coordinate, so WP-12 ships the shared layer in its first three days.

---

## 4. WP-0 — Foundation and contracts

**Owner:** the most senior person. **Effort:** 5–8 dev-days. **Everything else is blocked on this.**

### 4.1 Deliverables

1. Repo, Docker Compose (`app`, `db`), Makefile (`make dev`, `make test`, `make migrate`, `make eval`, `make backup`), CI running lint + tests + import-linter.
2. **Full database schema** (spec §15) as SQLAlchemy models + the initial Alembic migration. All tables, all columns, all indexes, all enums, created in one go by one person. **Do not let each package create its own tables.**
3. **`contracts/`** — the types in §5 below, as Pydantic v2 models, with JSON Schema export and TypeScript type generation (`datamodel-code-generator` → `json-schema-to-typescript`).
4. **`common/`** — settings (pydantic-settings), structured logging, the error taxonomy, `Money`/`Decimal` helpers with `ROUND_HALF_UP`, date parsing, DB session factory, advisory-lock helper.
5. **`jobs/`** — the queue primitive: `jobs` table, `enqueue(name, payload, dedupe_key)`, worker loop with `SELECT … FOR UPDATE SKIP LOCKED`, retry with backoff, `max_attempts`, dead-letter status. ~150 lines. Not the pipeline itself (WP-10) — just the mechanism.
6. **Auth stub** — session cookie + password hash + `read_only` flag, and a `current_user` dependency. 100 lines. WP-11 uses it; nobody else thinks about auth.
7. **`fixtures/`** — see §6.
8. **Table ownership map** and **Alembic revision ranges**, committed as `db/OWNERSHIP.md`.

### 4.2 Table ownership and migration ranges

| Package | Owns (writes to) | Alembic range |
|---|---|---|
| WP-1 ingestion | `batches`, `reports` | 100–119 |
| WP-2 classification | `extraction_units`, `document_signatures` | 120–139 |
| WP-3 identity | `properties` (identity columns), `property_owners`, `owners` | 140–159 |
| WP-4 extraction | `extracted_facts` | 160–179 |
| WP-5 normalization | `field_resolutions`, `mortgages`, `liens`, `foreclosure_events`, `bankruptcy_events`, `valuations`, `listings`, `comparable_sales` | 180–219 |
| WP-6/7 finance | `assumption_sets`, `deal_scenarios`, `offer_scenarios` | 220–239 |
| WP-8 scoring | `scores`, `scoring_configs`, `rankings` | 240–259 |
| WP-9 flags | `flags` | 260–269 |
| WP-11 api | `saved_views`, `property_notes` | 270–289 |
| WP-16 changes | `change_events` | 290–299 |
| WP-17 calibration | `realized_deals` | 300–309 |
| WP-18 ops | `settings`, `history` | 310–319 |

Everyone may **read** any table. Writes stay inside the owning package.

---

## 5. The contract layer (the seams)

These are the interfaces that make independent development possible. They are reproduced here in near-final form so package owners can start immediately.

### 5.1 Primitives

```python
class SourceKind(StrEnum):
    REPORT = "report"; DERIVED = "derived"; HUMAN = "human"; API = "api"; PASTED = "pasted"

class TrackedValue(BaseModel):          # every meaningful number in the system
    value: Decimal | None
    confidence: float                    # 0..1
    source_kind: SourceKind
    is_estimated: bool
    fact_id: UUID | None                 # provenance; None only for computed values
    as_of: date | None
    null_reason: NullReason | None       # required when value is None

class AttachmentBasis(StrEnum):
    RECORDED_AGAINST_PROPERTY = "recorded_against_property"
    OWNER_NAMED_ONLY = "owner_named_only"
    UNKNOWN = "unknown"

class Scenario(StrEnum):
    CONSERVATIVE = "conservative"; EXPECTED = "expected"; OPTIMISTIC = "optimistic"
```

### 5.2 `ExtractedFactDraft` — WP-4 output, WP-5 input

```python
class ExtractedFactDraft(BaseModel):
    report_id: UUID
    extraction_unit_id: UUID
    entity_type: EntityType          # property|mortgage|lien|foreclosure|bankruptcy|valuation|listing|comp|tax|rental|condition
    entity_local_id: str             # groups facts belonging to one extracted object
    field_path: str                  # e.g. "liens[0].amount"
    value_raw: str | None
    value_parsed: Decimal | None
    value_text: str | None
    value_date: date | None
    value_bool: bool | None
    unit: str | None
    as_of_date: date | None
    page_number: int
    snippet: str                     # verbatim, <=200 chars, grounding-verified
    extraction_confidence: float
    null_reason: NullReason | None
    source_kind: SourceKind
```

### 5.3 `NormalizedProperty` — WP-5 output; consumed by WP-6/7/8/11/13/14

**This is the most important contract in the system.** Everything downstream of normalization depends only on this.

```python
class NormalizedProperty(BaseModel):
    property_id: UUID
    apn: str | None
    address: AddressBlock                    # line1, unit, city, state, zip5, county, fips, lat, lng
    attributes: PropertyAttributes           # type, beds, baths, sqft, lot_sqft, year_built, units — each a TrackedValue
    ownership: OwnershipBlock                # owner names, entity_type, is_owner_occupied, is_absentee,
                                             # ownership_start_date, purchase_price, years_owned
    valuation_candidates: list[ValuationCandidate]   # type, value, low, high, as_of, reported_confidence, weight_hint
    mortgages: list[MortgageRecord]          # position, original_amount, rate, term, origination_date,
                                             # estimated_balance: TrackedValue, balance_method, is_open
    liens: list[LienRecord]                  # type, amount: TrackedValue, amount_is_estimated, status,
                                             # attachment_basis, attachment_confidence, recording_date, priority
    foreclosure: ForeclosureState | None     # stage, nod_date, nts_date, original_sale_date, current_sale_date,
                                             # published_bid, default_amount, postponement_count,
                                             # rescission_count, trustee, is_active
    bankruptcies: list[BankruptcyRecord]     # chapter, status, filing_date, discharge_date, sequence
    taxes: TaxBlock                          # annual_taxes, assessed_value, delinquent_amount, delinquent_years
    hoa: HoaBlock                            # monthly_dues, arrears, has_lien
    rental: RentalBlock                      # rent_estimate: TrackedValue, source
    listings: list[ListingRecord]            # list_date, delist_date, price, status, dom
    comparables: list[ComparableSale]        # address, sale_date, price, sqft, distance, similarity, included
    condition: ConditionSignal | None        # enum ladder only: pristine|cosmetic|moderate|heavy|gut + evidence
    data_quality: DataQualityBlock           # critical_field_coverage, source_counts_by_field, conflict_count,
                                             # material_conflict_count, verified_field_count, ocr_applied,
                                             # newest_report_date, mean_extraction_confidence
    open_flags: list[FlagSummary]            # type, severity, is_gating, financial_impact
    resolution_version: str                  # resolver version that produced this
```

### 5.4 `AssumptionSet` — WP-6 input (spec §7.5)

Flat, versioned, fully specified. No defaults resolved at read time; the set is complete or it's invalid.

```python
class AssumptionSet(BaseModel):
    id: UUID; version: int; name: str
    acquisition: AcquisitionCosts    # closing_pct, title_pct, escrow_flat, transfer_tax_lookup_key,
                                     # financing_points, financing_flat, inspection_flat, legal_flat, acq_fee_pct
    repairs: RepairAssumptions       # psf_by_condition {cosmetic:18, moderate:42, heavy:78, gut:135},
                                     # low_multiplier .75, high_multiplier 1.4, regional_index
    holding: HoldingAssumptions      # insurance_pct_yr, utilities_monthly, maintenance_pct_yr,
                                     # acquisition_months, repair_months_by_condition, market_days_default
    resale: ResaleAssumptions        # commission_pct, seller_closing_pct, concessions_pct, staging_flat, misc_pct
    strategy: StrategyAssumptions    # cash_target_margin, flip_target_margin_by_arv_band, wholesale_investor_pct,
                                     # min_assignment_spread, hard_money{rate, points, ltc},
                                     # rental{vacancy, mgmt_pct, maintenance_pct, reserves_pct, ltv, rate, term}
    attachment_probability: dict[AttachmentBasis, Decimal]   # owner_named_only: .35, unknown: .50
    unknown_lien_medians: dict[LienType, Decimal]
    valuation_weights: dict[ValuationType, Decimal]
```

### 5.5 `UnderwritingResult` — WP-6 output; WP-7/8 input

```python
class UnderwritingResult(BaseModel):
    property_id: UUID
    assumption_set_id: UUID
    engine_version: str
    status: Literal["ok", "insufficient_data"]
    unavailable_reason: str | None
    value: ValueBlock            # v_low, v_expected, v_high, dispersion, arv_by_scenario,
                                 # candidates_used[{type, value, weight, as_of}], valuation_confidence
    liabilities: LiabilityBlock  # confirmed, potential, maximum, breakdown[{label, amount, basis, is_estimated}]
    equity: dict[Scenario, EquityBlock]   # gross, adjusted, net_realizable, equity_pct
    costs: dict[Scenario, CostBlock]      # acquisition, repairs, holding, resale, financing (each itemized)
    debt_data_present: bool
    confidence: float
```

### 5.6 `StrategyResult` and `OfferGrid` — WP-7 output; WP-8/11/13 input

```python
class StrategyResult(BaseModel):
    strategy: StrategyType       # cash|flip|wholesale|rental|subject_to|foreclosure
    scenario: Scenario
    status: Literal["viable", "not_viable", "unavailable", "requires_human_review"]
    unavailable_reason: str | None
    mao: Decimal | None
    all_in_basis: Decimal | None
    profit: Decimal | None
    roi: Decimal | None
    margin_of_safety: Decimal | None
    metrics: dict[str, Decimal | None]    # cap_rate, cash_flow, coc, dscr, spread, buyer_margin…
    inputs_echo: dict[str, Decimal]       # every input used, for the UI breakdown
    notices: list[str]                    # templated strings only — never model-generated

class OfferPoint(BaseModel):
    offer_price: Decimal
    scenario: Scenario
    confirmed_payoffs: Decimal
    potential_payoffs: Decimal
    closing_costs: Decimal
    proceeds_low: Decimal; proceeds_expected: Decimal; proceeds_high: Decimal
    buyer_basis: Decimal; profit: Decimal; roi: Decimal | None
    is_short_sale: bool
    label: str | None            # "MAO cash", "MAO flip"

class OfferGrid(BaseModel):
    property_id: UUID
    points: list[OfferPoint]     # 9 offers × 3 scenarios, ascending
    interpolatable: bool = True  # linear in offer price — the UI may interpolate between points
```

### 5.7 `ScoreSet` — WP-8 output

```python
class ScoreSet(BaseModel):
    property_id: UUID
    scoring_config_id: UUID
    fos: Decimal; distress: Decimal; data_confidence: Decimal; risk: Decimal; overall: Decimal
    components: dict[str, Decimal]       # every sub-term, named — this is what powers "why is A above B"
    gates_applied: list[str]
    is_rankable: bool
    recommended_strategy: StrategyType | None
    recommended_alternatives: list[StrategyType]
```

### 5.8 `FlagRequest` — anyone raises, WP-9 owns

```python
class FlagRequest(BaseModel):
    property_id: UUID
    flag_type: FlagType
    payload: dict            # candidates, fact_ids, page refs, competing values
    financial_impact_usd: Decimal | None
    raised_by: str           # package name
    dedupe_key: str          # stable — re-running a package must not duplicate flags
```

---

## 6. Fixtures (WP-0 deliverable, everyone's development input)

```
fixtures/
├── pdfs/                     20 real anonymized reports: 12 digital, 4 scanned, 2 combined 30-page, 2 malformed
├── page_text/                extracted per-page text for each, so packages 2/4 need no PDF tooling
├── units/                    expected extraction_units per report (WP-2 output → WP-4 input)
├── facts/                    expected ExtractedFactDraft lists (WP-4 output → WP-5 input)
├── normalized/              12 NormalizedProperty JSON files, hand-verified, incl. edge cases (WP-5 output)
├── assumptions/              3 AssumptionSet files: default, aggressive, conservative
├── underwriting/             expected UnderwritingResult per (normalized × assumption set)
├── strategies/               expected StrategyResult + OfferGrid
├── scores/                   expected ScoreSet
└── gold/                     40-document extraction gold set with human-labeled field values
```

**The 12 `normalized/` fixtures must include, explicitly:** a clean high-equity property · a property with an owner-only federal tax lien · one with conflicting mortgage balances · one in active NTS with two postponements · one with no value data at all · one with no debt data · one with negative equity · one with an active bankruptcy · one OCR-sourced with low confidence · one with a released lien · one with a missing APN · one condo with HOA arrears.

Hand-verifying these is a day of work and it is the highest-leverage day in the project: every downstream package's correctness is defined by them.

---

## 7. Work packages

**Dependency waves** (after WP-0, which blocks everything):

```
WAVE A (start day 1, fully parallel — no dependencies beyond contracts+fixtures)
  WP-1 Ingestion      WP-2 Classification    WP-3 Identity
  WP-4 Extraction     WP-6 Finance           WP-7 Strategies*
  WP-8 Scoring*       WP-12 Frontend shell   WP-18 Ops
        (*WP-7 needs WP-6's types only — which are in contracts — not its code)

WAVE B (needs Wave A contracts satisfied by fixtures, still parallel)
  WP-5 Normalization  WP-9 Flags   WP-11 API   WP-13 Deal page   WP-15 Exports

WAVE C (needs real data flowing)
  WP-10 Pipeline/orchestration    WP-14 AI analyst
  WP-16 Change detection          WP-17 Calibration
```

Every package below follows the same template.

---

### WP-1 — Ingestion and document storage

**Purpose.** Get PDFs onto disk, deduplicated, with per-page text available for everything downstream. Spec §4.1–4.3, §4.7, §4.8.
**Skill profile.** Backend Python, comfortable with file IO and subprocess management.
**Depends on.** WP-0 only.
**Owns.** `ingestion/`, tables `batches`, `reports`, the `documents/` directory layout.
**Must not touch.** Anything about what a document *means* — classification is WP-2.

**Inputs.** Uploaded files (multipart), a watched folder, a ZIP, or pasted text.
**Outputs.**
- `reports` rows with `status ∈ {uploaded, text_extracted, ocr_pending, ready, failed}`.
- `documents/{report_id}/original.pdf`, `/ocr.pdf`, `/pages/{n}.txt`.
- A Python API: `get_page_text(report_id, page) -> str` and `get_all_page_text(report_id) -> dict[int, str]`. **Every other package reads page text through this function**, never by touching the filesystem directly.

**Implementation notes.**
- `sha256` unique constraint; on collision link `duplicate_of` and skip processing.
- Scanned detection: median chars/page < 100 or >40% empty pages → OCR path.
- OCRmyPDF with a 10-minute timeout; on timeout OCR the first 15 pages, set `PARTIAL_OCR`.
- Magic-byte validation, not extension. Limits: 300 MB/file, 3,000 files/batch.
- Paste ingestion produces a single-page pseudo-report with `vendor='pasted'`.
- Every failure writes a `reports.failure_reason` code from a closed enum — the Problems page (WP-18) renders those codes.

**Fixtures to develop against.** `fixtures/pdfs/` — must produce `fixtures/page_text/` byte-identically for the digital ones.

**Acceptance criteria.**
1. All 20 fixture PDFs ingest; the 12 digital ones reproduce the committed page text exactly.
2. The 2 malformed PDFs fail with the correct enum code and do not crash the worker.
3. Re-uploading the same file creates zero new documents and links `duplicate_of`.
4. A 400-file batch completes with no unbounded memory growth (measure RSS; PyMuPDF documents must be explicitly closed).
5. Scanned-vs-digital classifier is correct on all 20 fixtures.
6. `get_page_text` returns identical output for OCR'd and digital reports (same interface).

**Effort.** 6–8 dev-days.

---

### WP-2 — Classification and sectioning

**Purpose.** Decide what each document is and split it into narrow, cheap extraction units. Spec §4.4, §5.1.
**Depends on.** WP-0; reads page text via WP-1's function (mock it with fixtures during development).
**Owns.** `classification/`, tables `extraction_units`, `document_signatures`.

**Inputs.** `report_id` + page text.
**Outputs.** `extraction_units` rows: `{report_id, unit_type, page_start, page_end, text_path, token_estimate}` and `reports.report_type`, `vendor`, `classification_confidence`.

**Implementation notes.**
- Two tiers: regex signatures from `document_signatures` (editable at runtime, seeded from the fixture vendors), then a cheap-model fallback for unmatched documents only.
- Sectioning by header regex per vendor; fall back to 3-page windows with 1-page overlap.
- `token_estimate` must be within ±10% of actual — WP-18's cost pre-estimate depends on it. Use `tiktoken`-equivalent counting, not `len(text)/4`.
- Emit a `section_match_rate` metric per document for the Problems page.
- Unit types map 1:1 to the extraction schemas in spec §18.

**Acceptance criteria.**
1. All 20 fixtures classify to the correct `report_type`; ≥85% via regex tier alone.
2. The two 30-page combined reports produce the expected unit boundaries (`fixtures/units/`) with page ranges matching exactly.
3. A document matching no signature still produces valid page-window units — degradation, never failure.
4. Token estimates within ±10% of a real tokenizer count on all fixtures.
5. Adding a `document_signatures` row changes classification with no code deploy (test proves it).

**Effort.** 5–7 dev-days.

---

### WP-3 — Property identity resolution

**Purpose.** Decide which documents describe the same parcel. Spec §4.5.
**Depends on.** WP-0.
**Owns.** `identity/`, `properties` (identity columns), `owners`, `property_owners`.

**Inputs.** `{report_id, address_raw, apn_raw, county, owner_names}` — a small struct, deliberately not the whole document.
**Outputs.** `property_id` assigned to the report; possibly a new `properties` row; possibly a `FlagRequest(identity_conflict | possible_duplicate)`.

**Implementation notes.**
- Normalization order: APN (`fips + alphanumerics only`) → address hash (`usaddress` + USPS abbreviations → `number|street|suffix|unit|zip5`) → trigram fuzzy ≥0.92 within the same ZIP and same house number.
- **APN matches but ZIP or house number differs → do not merge**, raise `identity_conflict`.
- Concurrency: Postgres advisory lock on `hash(apn_key or address_hash)` around the lookup-or-create, plus a unique index as a backstop; on unique violation, retry the lookup. **Write a test that runs 50 concurrent resolutions of the same address and asserts exactly one property is created.**
- `merge(a, b)` and `unmerge(a)` are public functions; merge is a soft pointer (`merged_into_id`) and re-parents reports; unmerge restores and enqueues recompute for both.
- Geocoding (for the map) is a separate optional call with a cached table — never block resolution on it.

**Acceptance criteria.**
1. A 60-case address-normalization table (unit variants, directionals, `ST/STREET`, PO boxes, missing ZIP+4) passes.
2. 200 fixture report headers collapse to the expected property count, exactly.
3. Concurrency test: 50 parallel resolutions → 1 property.
4. Merge → unmerge → merge returns the database to an identical state (round-trip test).
5. APN/address conflict raises the flag and does **not** merge.

**Effort.** 6–8 dev-days. *Deceptively hard; give it to someone careful.*

---

### WP-4 — LLM extraction service

**Purpose.** Turn unit text into validated, grounded `ExtractedFactDraft`s. Spec §5.2–5.5, §18.
**Depends on.** WP-0; consumes `fixtures/units/`.
**Owns.** `extraction/`, table `extracted_facts`, `prompts/` directory.

**Inputs.** `ExtractionUnit` (id, type, text, page range, subject address/APN for the prompt header).
**Outputs.** `list[ExtractedFactDraft]`, plus per-unit `cost_usd`, `model`, `prompt_version`, `status`.

**Implementation notes.**
- One JSON Schema per unit type (spec §18). Structured-output/tool mode, temperature 0.
- **Validation gauntlet, in order** (spec §5.3): schema → **snippet grounding against stored page text** → parse consistency → range sanity → cross-field logic → null-reason discipline. One repair retry on schema failure only; grounding failures are never retried, they're dropped and counted.
- Model routing table by unit type: cheap model for comps/listings/tax/rental/classification, frontier for liens/mortgages/foreclosure/bankruptcy.
- Lien attachment classification is the highest-value output in this package — spec §5.4. Write dedicated tests: a lien naming only a person must never come out as `recorded_against_property`.
- Cost accounting per call, written to `extraction_units.cost_usd`; check the budget (WP-18 interface `budget.check_and_reserve(estimated_cost)`) before each call.
- **Re-extraction:** `reextract(report_ids, prompt_version)` reads stored page text, writes new facts, marks old ones `superseded_by`. Never deletes.
- Prompts live in git files, hashed into `prompt_version`. `make eval` scores the 40-doc gold set and prints per-field accuracy + grounding-failure rate.

**Acceptance criteria.**
1. Gold set: ≥97% exact match on the 22 critical fields; ≥99% on lien `attachment_basis`.
2. Grounding-failure rate <2% on the gold set; **100% of grounding failures result in a dropped fact** (test with a deliberately poisoned mock response).
3. A mocked model response containing a fabricated snippet, an out-of-range value, and a null without a reason produces zero stored facts and the right error counters.
4. Determinism: same unit + same prompt version + mocked model → identical facts.
5. Cost accounting within 1% of the provider's reported usage on a 50-unit run.
6. Re-extraction supersedes rather than deletes; old facts remain queryable.

**Effort.** 10–14 dev-days. *The largest single package. Consider splitting prompts/schemas from the validation gauntlet if you have two people.*

---

### WP-5 — Normalization and resolution

**Purpose.** Turn the fact ledger into one `NormalizedProperty`. Spec §6.
**Depends on.** WP-0; consumes `fixtures/facts/`; must produce `fixtures/normalized/`.
**Owns.** `normalization/`, `field_resolutions`, `mortgages`, `liens`, `foreclosure_events`, `bankruptcy_events`, `valuations`, `listings`, `comparable_sales`.

**Inputs.** All active facts for a `property_id`.
**Outputs.** Entity rows, `field_resolutions` rows, a `NormalizedProperty`, and `FlagRequest`s for conflicts.

**Implementation notes.**
- Resolver score: `0.40·source_rank + 0.30·recency + 0.20·specificity + 0.10·corroboration` (spec §6.2). Ties break conservative: highest liability, lowest asset value.
- Entity dedupe rules per spec §6.4 — mortgages by doc number or (lender alias, date ±30d, amount ±1%); liens by doc number or (type, creditor, amount ±$1, date ±7d).
- Derived mortgage balance by amortization (spec §6.5) with the `historical_rate_index` fallback and widened bands; tagged `source_kind=derived`, `derivation='amortization_v1'`.
- Conflict detection with per-field tolerances (spec §6.3) → flags.
- Published trustee bid vs. amortized balance reconciliation → `bid_mismatch` flag above 20% divergence.
- **Idempotent and re-runnable:** running normalization twice on the same facts must produce byte-identical output and must not duplicate entity rows. Entity rows carry a stable natural key hash for upsert.
- Compute `data_quality` (coverage over the 22 critical fields, source counts, conflicts, verification counts) — WP-8 depends on it and must not recompute it.

**Acceptance criteria.**
1. All 12 `fixtures/normalized/` outputs reproduced exactly from `fixtures/facts/`.
2. Idempotency: two runs → identical DB state and identical output hash.
3. A human override fact (`source_kind=human`) always wins regardless of recency.
4. An owner-only lien never appears in a property-attached position anywhere in the output.
5. Conflicting mortgage balances 6% apart produce exactly one flag with a correct `financial_impact_usd`.
6. Released liens are present in the record but excluded from every "active" total.

**Effort.** 10–12 dev-days.

---

### WP-6 — Financial engine (pure library)

**Purpose.** `underwrite(NormalizedProperty, AssumptionSet) -> UnderwritingResult`. Spec §7.
**Depends on.** WP-0 contracts only. **No database, no IO, no network.** This package is a pure function library and must remain so.
**Owns.** `finance/`, `assumption_sets` table (schema only; the API owns editing).

**Implementation notes.**
- Value candidates → weighted expectation + dispersion → `V_low/V_exp/V_high` (spec §7.2). Single candidate → forced dispersion 0.15 and confidence ≤0.5.
- Three-bucket liabilities (spec §7.3): confirmed / potential / maximum. Attachment basis drives the split — this is the rule everything else inherits.
- Equity: gross, adjusted, net realizable, per scenario (spec §7.4).
- Cost models per spec §7.5. Repairs come from the condition enum + sqft + regional index; **the engine never accepts a repair dollar figure from an LLM**, only from the assumption table or a human override field.
- Failure cases per spec §7.7 return `insufficient_data` with a reason — never a default value.

**Acceptance criteria.**
1. All `fixtures/underwriting/` expectations reproduced exactly (12 properties × 3 assumption sets).
2. Determinism test: 100 identical runs, identical output.
3. Zero floats in the money path (AST check in CI, plus a runtime assertion in tests).
4. Property with no value data → `insufficient_data`, and **no** equity/cost numbers emitted at all.
5. Property with no debt data → `confirmed=0` **and** `debt_data_present=false` in the output.
6. Hand-computed spreadsheet cross-check for 3 properties, committed as a CSV alongside the test.

**Effort.** 8–10 dev-days. *Highest ratio of correctness-criticality to code volume in the project.*

---

### WP-7 — Strategy and offer engine (pure library)

**Purpose.** `evaluate(NormalizedProperty, UnderwritingResult, AssumptionSet) -> list[StrategyResult]` and `build_offer_grid(...) -> OfferGrid`. Spec §8, §9.
**Depends on.** WP-0 contracts; `UnderwritingResult` fixtures. **No DB, no IO.**
**Owns.** `strategies/`, tables `deal_scenarios`, `offer_scenarios` (schema; WP-10 persists).

**Implementation notes.**
- Six strategies × three scenarios. Any uncomputable strategy returns `unavailable` + reason, never a number.
- Wholesale viability gate: spread ≥ $15k **and** data confidence ≥60 — the confidence value is passed in, not computed here.
- Subject-to is **detection only**: fires on the four conditions, returns `requires_human_review`, notices are templated constants, and it never contributes to scoring.
- Foreclosure strategy forces *high* repairs in all scenarios when interior condition is unknown, and emits the risk flags as booleans with sources.
- Offer grid: 9 points from 0.60·V to 1.00·V rounded to $5k, plus MAO markers, ×3 scenarios. **Verify and assert linearity in offer price** so the UI can interpolate safely — a unit test must confirm that interpolated midpoints match computed midpoints to the cent.
- Proceeds always emit low/expected/high; if potential liabilities > 0, the "single number" path must be unrepresentable in the type (no optional collapse).

**Acceptance criteria.**
1. `fixtures/strategies/` reproduced exactly.
2. Interpolation-equals-computation test across all grid segments.
3. Missing sqft → flip and rental `unavailable`, cash and foreclosure still computed.
4. Missing rent → rental `unavailable: no_rent_data`; rent is never inferred from value (test asserts this explicitly).
5. Negative proceeds → `is_short_sale=true` and the short-sale flag request emitted.
6. Determinism test.

**Effort.** 8–10 dev-days.

---

### WP-8 — Scoring and ranking

**Purpose.** `score(NormalizedProperty, UnderwritingResult, list[StrategyResult], ScoringConfig) -> ScoreSet`, plus the ranking job. Spec §10, §11.1.
**Depends on.** WP-0 contracts; fixtures for all three inputs.
**Owns.** `scoring/`, tables `scores`, `scoring_configs`, `rankings`.

**Implementation notes.**
- Four component scores exactly as specified (§10). Every sub-term must be emitted into `components` with a stable name — WP-14's "why is A above B" reads these names, so treat them as a contract.
- Gates after the formula: `DCS<40 → cap 45 + needs_review`; `insufficient_data → unranked`; gating flag open → unranked.
- Recommended strategy = highest-scoring viable strategy; near-ties within 5 points listed as alternatives. Deterministic tie-break by a fixed strategy priority order.
- Ranking is a single SQL statement writing `rankings` with `prev_rank` carried from the previous snapshot for the same scope.
- Scoring config is data, not code. Changing weights must require no deploy.

**Acceptance criteria.**
1. `fixtures/scores/` reproduced exactly.
2. Every gate has a test that proves it fires and caps/blocks correctly.
3. A property with excellent financials and DCS 35 cannot exceed 45 overall.
4. Ranking 50,000 synthetic rows completes in <10 seconds and `prev_rank` is correct across two runs.
5. Changing a weight in `scoring_configs` changes scores on recompute with no code change.
6. Determinism test.

**Effort.** 6–8 dev-days.

---

### WP-9 — Flags and verification

**Purpose.** Own the flag lifecycle and the human-override path. Spec §12.
**Depends on.** WP-0. Receives `FlagRequest` from WP-3/4/5/7.
**Owns.** `flags/`, table `flags`, and the "write a human fact" function.

**Inputs.** `FlagRequest` (any package) and resolution actions from the API.
**Outputs.** `flags` rows; on resolution, a `source_kind='human'` fact via a public function `apply_override(property_id, field_path, value, note, user_id) -> fact_id`, then a recompute enqueue.

**Implementation notes.**
- `dedupe_key` prevents duplicate flags across re-runs — mandatory, and tested by re-running normalization twice.
- `financial_impact_usd` = equity delta between accepting and rejecting the disputed value. Calculated by calling WP-6 twice with the two candidate records. This is the queue's sort key.
- Four resolutions: approve (marks `human_verified`), reject (deactivates the fact), replace (creates a human fact at precedence 1.0), dismiss.
- Gating flags (`identity_conflict`) block ranking until resolved — expose `is_gating` so WP-8 can read it.
- Resolution writes a `history` row and enqueues `recompute_property`, and returns the before/after score so the UI can show the delta.

**Acceptance criteria.**
1. Re-running the fact producer twice yields one flag, not two.
2. Each of the ten flag types has a trigger test and a resolution test.
3. `apply_override` produces a fact that wins resolution against every other source (verified against WP-5's resolver via fixtures).
4. Resolving a gating flag makes the property rankable in the next recompute.
5. `financial_impact_usd` is correct for a synthetic $128k owner-only lien case.

**Effort.** 5–7 dev-days.

---

### WP-10 — Pipeline orchestration

**Purpose.** The only package allowed to call many other packages. Turns "a batch was uploaded" into "properties are ranked." Spec §17.
**Depends on.** Contracts of WP-1 through WP-9. **Develops against stub implementations** of each (WP-0 ships `stubs/` returning fixture data for every package interface), so this can start in Wave A/B and be swapped to real implementations at each integration gate.
**Owns.** `pipeline/`, all job definitions, batch lifecycle state machine.

**Job definitions it owns:**
```
ingest_document(report_id)        → WP-1 → WP-2 → identity → enqueue extract_unit per unit
extract_unit(unit_id)             → WP-4 (budget-checked) → on last unit for a property: recompute
recompute_property(property_id, reason)
      → WP-5 normalize → WP-6 underwrite → WP-7 strategies+offers → WP-8 score
      → persist all → emit flags → mark rank_dirty
rank_scope(scope)                 → WP-8 ranking
detect_changes(property_id)       → WP-16
nightly()                         → rank all, refresh aggregates, backup (WP-18), prune temp
```

**Implementation notes.**
- **`recompute_property` is the single re-entry point** for everything downstream of the ledger and must be idempotent, safe to run concurrently for different properties, and serialized per property via advisory lock.
- Batch state machine: `uploading → ingesting → estimating → awaiting_confirmation → extracting → computing → complete | paused_budget | failed`. Pausing on budget is a first-class state, not an error.
- Fan-in problem: extraction units complete out of order. Use a per-property counter of outstanding units (`SELECT count(*) … FOR UPDATE`) rather than "the last job wins" — write a concurrency test for this specifically.
- Bulk recompute (assumption or scoring config change) enqueues in chunks of 500 with a progress row the API can poll.
- Retry policy per job type; permanent failures write `failure_reason` and surface on the Problems page. Nothing fails silently.

**Acceptance criteria.**
1. End-to-end with stubs: 100 synthetic documents → ranked properties, no orphaned jobs.
2. Concurrency: 20 units for one property finishing simultaneously trigger exactly one recompute.
3. `recompute_property` run 3× produces identical DB state.
4. Killing the worker mid-batch and restarting resumes with no duplicate work and no lost documents.
5. Budget exhaustion pauses the batch and resumes cleanly after the budget is raised.
6. Bulk recompute of 5,000 properties reports accurate progress and completes.

**Effort.** 8–10 dev-days. **Assign to your strongest systems person** — this is where correctness at scale is won or lost.

---

### WP-11 — HTTP API

**Purpose.** Every endpoint in spec §16, serialization, filtering, auth, pagination.
**Depends on.** WP-0 contracts; reads from tables owned by others; calls WP-9 and WP-10 public functions. Develops against fixtures loaded into a test DB.
**Owns.** `api/`, tables `saved_views`, `property_notes`.

**Implementation notes.**
- **The filter grammar is a contract** — WP-12 (portfolio), WP-14 (analyst), and WP-15 (exports) all emit it. Define it once, in `contracts/`, as a typed model: `{field, op, value}[]` with a closed operator set (`eq, neq, gt, gte, lt, lte, in, between, contains, is_null`). Translate to SQLAlchemy in one place with an allowlist of filterable fields.
- Money serializes as `{value: "123456.78", confidence, source_kind, is_estimated}` — strings, never floats. **The frontend must be unable to render an estimate as a fact.**
- `GET /properties/{id}/analysis` returns the full payload the deal page needs in one call: normalized record + underwriting + all strategy results + full offer grid + scores + flags + timeline. One round trip; the scenario toggle and offer slider then work with zero further requests.
- Pagination is cursor-based on `(sort_key, id)`. Offset pagination on a 100k table with sorting is a performance trap.
- All list endpoints accept the same filter grammar; saved views are just stored filter documents.
- Errors: one envelope `{error: {code, message, details}}` with a closed code enum shared with the frontend.

**Acceptance criteria.**
1. Contract tests: every endpoint's response validates against the shared JSON Schema (run against the real schema, not a hand-written copy).
2. Filter grammar test suite: 40 filter combinations produce correct SQL and correct results against a seeded 5,000-property DB.
3. `/analysis` p95 <300ms on a 50k-property DB with warm cache.
4. List endpoint p95 <500ms with 10 filters on 50k properties.
5. Read-only user cannot mutate anything (test every mutating endpoint).
6. No endpoint returns a float for money (automated response scan in tests).

**Effort.** 8–10 dev-days.

---

### WP-12 — Frontend: shell, portfolio, dashboard

**Purpose.** Spec §11.1–11.6. The app shell, dashboard, portfolio table, filters, saved views, keyboard triage, compare view, map, Problems page.
**Depends on.** WP-0 contracts (generated TS types) + a **mock API server** (MSW) seeded from fixtures — ships in WP-0 so the frontend never waits for the backend.
**Owns.** `web/` shell, `web/src/api/` client, `web/src/components/ui/`, `web/src/features/portfolio/`.

**Implementation notes.**
- Ship the shared layer in the first three days (API client, query setup, UI primitives, money/confidence display components), because WP-13 consumes it.
- **The money display component is a shared contract:** it takes the `{value, confidence, source_kind, is_estimated}` object and renders estimated values distinctly (italic + dotted underline + tooltip). No raw number rendering anywhere in either frontend package — enforced by an ESLint rule if you can manage it.
- Table: TanStack Table + virtualization, server-side sort/filter/pagination, sticky address + score columns, column selector persisted per user.
- Keyboard triage: `j/k`, `space`, `enter`, `p`, `x`, `f`, `t`, `1–5`, `?`. Optimistic updates with rollback on error.
- Compare view: 2–4 properties, one column each, best-in-row highlighted.
- Map: Leaflet + OSM, markers colored by score and sized by equity; only renders properties with lat/lng, and says how many were excluded.
- **Every aggregate tile must display its exclusion count.** Bake this into the tile component so it can't be forgotten.

**Acceptance criteria.**
1. All views render correctly against MSW fixtures with zero backend running.
2. 5,000-row table scrolls at 60fps (measured), with sort/filter round trips under 500ms perceived.
3. Keyboard triage: a scripted test triages 50 properties without touching the mouse.
4. Every money value on screen originates from the shared component (grep-based test).
5. Filter state is URL-encoded and shareable/restorable.
6. Aggregate tiles show exclusion counts (test with a fixture where 34 properties lack value data).

**Effort.** 12–15 dev-days.

---

### WP-13 — Frontend: property deal page

**Purpose.** Spec §11.7 — executive summary, scenario toggle, financial breakdown, strategy tabs, offer simulator, distress timeline, evidence drawer, flags, notes, history.
**Depends on.** WP-0 contracts, WP-12's shared layer (available day 3), MSW fixtures.
**Owns.** `web/src/features/property/`.

**Implementation notes.**
- One `/analysis` call supplies everything. The scenario toggle switches a key in the already-loaded payload — no refetch.
- **Offer slider:** snaps to the 9 grid points and linearly interpolates between them (exact, per WP-7's linearity guarantee). Exact-entry field posts to `/offers` for an authoritative value. No formula reimplementation in TypeScript — if you find yourself writing arithmetic beyond linear interpolation, the API is missing a field.
- **Proceeds display refuses to collapse a range.** The component's props require low/expected/high when potential liabilities exist; make the single-value variant impossible to construct in that case.
- Evidence drawer: pdf.js at the cited page with `snippet` text-search highlighting. Shows the resolved value, method, every competing candidate with source and score, and any human override with attribution.
- Timeline merges foreclosure, bankruptcy, lien, listing, mortgage, and ownership events; postponements chain under their parent sale.
- Flags are resolvable inline; the confirmation toast shows the score and rank delta returned by the API.

**Acceptance criteria.**
1. Renders correctly for all 12 normalized fixtures, including the ugly ones (no value data, no debt data, negative equity, missing APN).
2. Scenario toggle causes zero network requests.
3. Slider interpolation matches server values to the cent across all grid segments (automated comparison test).
4. A property with potential liabilities cannot render a single proceeds figure (type-level test).
5. Evidence drawer opens to the correct page and highlights the snippet for all 12 fixtures.
6. Estimated values are visually distinct from recorded values (snapshot test).

**Effort.** 12–15 dev-days.

---

### WP-14 — AI analyst

**Purpose.** Spec §13 — structured query, comparison, simulation.
**Depends on.** WP-0 contracts; WP-8's `components` key names; WP-11's filter grammar; a seeded DB.
**Owns.** `analyst/`, the semantic views (`v_property`, `v_liens`, `v_foreclosure`, `v_offers`), the read-only DB role.

**Implementation notes.**
- Route classification first (cheap model): `structured_query | comparison | simulation | unsupported`.
- **Structured query:** text-to-SQL against the semantic views only. Guardrails: dedicated read-only Postgres role, `sqlglot` parse asserting a single `SELECT` with no CTE writes/DDL/DML, forced `LIMIT 500`, 5s statement timeout. SQL is returned to the UI, editable, and convertible to a saved view via the filter grammar where possible.
- **Comparison:** deterministic. Diff two `ScoreSet.components` dicts, rank terms by absolute delta, hand the top 5 plus their raw values to the model **for phrasing only**. Validate the output contains no number absent from the input payload; on failure, emit the templated version.
- **Simulation:** parse the offer amount and property reference, call `POST /properties/{id}/offers`, narrate the returned numbers. Zero model arithmetic.
- Cache `(normalized_question, data_version)` → answer, 15 minutes.

**Acceptance criteria.**
1. A 30-question benchmark suite (committed) — ≥90% return correct row sets, judged against hand-written SQL.
2. Every one of 15 adversarial prompts (`DROP TABLE`, `; UPDATE`, `pg_read_file`, cross-schema access) is rejected before execution.
3. Comparison output is deterministic in its *numbers* across 20 runs (phrasing may vary).
4. Number-validation rejects a deliberately poisoned model response.
5. Zero-row results state the interpreted filters rather than producing a narrative.

**Effort.** 7–9 dev-days.

---

### WP-15 — Exports and documents

**Purpose.** Spec §9.4, §20 — deal sheet PDF, seller net sheet PDF, filtered CSV, full data export.
**Depends on.** WP-0 contracts; fixtures for analysis payloads.
**Owns.** `exports/`, HTML templates, the export job.

**Implementation notes.**
- WeasyPrint from HTML templates; the templates consume the same `/analysis` payload the deal page does, so there is one source of truth for what a property "is."
- **Deal sheet footer must list which figures are estimated** — pull it from `is_estimated` flags, don't hand-maintain it.
- **Net sheet must state which obligations are unverified** and show the proceeds range in plain language. This document may end up in front of a seller; it cannot imply certainty the data doesn't support.
- CSV export: honors the current filter grammar and column selection, streams (never materializes 100k rows in memory).
- Full export: one command producing `properties.csv`, `liens.csv`, `mortgages.csv`, `scores.csv`, `facts.jsonl` + the documents folder.

**Acceptance criteria.**
1. Deal sheets render for all 12 fixtures, single page, no overflow, including the missing-data cases.
2. Estimated-value footnotes match the payload's `is_estimated` flags exactly (automated diff).
3. Net sheet shows a range whenever potential liabilities exist (test the owner-only-lien fixture).
4. 100k-row CSV export streams with flat memory usage.
5. Full export round-trips: exported data can rebuild an equivalent read-only dataset.

**Effort.** 5–6 dev-days.

---

### WP-16 — Change detection and watchlist

**Purpose.** Spec §14 — diff re-ingested reports, record change events, surface "What Changed."
**Depends on.** WP-5's `NormalizedProperty` (before/after), WP-8's scores.
**Owns.** `changes/`, table `change_events`, watchlist columns.

**Implementation notes.**
- Snapshot the prior `NormalizedProperty` hash + key fields before recompute; diff after. Diff at the *field and entity* level, not the JSON-blob level, so "new lien" is distinguishable from "lien amount corrected."
- Change types are a closed enum (spec §14). Anything not in the enum is not a change worth reporting.
- Watchlist: manual flag or auto-add at `OVERALL ≥ 70`.
- Daily digest only — no per-event notifications.

**Acceptance criteria.**
1. Ingesting a v2 report for a fixture property produces exactly the expected change events (fixture pair committed).
2. Re-ingesting an identical report produces zero change events.
3. A lien amount correction and a genuinely new lien produce different change types.
4. Score delta and rank delta recorded correctly on each change.

**Effort.** 4–5 dev-days.

---

### WP-17 — Calibration

**Purpose.** Spec §22 — learn your real repair costs, value weights, and holding periods from closed deals.
**Depends on.** WP-6's `AssumptionSet` shape; `realized_deals`.
**Owns.** `calibration/`, table `realized_deals`, the calibration page.

**Implementation notes.**
- Entry form for actuals: purchase, repairs, days held, sale price, costs, outcome.
- Analyses: predicted vs. actual repairs per condition level (suggest new $/sqft); predicted vs. actual sale price per valuation candidate type (suggest weight changes); predicted vs. actual holding period; gut rating vs. Overall Score correlation.
- **Suggestions are proposals.** Accepting one creates a new `AssumptionSet` version and enqueues a bulk recompute with a before/after preview. Nothing auto-applies.
- Requires a minimum sample (default 5 deals per condition level) before suggesting; below that it shows the data and no recommendation.

**Acceptance criteria.**
1. With 20 synthetic closed deals, suggested repair $/sqft is within 5% of the planted true value.
2. Below the minimum sample, no suggestion is produced.
3. Accepting a suggestion creates a new version and leaves the old one intact and rollback-able.
4. Preview shows accurate before/after rank changes.

**Effort.** 4–5 dev-days.

---

### WP-18 — Ops, cost control, settings

**Purpose.** Spec §19, §20, §21, §4.8 — budgets and cost meter, pre-flight estimate, Problems page, backups/restore, deployment, settings.
**Depends on.** WP-2's token estimates; WP-4's cost accounting; failure codes from everyone.
**Owns.** `ops/`, tables `settings`, `history`, deployment config, backup scripts.

**Implementation notes.**
- **Budget interface (used by WP-4, define it early):** `budget.check_and_reserve(batch_id, estimated_cost) -> Allowed | Paused`. Hitting the cap pauses the batch; it does not fail jobs or silently overspend.
- **Pre-flight estimate:** after classification and sectioning (both free), sum `token_estimate` × per-model price → dollar and time estimate → `awaiting_confirmation`. Nothing hits the API until confirmed.
- Live cost meter per batch, lifetime total in settings.
- **Problems page:** one query over failed reports, unmatched sections, dead-letter jobs, extraction errors, and grounding failures, each with a Retry action. Failure codes are a shared enum in `contracts/`.
- Backups: nightly `pg_dump` + `restic` of `documents/`, 30 daily + 12 monthly, encrypted. **`make restore` must be tested in CI against a scratch database — an untested restore is not a backup.**
- Deployment: Docker Compose, Caddy TLS, `.env` template, one-command deploy and rollback, health endpoint.

**Acceptance criteria.**
1. A batch that exceeds its budget mid-run pauses and resumes correctly after the budget is raised.
2. Pre-flight estimate within 15% of actual spend on the 20-fixture batch.
3. Every failure code in the shared enum renders on the Problems page with a working retry.
4. CI performs a full backup → wipe → restore → data-integrity check.
5. Cold deploy from an empty machine to a working system in under 15 minutes, documented.

**Effort.** 5–7 dev-days.

---

## 8. Integration gates

Integration is not "one big merge at the end." Four gates, each an owned deliverable with tests in `tests/integration/`.

**Gate A — Documents to units (WP-1 + WP-2 + WP-3).**
Real PDFs in → correct `reports`, `extraction_units`, `properties`. Test: the 20 fixture PDFs produce the committed unit set and the expected property count. *Replaces the ingestion stubs in WP-10.*

**Gate B — Units to facts (Gate A + WP-4).**
Real extraction against a **recorded-response mock** of the model (record real responses once, replay in CI — never call the live API in tests). Test: fixture units produce the committed facts, with grounding enforced.

**Gate C — Facts to scores (Gate B + WP-5/6/7/8/9).**
Test: fixture facts flow through to committed `ScoreSet`s and flags. This is the gate that proves the numeric core is correct end to end; it should be the most heavily tested thing in the repo.

**Gate D — Full stack (Gate C + WP-10/11/12/13).**
Playwright: upload a batch, confirm the cost, wait for completion, verify the ranked list, open a deal page, drag the slider, resolve a flag, confirm the rank moves, export a deal sheet.

**Gate E — Ancillary (WP-14/15/16/17/18).**
Each verified against a fully populated database from Gate D.

**Rule:** a gate is owned by one named person, and packages entering it must already be green on their own acceptance criteria. Gate owners fix integration bugs by filing them back to package owners, not by patching other people's code.

---

## 9. Staffing plans

**Solo (~5–6 months).**
Strict order: WP-0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 18 → then 15, 14, 16, 17. Don't build ahead of the fixtures.

**Three devs (~10–12 weeks).**
- **Dev A (lead):** WP-0, then 4 (extraction), 10 (pipeline), Gates B/C.
- **Dev B (backend):** WP-1, 2, 3, then 5, 9, 11, 18.
- **Dev C (full-stack/frontend):** WP-6, 7, 8 first (pure libraries, no backend dependency), then 12, 13, 15.

**Six devs (~7–8 weeks).**
- Lead: WP-0, then 10 + all gates.
- Dev 2: WP-1 + WP-2 + WP-18.
- Dev 3: WP-3 + WP-5 (the identity/normalization axis — same mindset, adjacent data).
- Dev 4: WP-4 + WP-14 (both LLM-facing).
- Dev 5: WP-6 + WP-7 + WP-8 (the numeric core; one owner keeps it coherent).
- Dev 6: WP-12 + WP-13. Add a seventh for WP-11 + WP-15 if available.

**Ten or more.** Split the largest packages along their internal seams: WP-4 into (prompts + schemas) and (validation gauntlet + cost accounting); WP-12 into (shell + table) and (dashboard + compare + map); WP-5 into (resolver + conflicts) and (entity dedupe + derived values); assign WP-9, WP-15, WP-16, WP-17, WP-18 individually. Beyond ~10, add QA and a dedicated fixtures/gold-set owner rather than more feature devs — coordination cost rises faster than throughput.

**Never split:** WP-6/7/8 across three people (the numeric core needs one coherent mind), or WP-10 (the orchestration invariants live in one head).

---

## 10. Cross-cutting standards

Put these in `CONTRIBUTING.md` on day one; they cost nothing up front and save the integration from a hundred small inconsistencies.

- **Money:** `Decimal` everywhere, `numeric(14,2)` in Postgres, strings in JSON. Percentages as decimals (0.065, not 6.5). CI rejects floats in `finance/`, `strategies/`, `scoring/`.
- **Dates:** `date` for real-world events, `timestamptz` UTC for system events. No naive datetimes.
- **IDs:** UUIDv7 (time-ordered, index-friendly).
- **Nulls:** never a sentinel (`0`, `-1`, `""`). Null + a reason code from the shared enum.
- **Errors:** one taxonomy in `common/errors.py`; every failure has a stable code that appears on the Problems page.
- **Logging:** structured JSON with `property_id`, `report_id`, `job_id` where applicable. No PII in logs.
- **Testing:** unit tests inside packages; contract tests validating outputs against JSON Schema; integration tests only in `tests/integration/`. LLM calls use recorded responses — CI never touches a live API.
- **Config:** `pydantic-settings`, env vars, one `.env.example` kept current.
- **Versioning:** `engine_version`, `resolution_version`, `prompt_version`, `scoring_config_id`, `assumption_set_id` are stamped on every derived record. Anything that changes a number bumps a version, or you lose the ability to explain the past.
- **Docs:** each package owns a `README.md` with its public interface — the functions other packages may call. If it's not in the README, it's private.

---

## 11. Project definition of done

1. Every package green on its own acceptance criteria.
2. Gates A–E green in CI.
3. Extraction gold set: ≥97% on the 22 critical fields, ≥99% on lien attachment basis, <2% grounding failures.
4. Full numeric core determinism suite green (finance, strategies, scoring, 100 runs each).
5. 2,000-document batch processed end to end within budget and within the estimated time ±20%.
6. Backup → wipe → restore verified in CI.
7. Cold deploy documented and executed from scratch in under 15 minutes.
8. No float in any money path; no endpoint returning a bare number for money; no rendered value that bypasses the shared money component.
9. The two rules hold, verifiably: no dollar figure originates in a model (traceable via `source_kind` and `engine_version` on every stored number), and no owner-only lien appears in confirmed liabilities anywhere in the system.
