# Property Acquisition Analysis Platform — Developer Specification (v2, lean build)

**Context this version is written for:** a private, everyday underwriting tool used by one person or a small team. Not a SaaS product. No customers, no compliance auditors, no sales demos. Every feature has to earn its place by saving *you* time or stopping *you* from making a costly mistake.

**Codename:** `ACQ`

---

## 0. What changed from v1, and why

### 0.1 Removed as overkill

| Removed | Why it was overkill for a private tool |
|---|---|
| Multi-tenancy, `org_id` on every table, Postgres RLS | There is one tenant. RLS adds a session-variable dance to every query for zero benefit. |
| SSO/SAML/OIDC, WorkOS/Auth0, scoped API keys, 5-role RBAC | One password and (optionally) Cloudflare Access or Tailscale in front. Roles: you, and maybe a second person. |
| Audit log with 7-year retention, append-only grants, SOC 2 | You don't need to prove anything to a regulator. Keep a lightweight change history because *you* want to know why a number moved — that's a different, much smaller thing. |
| Microservices, Kafka, Airflow, Kubernetes, three autoscaling worker pools | One worker process with two concurrency settings. Docker Compose on one machine. |
| Redis as a required component | Postgres-backed job queue. Two containers total (app + db) instead of five. |
| Read replicas, hash partitioning, columnar migration plan | You will have tens of thousands of properties, not millions. A single Postgres with good indexes handles this on a laptop. |
| Presigned S3 URLs, SSE-KMS, bucket policies | Local disk for documents + nightly encrypted backup to cheap object storage. |
| ClamAV virus scanning, zip-bomb hardening | These are your own vendor PDFs, not hostile uploads. Keep a file-count/size sanity check and move on. |
| Shared TS/Python formula module compiled from a spec | Replaced by **precomputed offer grid + client-side interpolation** (§9.3). Same instant slider, one implementation, no drift bugs. |
| SSE progress streams | Poll a status endpoint every 2 seconds. |
| Next.js / SSR | Vite + React SPA served as static files by the API. |
| Elasticsearch, vector DB, RAG over PDFs | Postgres trigram search. Extraction happens once into structured tables. |
| Bounding-box evidence coordinates | Page number + verbatim snippet, highlighted by pdf.js text search. |
| Formal review-*task* system (assignment, priority queues, SLAs, false-positive reports) | Replaced by **flags** attached to properties, resolvable inline (§13). Same protection, a tenth of the code. |
| Blocking CI eval harness on prompt changes | A `make eval` script you run manually against a 40-document gold set. |
| MLS/IDX, title-provider API, permit data, geospatial layers, webhooks, public API, scheduled reports | Licensing, integration, and maintenance cost far beyond the value to a solo operator. |
| Continuous county scraping / paid foreclosure feeds in the base build | Re-ingest refreshed vendor reports and diff. Add a paid feed later only if you're actively bidding at auctions. |
| Custom OCR, fine-tuned models, in-house AVM | Frontier model + schema validation; use the reports' own comps. |

### 0.2 Added because it was missing for daily private use

| Added | Why |
|---|---|
| **Cost pre-estimate + hard budget** (§20) | You pay the API bill. Before extracting 400 PDFs, see "≈ $38, 92 min" and confirm. |
| **Deal sheet PDF + seller net sheet PDF** (§9.4) | The two artifacts you actually send to a partner, lender, or the seller. |
| **Notes, tags, pipeline status, next-action date** (§11.3) | Without these you'll keep a parallel spreadsheet, and the parallel spreadsheet becomes the real system. |
| **Keyboard triage mode** (§11.4) | Triaging 200 properties with a mouse is the actual daily bottleneck. |
| **Side-by-side compare (2–4 properties)** (§11.5) | The real decision is rarely "is this good" — it's "which of these three." |
| **Assumption sandbox with before/after preview** (§7.6) | Change your target margin from 20% → 25% and see who drops off the list *before* committing. |
| **Manual property entry + paste-text ingestion** (§4.7) | Half your leads arrive as an email, a text message, or a screenshot. |
| **Re-extraction from cached page text** (§20.3) | Improve a prompt, re-run everything without re-uploading or re-OCRing. |
| **What-changed diff view** (§15) | When a refreshed report arrives, one screen shows exactly what moved and by how much. |
| **Calibration loop from your closed deals** (§23) | After 10 deals, your repair $/sqft and value weights beat any vendor's defaults. This is the only real moat and it costs almost nothing to build. |
| **One-command backup and restore; full data export** (§21) | A private tool with no backup is a liability. Also guarantees you're never locked in. |
| **Problems page** (§4.8) | One place listing every failed file, unparsed section, and stuck job. |
| **Map view of a batch** (§11.6) | Cheap (Leaflet + geocoded lat/lng you already store) and genuinely useful for spotting clusters. |

---

## 1. Product goal and operating constraints

Ingest property report PDFs, turn each property into one normalized, source-traced record, underwrite it deterministically across several exit strategies, and rank the results so you can decide what to pursue today.

**Two rules govern everything and are non-negotiable:**

1. **The LLM extracts and classifies. It never calculates.** Every dollar you see comes from deterministic Python on `Decimal` values.
2. **Absence is a value.** Missing → `null` with a reason. A person-level lien never becomes a property lien without recorded evidence.

**Operating constraints this build assumes:**
- 1–3 users, one machine (a $40/month VPS, a spare Mac mini, or your laptop).
- 200–3,000 properties per batch; 20k–100k properties lifetime.
- You pay per-token costs directly and care about them.
- Downtime for 20 minutes is annoying, not catastrophic. No HA requirement.

---

## 2. Daily workflow

1. **Drop files.** Drag 400 PDFs (or a ZIP) onto the upload box. Optionally tag the batch (`Riverside NOD — Aug 2026`).
2. **Confirm the cost.** System dedupes, classifies, sections, and shows: *"312 new documents, 88 already seen. Estimated 1,910 extraction units, ≈ $34.80, ≈ 74 minutes. Start?"*
3. **Walk away.** Progress bar; the Problems page collects anything that failed.
4. **Triage.** Land on the portfolio table sorted by Overall Score. Enter keyboard mode: `j/k` to move, `space` to peek at the summary card, `p` to mark Pursue, `x` to kill, `f` to flag for review.
5. **Underwrite the shortlist.** Open a deal page, flip Conservative/Expected/Optimistic, drag the offer slider, check the two or three numbers that matter by clicking through to the PDF page.
6. **Resolve flags.** Anything gated (unclear lien attachment, conflicting balance) is a one-click inline resolution with the PDF beside it.
7. **Act.** Export a deal sheet PDF, a seller net sheet, or a CSV of the shortlist. Set a next-action date.
8. **Later.** New reports arrive for tracked properties → the What Changed page shows the deltas and the rank movement.

---

## 3. Architecture

```
        Browser (React SPA, static build)
              │  REST/JSON, polling
        ┌─────▼──────────────────────────────┐
        │ FastAPI (Python 3.12)              │
        │  API + serves the SPA + auth       │
        └─────┬───────────────────┬──────────┘
              │                   │
     ┌────────▼────────┐   ┌──────▼───────────────┐
     │ Postgres 16     │   │ ./documents/         │
     │ data + job queue│   │ originals + page text│
     │ + pg_trgm       │   └──────────────────────┘
     └────────▲────────┘
              │
     ┌────────┴──────────────────────────┐
     │ worker.py (same image, --worker)  │
     │  ingest · ocr · extract · compute │
     └────────┬──────────────────────────┘
              │
      Anthropic API (structured output)
```

**Total moving parts: two containers** (`app`, `db`) plus one worker process of the same image. Caddy in front for automatic TLS if exposed; otherwise bind to localhost and reach it over Tailscale.

**Stack:**
- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.0, Pydantic v2, PyMuPDF, OCRmyPDF/Tesseract, `usaddress`.
- **Queue:** Postgres-backed (`procrastinate`, or a 60-line `jobs` table + `SELECT ... FOR UPDATE SKIP LOCKED` loop). No Redis.
- **DB:** Postgres 16 with `pg_trgm`. That's the only extension you need.
- **Frontend:** Vite + React + TypeScript, TanStack Query + Table, Tailwind, Recharts, pdf.js, Leaflet.
- **Docs:** local filesystem, path `documents/{report_id}/original.pdf`. Nightly `restic` backup to B2/S3.

**Why Python:** PDF tooling, LLM SDKs, and the financial engine all live comfortably there, and the financial engine is the part you'll edit most.

---

## 4. Document ingestion

### 4.1 Upload
- **Input:** `POST /api/uploads` multipart, or drag-drop of many files, or a `.zip`. Also a watched folder (`./inbox/`) — drop files there from any script and they get picked up. Limits: 300 MB/file, 3,000 files/batch.
- **Processing:** write to disk, compute `sha256`, insert `reports` row (`status='uploaded'`), enqueue `ingest_document`.
- **Output:** `batch_id`, live counters.
- **Failure:** encrypted → `ENCRYPTED`; corrupt → `CORRUPT`; not a PDF (magic-byte check, not extension) → rejected with a message. All failures land on the Problems page; nothing disappears silently.

### 4.2 File dedupe
`sha256` unique. Exact re-upload links to the existing report, sets `duplicate_of`, skips extraction. Analysts re-upload constantly; this alone saves 10–20% of the API bill.

A regenerated report (same property + type, newer `generated_date`) is *not* a duplicate — it's the monitoring signal (§15).

### 4.3 Text extraction and OCR
1. PyMuPDF extracts per-page text + metadata.
2. **Scanned test:** median `chars_per_page < 100`, or >40% of pages empty → OCR path.
3. **OCR:** OCRmyPDF/Tesseract at 300 DPI, output stored *alongside* the original. `ocr_applied=true` caps every fact from that document at `confidence ≤ 0.80`.
4. Per-page text written to `documents/{report_id}/pages/{n}.txt`. This file is what makes snippet grounding, re-extraction, and search free later. **Never delete it.**

**Cost note:** OCR is the slowest step (~5–20 s/page of CPU). Hard timeout 10 min/doc; on timeout OCR the first 15 pages and flag `PARTIAL_OCR`.

### 4.4 Classification
Assign `report_type` ∈ {`property_profile`, `owner_report`, `mortgage`, `foreclosure`, `lien`, `bankruptcy`, `tax`, `comparables`, `valuation`, `listing_history`, `ownership_history`, `rental`, `combined`, `unknown`}.

1. **Rules first:** regex on page-1 header text and filename, stored in a `document_signatures` table you can edit in the UI without a deploy. Resolves ~85–90% at zero cost.
2. **LLM fallback:** unmatched docs only — first 1,500 chars to the cheap model → `{report_type, vendor, confidence}`.

Combined reports (one 40-page PDF containing everything) get *sectioned* rather than typed (§5.1).

### 4.5 Property identity resolution
The highest-consequence step in the pipeline. A bad merge fuses two properties' debts; a bad split hides half the liens.

1. **APN match.** `apn_key = FIPS + uppercase alphanumerics only`. Exact match → same property. Only automatic-merge path.
2. **Address match.** `usaddress` parse → USPS normalization → `address_key = number|street|suffix|unit|zip5` → hash. Exact → same property.
3. **Fuzzy.** `pg_trgm` similarity within the same ZIP: ≥ 0.92 *and* same house number → merge. 0.80–0.92 → create separately and raise a `possible_duplicate` flag.
4. Otherwise → new property.

**Conflict rule:** APN matches but ZIP or house number differs → do **not** merge; raise `identity_conflict`. Vendor APN typos are common enough that blind trust is dangerous.

**Merges are reversible** (`merged_into_id` soft pointer, unmerge re-parents reports and recomputes both). Never hard-delete a property.

### 4.6 Traceability
Every fact stores `report_id + page_number + snippet`. A fact without evidence fails validation and is discarded — enforced in code, not by convention.

### 4.7 Manual and paste ingestion *(new)*
- **Quick add:** address + APN → creates a property with no facts; you can then attach documents or type values manually (stored as `source_kind='human'`).
- **Paste text:** a textarea that accepts a pasted email, listing description, or OCR'd screenshot. It becomes a pseudo-report (`vendor='pasted'`, one page) and runs the identical extraction pipeline. Facts from pasted text are capped at `confidence 0.7`.

**Why:** roughly half of real-world leads never arrive as a clean vendor PDF, and without this you'll maintain a side spreadsheet.

### 4.8 Problems page *(new)*
One table: every failed file, unclassified document, section that matched no regex, stuck job, and API error, with the reason and a Retry button. This replaces v1's monitoring/alerting stack. Check it once per batch.

---

## 5. AI extraction

### 5.1 Sectioning (deterministic, pre-LLM)
Never send a 40-page report in one call — it's expensive and accuracy sags mid-context.

1. Split on header regexes per vendor (`OWNER INFORMATION`, `MORTGAGE/TRANSFER HISTORY`, `FORECLOSURE DETAIL`, `INVOLUNTARY LIENS`, `COMPARABLE SALES`, `TAX INFORMATION`, `BANKRUPTCY`).
2. Fall back to 3-page windows with 1-page overlap when nothing matches.
3. Emit `extraction_units`: `{report_id, unit_type, page_range, text}`.

**Payoff:** one 45k-token call becomes six 3–8k-token calls, each with a *narrow* schema. ~35% cheaper and measurably more accurate, because the lien call's schema contains only lien fields.

### 5.2 The call
- **Model routing:** cheap/fast model for comps tables, listing history, tax tables, classification. Frontier model for liens, mortgages, foreclosure, bankruptcy — the places where ambiguity costs money.
- Temperature 0, structured-output/tool mode enforced.
- **Output:** typed objects, each carrying `value_raw`, `value_parsed`, `page_number`, `snippet`, `extraction_confidence`, `null_reason`.

### 5.3 Validation gauntlet (deterministic; the model never gets the last word)
1. **Schema validation** (Pydantic). One repair retry with the error appended; second failure → drop + flag.
2. **Snippet grounding.** Normalize whitespace/case, assert the snippet exists in the stored text of that page. If not, the model hallucinated or misattributed → drop the fact. *Costs nothing, catches most fabrications. This is the single most valuable check in the system.*
3. **Parse consistency.** `value_parsed` must be mechanically derivable from `value_raw` ($ , parentheses-negatives, `1.2M`).
4. **Range sanity.** Sale price $1k–$100M · sqft 100–100k · beds 0–30 · year 1600–now+2 · rate 0–25% · lien $1–$50M. Out of range → stored but inactive + flag.
5. **Cross-field logic.** Sale date ≥ year built; NTS ≥ NOD; balance ≤ original × 1.5; a second mortgage requires a first.
6. **Null discipline.** `null` requires `null_reason` ∈ `not_present | illegible | redacted | conflicting_in_source`.

### 5.4 The lien attachment rule (the most important rule in the product)
Every lien carries:
```
attachment_basis: "recorded_against_property" | "owner_named_only" | "unknown"
attachment_confidence: 0.0–1.0
```
`recorded_against_property` is permitted **only** when the snippet contains a parcel anchor — APN, legal description, a recording reference for this parcel, or the subject address. A federal tax lien naming only "JOHN A SMITH" is `owner_named_only`, full stop.

**Consequence:** `owner_named_only` and `unknown` liens are excluded from confirmed liabilities, included in *potential* liabilities, and raise a flag above $10,000. This is where naive tools produce six-figure equity errors.

### 5.5 Failure handling

| Failure | Detection | Response |
|---|---|---|
| Invented mortgage/lien | Snippet grounding | Dropped, counted in the weekly grounding-failure number on the Problems page |
| Vendor template drift | Section match-rate falls | Falls back to page chunking (degrades, doesn't break); Problems page shows the rate |
| OCR garbage | Confidence cap + range sanity | Facts flagged; Data Confidence drops, which gates the score (§10.5) |
| API 429/5xx | Retry ×5, exponential backoff | Then `extraction_failed`, visible + retryable |
| Cost overrun | Running batch total vs. budget | Batch pauses and asks, rather than silently burning money |

---

## 6. Normalization

### 6.1 The fact ledger
`extracted_facts` is append-only. Nothing is updated or deleted. Every conflicting value from every report survives, which is what lets you answer "why did we think the balance was $412k?" six months later.

Resolution is a *view* over the ledger: `field_resolutions` stores, per `(property_id, field_path)`, the winning fact, the method, and the contenders.

### 6.2 Resolution precedence
```
score = 0.40 × source_rank + 0.30 × recency + 0.20 × specificity + 0.10 × corroboration
```
- **source_rank:** human override 1.0 · verified API 0.9 · county-recorder section 0.85 · vendor report 0.7 · derived/estimated 0.5 · pasted text 0.45 · ×0.85 multiplier if OCR'd.
- **recency:** exponential decay on the *event's* as-of date, not the report date. Half-life 180 days for money, 3 years for structural facts.
- **specificity:** recorded exact figure > stated range > rounded estimate.
- **corroboration:** +0.1 per independent agreeing report (2% money tolerance, exact dates), capped.

**Ties break conservative:** highest liability, lowest asset value. Being wrong optimistically is what loses money.

### 6.3 Conflicts

| Field | Tolerance | On breach |
|---|---|---|
| Value estimates | 15% | Flag only if ratio > 1.5; dispersion otherwise feeds confidence |
| Mortgage balance | 5% or $10k | `conflicting_mortgage` flag |
| Lien amount | any | Dedupe first; if genuinely different → flag |
| Foreclosure sale date | any | Take latest recorded; postponements go in the timeline |
| APN / address | any | `identity_conflict`, gating |

Conflicts lower Data Confidence whether or not you resolve them. Unresolved uncertainty must show up in the ranking, not hide.

### 6.4 Entity dedupe
- **Mortgages:** same if `recording_doc_number` matches, or `(lender_normalized, date ±30d, amount ±1%)`. Lender aliases in a small table (`WELLS FARGO BANK NA` ≡ `WELLS FARGO HOME MTG`).
- **Liens:** same if doc number matches, or `(type, creditor_normalized, amount ±$1, date ±7d)`. Released liens retained with `status='released'`, excluded from liabilities, shown in the timeline.
- **Foreclosure events:** keyed `(trustee_sale_number, event_type, event_date)`. Postponements are new events, never edits.

### 6.5 Derived mortgage balance
When only the original amount and date exist, compute — don't guess:
```python
def estimate_balance(original, rate, term_months, origination_date, as_of):
    n = months_between(origination_date, as_of)
    if n <= 0: return original
    if n >= term_months: return Decimal(0)
    r = rate / 12
    return original * ((1+r)**term_months - (1+r)**n) / ((1+r)**term_months - 1)
```
Missing rate → `historical_rate_index` (year × loan type), `confidence = 0.55`, and widen the band: conservative uses rate −150bps (higher balance), optimistic +150bps. Tag `source_kind='derived'`, `derivation='amortization_v1'`. Always rendered with the "estimated" treatment.

---

## 7. Financial engine

Pure Python, `Decimal`, `ROUND_HALF_UP`, no floats, no LLM:
```python
def underwrite(record: NormalizedProperty, assumptions: AssumptionSet) -> UnderwritingResult
```
Deterministic and reproducible. `assumption_set_id` + `engine_version` are stored on every result so you can diff why a number changed.

### 7.1 Three scenarios, not interval arithmetic
Naive interval math compounds worst cases into meaningless ±$500k bands. Instead, three internally consistent assumption vectors evaluated end to end:

| Input | Conservative | Expected | Optimistic |
|---|---|---|---|
| Market value | `V_low` | `V_exp` | `V_high` |
| Repairs | high | expected | low |
| Holding period | ×1.5 | ×1.0 | ×0.75 |
| Potential liabilities | in full | × attachment probability | excluded |
| Resale cost % | high | standard | low |

### 7.2 Asset value — never one AVM

| Candidate | Weight | Adjustment |
|---|---|---|
| Vendor AVM | 0.30 | × its own confidence × recency decay (90-day half-life) |
| Comparable sales estimate | 0.35 | × comp quality (count, distance, recency, sqft delta) |
| Comparable listing estimate | 0.15 | asks, not sales — discounted |
| Recent list price (≤12 mo) | 0.10 | |
| Assessed ÷ county ratio | 0.10 | only where ratios are reliable |

```
V_exp = Σ(value_i × w_i) / Σ(w_i)
disp  = clamp(weighted_stdev / V_exp, 0.04, 0.30)
V_low  = max(V_exp × (1 − disp), comp_range_low or 0)
V_high = min(V_exp × (1 + disp), comp_range_high or ∞)
```
One candidate only → force `disp = 0.15` and cap valuation confidence at 0.5. One estimate is not agreement.

**ARV:** only when repairs are specified. Prefer comps filtered to renovated condition; otherwise `ARV = V_exp + repairs × recapture_multiplier` (default 1.0, deliberately unaggressive), flagged as assumption-driven.

### 7.3 Liabilities — three buckets, never one
```
CONFIRMED = first + second + heloc_drawn
          + liens WHERE attachment_basis='recorded_against_property' AND status='active'
          + delinquent property taxes + confirmed HOA arrears

POTENTIAL = liens WHERE attachment_basis IN ('owner_named_only','unknown')
          + liens with unknown amounts (valued at type median, flagged)
          + undrawn HELOC capacity

MAXIMUM   = CONFIRMED + POTENTIAL
```
Median defaults for unknown amounts: HOA $4,500 · mechanics $12,000 · judgment $18,000 (replaced by your own medians once you have data).

**Published bid reconciliation:** a trustee's published bid is usually the best available payoff figure, since it's their actual accounting. If `|bid − estimated_first_balance| / bid > 0.20`, flag `bid_mismatch`; precedence otherwise favors the bid.

### 7.4 Equity
```
Gross    = V − CONFIRMED
Adjusted = V − CONFIRMED − (POTENTIAL × attachment_probability)
Net realizable = V×(1 − resale_cost_pct) − CONFIRMED − POTENTIAL_weighted − holding
Equity % = Gross / V
```
`attachment_probability` defaults: `owner_named_only` 0.35, `unknown` 0.50 — configurable globally and per lien.

### 7.5 Cost models (all in `assumption_sets`, versioned)
- **Acquisition:** closing 1.0% · title 0.5% · escrow $1,500 · transfer tax from a county table · financing 2 pts + $1,200 · inspection $600 · legal $1,500 · assignment fee (wholesale) · acquisition fee 1.0%.
- **Repairs:** (1) your manual line items always win; (2) AI extracts **condition signals only** into `pristine|cosmetic|moderate|heavy|gut`, and deterministic code maps `(condition, sqft, regional index)` → $/sqft (defaults 18/42/78/135) — **the LLM never emits a repair dollar figure**; (3) fallback `sqft × moderate` with a wide band. Low/exp/high = ×0.75 / ×1.0 / ×1.4.
- **Holding (monthly):** taxes/12 + insurance (0.35%/yr) + utilities $180 + HOA + maintenance (0.5%/yr) + loan interest. Period = 2 months acquisition + repair duration by condition + market time (local DOM if known, else 60 days).
- **Resale:** commission 5.0% (0 if selling to an investor) + seller closing 1.0% + concessions 1.0% + staging $3,500 (flip only) + misc 0.25%.

### 7.6 Assumption sandbox *(new)*
Edit any assumption in a modal and see, **before saving**: how many properties move in or out of your "strong" bucket, the top 10 rank changes, and the aggregate equity delta. Save creates a new version; the old one is retained so old results stay explicable. Rollback is one click.

**Why:** you will tune these constantly, and blind global recalculation destroys your trust in the list.

### 7.7 Engine failure cases

| Case | Behavior |
|---|---|
| No value candidates | `insufficient_data` — unranked, flagged. **Never given a default value.** |
| No debt records | Confirmed = 0 but `debt_data_present=false`; UI says "no debt records found," not "$0 debt." Confidence heavily penalized. |
| Negative equity | Supported and surfaced — it's a short-sale / subject-to signal, not an error. |
| Missing sqft | Flip/rental return `unavailable` with a reason; other strategies still compute. |
| ROI on $0 cash | `null`, displayed "n/a — fully financed." |

---

## 8. Deal strategies

Six strategies × three scenarios = 18 results per property, ~2 ms of Python. Anything uncomputable returns `unavailable` + reason, never a fabricated number.

**Cash**
```
all_in         = offer + acquisition + repairs + holding
profit         = V×(1−resale_pct) − all_in
roi            = profit / all_in
margin_of_safety = (V − all_in) / V
MAO_cash       = V×(1 − target_margin) − repairs − holding − acquisition − resale     # default margin 0.20
```

**Fix and flip**
```
MAO_flip = ARV×(1 − target_profit_margin) − repairs − holding − financing − resale − acquisition
profit   = ARV_net − (purchase + repairs + holding + financing + acquisition + resale)
margin   = profit / ARV
CoC      = profit / (down + unfinanced repairs + holding + closing)
```
Hard-money defaults: 10% rate, 2 points, 85% LTC, repairs drawn. Target margin 0.20 under $500k ARV, 0.15 above.

**Wholesale**
```
investor_threshold = ARV × 0.70 − repairs        # configurable
max_contract       = investor_threshold − target_assignment_fee
spread             = investor_threshold − contract_price
```
Marked viable only if `spread ≥ $15,000` **and** Data Confidence ≥ 60 — you can't assign a contract on a property whose lien picture is unknown.

**Rental**
```
EGI = rent×12 × (1 − vacancy 6%)
OpEx = taxes + insurance + HOA + maintenance 8% + mgmt 8% + reserves 5% + owner-paid utilities
NOI = EGI − OpEx ; cap = NOI/price ; cash_flow = NOI − debt_service ; DSCR = NOI/ADS
CoC = cash_flow / (down + closing + repairs)
```
No rent estimate → `unavailable: no_rent_data`. Never infer rent from value.

**Subject-to / creative — detection only.** Fires when: note rate ≥ 200bps below market **and** balance/value ≤ 0.80 **and** no acceleration **and** distress present. Output is a *templated* explanation of which conditions fired plus a due-on-sale/legal-review notice. Excluded from the Overall Score; `requires_human_review=true`. The AI writes nothing here.

**Foreclosure acquisition**
```
total_obligations = published_bid + surviving junior liens + delinquent taxes + transfer costs
spread            = V_conservative − total_obligations − repairs − auction_holding
```
Plus explicit flags, each a boolean with a source: junior liens present · IRS lien (120-day federal redemption) · HOA super-priority · owner-occupied (eviction cost/time from a table) · interior unknown (forces *high* repairs in all scenarios) · prior postponement count (≥3 materially lowers the odds the sale happens). Score capped at 70 unless Data Confidence ≥ 75.

---

## 9. Seller proceeds and net sheets

### 9.1 Offer grid
Nine points from `0.60 × V_exp` to `1.00 × V_exp`, rounded to $5,000, with `MAO_cash` and `MAO_flip` injected as marked points. Overridable with your own list.

### 9.2 Per-offer math
```
offer
− first mortgage payoff (balance + payoff interest/fees, default $1,200)
− junior mortgages / drawn HELOC
− property-attached liens
− delinquent taxes + penalties + prorations
− seller closing costs (title, escrow, transfer tax, recording)
− commission (0 if direct)
= SELLER PROCEEDS
```
Always three outputs: `high` (no potential obligation attaches) · `expected` (weighted) · `low` (all attach). If `low < 0` → short-sale banner + `short_sale_candidate` flag. **When potential liabilities exist, the UI component physically cannot render a single number — it requires a range prop.**

### 9.3 Instant slider without duplicated formulas *(simplification)*
The API returns the full 9-point grid (×3 scenarios) with the property payload. The slider snaps to grid points and, between them, linearly interpolates — which is *exact*, because every quantity here is linear in the offer price. Dragging is instantaneous with zero recomputation and zero second implementation. Off-grid exact entry posts to `/offers` and gets an authoritative answer.

### 9.4 Exports *(new)*
- **Deal sheet PDF (1 page):** address, photo placeholder, executive summary, value/debt/equity, top two strategies with MAO, distress timeline highlights, confidence, and a footer listing which figures are estimated. Generated with WeasyPrint from an HTML template.
- **Seller net sheet PDF:** the offer, the deductions, and the proceeds range in plain language, with a line stating which obligations are unverified. This is the sheet you put in front of a seller.
- Both are one click from the deal page and stored under `documents/{property_id}/exports/`.

---

## 10. Scoring

Four deterministic component scores, 0–100, then one weighted overall. **No number here comes from a language model.** Helper: `n(x, lo, hi) = clamp((x−lo)/(hi−lo), 0, 1)`.

**Financial Opportunity (FOS)**
```
FOS = 100 × ( 0.30×n(profit, 0, 150_000)
            + 0.25×n(roi, 0, 0.50)
            + 0.20×n(equity_pct, 0, 0.60)
            + 0.15×n(discount_to_value, 0, 0.35)
            + 0.10×n(margin_of_safety, 0, 0.35) )
```
`discount_to_value = (V_exp − MAO_best)/V_exp`. Bounds are configurable — $150k profit means different things in Cleveland and Palo Alto.

**Distress** — additive points, recency decay `0.5^(months/18)`, capped at 100:
NTS ≤30 days 30 · NTS >30 days 24 · NOD 18 · prior foreclosure activity 8 each (max 16) · active bankruptcy 12 · prior/dismissed bankruptcy 6 each (max 18) · repeat filings +8 · property-attached tax lien 10 · owner-only tax lien 4 · other involuntary liens 3 each (max 12) · taxes delinquent ≥2 yrs 10 · absentee 5 · ownership >15 yrs 4 · expired/cancelled listing 6 each (max 12) · high equity + distress +5.

Shown verbatim in the UI: *distress indicates financial pressure, not willingness to sell.*

**Data Confidence (DCS)**
```
DCS = 100 × ( 0.30×field_coverage + 0.20×corroboration + 0.20×recency
            + 0.15×(1 − conflict_penalty) + 0.10×verification_rate + 0.05×extraction_quality )
```
Coverage is measured over 22 critical fields (APN, address, sqft, beds, baths, year built, owner, occupancy, last sale date/price, AVM, comp estimate, assessed, taxes, 1st mortgage original/date/rate, balance, foreclosure status, lien count, lien total, rent).

**Risk**
```
RISK = clamp( 6×lien_count + 15×active_bankruptcy + 12×(stage in NTS/auction)
            + 10×owner_only_liens_over_10k + 10×title_flags + 8×owner_occupied
            + 8×hoa_arrears + 10×material_conflicts + 12×(DCS<50) + 6×federal_tax_lien, 0, 100)
```

**Overall**
```
OVERALL = clamp(0.50×FOS + 0.20×Distress + 0.20×DCS − 0.25×RISK, 0, 100)
```
**Gates (these matter more than the weights):** `DCS < 40` → capped at 45 and marked `needs_review`; `insufficient_data` → unranked; unresolved `identity_conflict` → unranked.

Weights/bounds/points live in one `scoring_config` row with a version; every score stores which version produced it.

**AI's only job here:** a 2–4 sentence explanation, given *only already-computed numbers*. Validation rejects any response containing a number not present in the input payload; the fallback is a template. Cheap, and it eliminates the most common way LLM prose goes wrong.

---

## 11. Ranking and UI

### 11.1 Ranking
`RANK() OVER (PARTITION BY scope ORDER BY overall DESC, property_id)`, materialized into `rankings` after each batch and nightly. Scope = batch, saved view, or whole portfolio — always displayed, because "#7 of 2,413" is meaningless without the denominator. Previous rank is kept in the same row so "moved up 14" is free.

**Sorts:** overall · profit · ROI · equity $ · equity % · discount · distress · lowest risk · highest confidence · wholesale spread · flip profit · cap rate · cash flow · next auction date.

**Filters:** city/ZIP/county · value · equity $ and % · foreclosure stage · days to auction · min profit/ROI · max offer · property type · beds/baths/sqft · owner-occupied vs absentee · bankruptcy · tax lien · lien count · DCS floor · RISK ceiling · batch/tag · pipeline status · flagged · changed in last N days.

**Saved views:** name + filters + sort + columns. Evaluated live.

### 11.2 Portfolio dashboard
Tiles: total analyzed · strong deals (`OVERALL ≥ 70 AND DCS ≥ 60`) · flagged · foreclosure opportunities · aggregate adjusted equity · average ROI of the top decile · new since last visit · changed in 7 days.

**Any tile that excludes properties says so** ("excludes 34 with no value data"). Silent exclusion from an aggregate is a lie by omission.

Table: virtualized, server-side sort/filter, sticky address + score columns, bulk actions (tag, set status, export, re-extract, watchlist).

### 11.3 Pipeline, notes, tags, reminders *(new)*
Per property: `status` (`new / reviewing / pursue / offer_made / under_contract / dead`), free-text notes with timestamps (markdown), arbitrary tags, `next_action_date` + one-line next action. A "Due today" tile on the dashboard. No CRM, no email, no automation — just enough that you never need a side spreadsheet.

### 11.4 Keyboard triage mode *(new)*
`j/k` move · `space` expand summary card · `enter` open deal page · `p` pursue · `x` dead · `f` flag · `t` tag · `1–5` gut rating (stored, and later used for calibration) · `?` help. Triaging 200 properties should take 15 minutes, not 90.

### 11.5 Compare view *(new)*
Select 2–4 properties → a column-per-property table of value, debt, equity, scores, best strategy, MAO, profit, ROI, confidence, and key risks, with the winning cell in each row highlighted. This is how the actual decision gets made.

### 11.6 Map view *(new, cheap)*
Leaflet + OpenStreetMap tiles, markers colored by score, sized by equity, clicking opens the deal card. Uses lat/lng already stored from geocoding. A few hours of work; catches geographic clusters and obviously wrong addresses.

### 11.7 Property deal page
1. **Executive summary card** — `Rank #7 of 2,413 · Est. value $1,200,000 · Confirmed debt $626,000 · Potential additional obligations $140,000 · Equity range $434,000–$574,000 · Distress 94/100 · Confidence 78/100 · Recommended: Direct cash acquisition / wholesale review`. Recommendation is deterministic: the highest-scoring viable strategy; near-ties (within 5 points) shown as "A / B."
2. **Scenario toggle** (Conservative | Expected | Optimistic) — re-renders from an already-loaded payload, no round trip.
3. **Financial breakdown** — value→debt→potential→equity waterfall, then tables for value candidates (with weights), debt, liens (with attachment badges), costs, and profit/ROI by strategy.
4. **Strategy tabs** with full calculations and `unavailable` reasons.
5. **Offer simulator** (§9.3).
6. **Distress timeline** — unified chronological feed of purchases, loans, NOD, NTS, postponements, rescissions, bankruptcies, liens, listings, each with date, amount, and a source link.
7. **Evidence drawer** — click any material number: resolved value, method, every competing value with its source and score, and pdf.js scrolled to the page with the snippet highlighted. Shows overrides ("you replaced this on Aug 3").
8. **Flags** — inline resolvable.
9. **History** — what changed on this property, when, why, and the score before/after.

---

## 12. Verification and flags (replaces the review-queue system)

A **flag** is a row on the property, not a ticket. Flags gate or penalize scores and are resolved inline from the deal page or from a filtered "Flagged" list sorted by financial impact.

| Flag | Trigger | Effect |
|---|---|---|
| `identity_conflict` | APN/address disagreement or fuzzy 0.80–0.92 | **Unranked until resolved** |
| `lien_attachment` | owner-only/unknown lien > $10,000 | Excluded from confirmed; RISK penalty |
| `conflicting_mortgage` | balances differ > 5% or $10k | DCS penalty |
| `foreclosure_unclear` | contradictory events, NTS without NOD | DCS penalty |
| `missing_lien_amount` | active lien, no amount | Median used, marked estimated |
| `valuation_dispersion` | max/min candidate ratio > 1.5 | Widens the band, lowers confidence |
| `missing_apn` | no APN after ingestion | DCS penalty |
| `low_extraction_confidence` | any critical field < 0.65 | DCS penalty |
| `bid_mismatch` | published bid vs. balance > 20% | Flag only |
| `range_violation` | value outside plausibility bounds | Fact inactive |

**Sorted by financial impact,** not age: `financial_impact_usd` = the equity delta between accepting and rejecting the disputed value. Resolving a $128k lien question before a missing bathroom count is the entire point.

**Resolution actions:** Approve (marks `human_verified`) · Reject (deactivates the fact, resolver re-runs) · Replace (creates a `source_kind='human'` fact at precedence 1.0) · Dismiss. Every resolution triggers `recompute_property` and the confirmation toast shows the score/rank delta — you should see your work move numbers.

**Verification states:** `unverified → corroborated (≥2 independent reports) → api_verified → human_verified`. Only the last two count toward `verification_rate` in the DCS.

**Confidence propagation:** derived values inherit the *minimum* confidence of their inputs × a derivation penalty (0.9 amortization, 0.85 weighted valuation, 0.8 regional defaults). Equity confidence is therefore bounded by mortgage-balance confidence — usually the weakest link — and the deal page says so in words.

---

## 13. AI analyst

Answers questions against the **normalized database**, never by re-reading PDFs. Three routes, chosen by a cheap classifier:

1. **Structured query** — *"show me the 20 foreclosure properties with at least $300k equity."* Text-to-SQL against a small **semantic layer** of curated read-only views (`v_property`, `v_liens`, `v_foreclosure`, `v_offers`) with plain-English column names. Guardrails, all cheap: a dedicated read-only Postgres role, `SELECT`-only check via `sqlglot`, forced `LIMIT 500`, 5-second statement timeout. The generated SQL is shown and editable, and one click turns the result into a saved view.
2. **Comparison** — *"why is 57 Cottage ranked above 42 Main?"* **Not** LLM reasoning: deterministic code diffs the two `components` blobs and ranks the terms by absolute delta; the LLM only phrases the top five. *"57 Cottage is 18 points higher on Financial Opportunity ($296k vs $71k expected profit) and 9 higher on Distress (NTS in 21 days vs none), partly offset by 6 more risk points from three additional liens."*
3. **Simulation** — *"what if I offered $780k?"* Parses the amount, calls the existing offer engine, narrates the returned numbers. Zero arithmetic in the model.

**Failures:** ambiguous property → asks, with candidates · zero rows → says so and shows the interpreted filters instead of inventing a story · invalid SQL after one repair → falls back to a pre-populated filter UI.

Cache `(question, data_version)` → answer for 15 minutes; you'll ask the same thing repeatedly.

---

## 14. Change detection

**Scope it tightly** — this is where teams burn money for little return. Only watchlisted properties (flagged by you, or auto-added at `OVERALL ≥ 70`) are tracked.

**Mechanism (free): re-ingested reports.** When a newer report arrives for an existing property, `detect_changes` diffs the newly resolved record against the previous one and writes `change_events`.

**Tracked:** new foreclosure notice · sale date moved · sale cancelled/rescinded · sale completed · new lien · lien released · new listing · price cut · ownership transfer · new bankruptcy · value shift > 10%.

**On change:** recompute → new score row → re-rank on the next run → the property appears in **What Changed**, a single page showing before/after values, what caused it, and the rank delta. A daily digest at most; per-event notifications create alert fatigue and get muted.

**Paid foreclosure/county feeds:** only worth adding if you're actively bidding at auctions, and then poll *only* watchlisted APNs, daily.

---

## 15. Database schema

Postgres 16. `id uuid pk`, `created_at`, `updated_at` on everything. Money `numeric(14,2)`, percentages `numeric(7,6)`. No `org_id`.

**`properties`** — `apn`, `apn_key`, `fips_county`, address components, `address_key`, `address_hash`, `lat`, `lng`, `property_type`, `beds`, `baths`, `sqft`, `lot_sqft`, `year_built`, `units`, `pipeline_status`, `tags text[]`, `next_action`, `next_action_date`, `gut_rating`, `is_watchlisted`, `merged_into_id`, `underwriting_status`, `last_recomputed_at`.

**`owners`** — `full_name`, `name_normalized`, `entity_type` (individual/trust/LLC/estate), mailing address, `is_absentee`, `phone`, `email`.

**`property_owners`** — `ownership_start/end_date`, `vesting`, `ownership_pct`, `is_current`, `acquired_via`.

**`batches`** — `name`, `tag`, `file_count`, `status`, `estimated_cost_usd`, `actual_cost_usd`, counters.

**`reports`** — `batch_id`, `property_id`, `report_type`, `vendor`, `generated_date`, `file_path`, `ocr_path`, `sha256`, `page_count`, `is_scanned`, `ocr_applied`, `duplicate_of`, `status`, `classification_confidence`.

**`extraction_units`** — `report_id`, `unit_type`, `page_start/end`, `text_path`, `token_estimate`, `status`, `model`, `prompt_version`, `cost_usd`.

**`extracted_facts`** *(append-only, the ledger)* — `property_id`, `report_id`, `extraction_unit_id`, `entity_type`, `entity_local_id`, `field_path`, `value_raw`, `value_parsed`, `value_text`, `value_date`, `value_bool`, `unit`, `as_of_date`, `page_number`, `snippet`, `extraction_confidence`, `null_reason`, `source_kind` (report/derived/human/api/pasted), `is_active`, `superseded_by`. Indexes: `(property_id, field_path)`, `(report_id)`.

**`field_resolutions`** — `property_id`, `field_path`, `winning_fact_id`, `method`, `score`, `candidate_fact_ids uuid[]`, `has_conflict`, `conflict_magnitude`, `verification_state`. Unique `(property_id, field_path)`.

**`mortgages`** — `position` (1/2/heloc/other), `lender_raw/normalized`, `original_amount`, `origination_date`, `recording_date`, `recording_doc_number`, `term_months`, `interest_rate`, `rate_type`, `estimated_balance`, `balance_method` (reported/amortized/derived), `balance_as_of`, `is_open`, `confidence`, `primary_fact_id`.

**`liens`** — `property_id`, `owner_id`, `lien_type`, `creditor_raw/normalized`, `amount`, `amount_is_estimated`, `recording_date`, `recording_doc_number`, `status`, **`attachment_basis`**, **`attachment_confidence`**, `attachment_verified_by/at`, `priority_position`, `confidence`, `primary_fact_id`. The three attachment columns are the heart of the schema.

**`foreclosure_events`** — `event_type` (nod/nts/postponement/rescission/sale/reinstatement/cancellation), `event_date`, `trustee_name`, `trustee_phone`, `trustee_sale_number`, `original_sale_date`, `current_sale_date`, `published_bid`, `default_amount`, `default_as_of`, `beneficiary`, `stage_after_event`, `confidence`.

**`bankruptcy_events`** — `owner_id`, `property_id`, `chapter`, `case_number`, `court`, `filing_date`, `status`, `discharge_date`, `filing_sequence`, `is_repeat`.

**`valuations`** — `valuation_type` (avm/comp/listing/assessed/manual), `value`, `value_low/high`, `confidence_reported`, `as_of_date`, `source_report_id`, `weight_applied`, `is_active`.

**`listings`** — `list_date`, `delist_date`, `list_price`, `final_price`, `status`, `dom`, `mls_number`.

**`comparable_sales`** — `property_id` (subject), `comp_address`, `sale_date`, `sale_price`, `sqft`, `beds`, `baths`, `distance_miles`, `price_per_sqft`, `similarity_score`, `included`, `exclusion_reason`.

**`assumption_sets`** — `name`, `is_default`, `params jsonb`, `version`, `effective_from`.

**`deal_scenarios`** — `property_id`, `strategy`, `scenario`, `assumption_set_id`, `engine_version`, `purchase_price`, `arv`, `repairs`, `holding`, `financing`, `resale`, `all_in_basis`, `profit`, `roi`, `margin_of_safety`, `cap_rate`, `cash_flow`, `coc`, `mao`, `status`, `unavailable_reason`, `computed_at`.

**`offer_scenarios`** — `property_id`, `offer_price`, `scenario`, `confirmed_payoffs`, `potential_payoffs`, `closing_costs`, `proceeds_low/expected/high`, `buyer_basis`, `profit`, `roi`, `is_short_sale`.

**`scores`** — `property_id`, `scoring_config_id`, `fos`, `distress`, `data_confidence`, `risk`, `overall`, `components jsonb`, `gates_applied text[]`, `computed_at`.

**`scoring_configs`** — `weights`, `bounds`, `distress_points`, `gates`, `version`, `is_active`.

**`rankings`** — `scope_type`, `scope_id`, `property_id`, `rank`, `prev_rank`, `score`, `ranked_at`.

**`flags`** — `property_id`, `flag_type`, `payload jsonb`, `financial_impact_usd`, `status`, `resolution`, `resolved_value jsonb`, `note`, `resolved_at`.

**`change_events`** — `property_id`, `change_type`, `field_path`, `old_value`, `new_value`, `source_report_id`, `score_delta`, `detected_at`.

**`property_notes`** — `property_id`, `body`, `created_at`.

**`realized_deals`** *(new, for calibration)* — `property_id`, `purchase_price`, `actual_repairs`, `actual_holding_days`, `sale_price`, `actual_costs`, `outcome`, `notes`, `closed_at`.

**`history`** — lightweight: `entity_type`, `entity_id`, `action`, `before jsonb`, `after jsonb`, `at`. Written on fact overrides, merges, assumption/config changes, and recomputes. Not an audit system — a "why did this move" system.

**Small config tables:** `document_signatures`, `lender_aliases`, `historical_rate_index`, `regional_cost_index`, `transfer_tax_rates`, `saved_views`, `jobs`.

---

## 16. API

REST/JSON at `/api`. Cursor pagination. Money fields return `{value, confidence, source_kind, is_estimated}` so the frontend can't accidentally render an estimate as a fact.

```
POST /uploads · POST /batches/{id}/estimate · POST /batches/{id}/start · GET /batches/{id}
GET  /properties (filters/sort/page) · GET /properties/{id} · GET /properties/{id}/analysis?scenario=
GET  /properties/{id}/evidence/{field_path} · GET /properties/{id}/timeline · GET /properties/{id}/reports
POST /properties/{id}/offers · POST /properties/{id}/recompute · POST /properties/{id}/facts
PATCH /properties/{id}   (status, tags, notes, next action, rating)
POST /properties/merge · POST /properties/unmerge · POST /properties/quick-add · POST /ingest/paste
GET  /rankings · GET /dashboard · GET /changes · GET /problems
GET/POST /saved-views · GET/POST /assumption-sets · POST /assumption-sets/preview
GET  /flags · POST /flags/{id}/resolve
POST /analyst/query
GET  /exports/csv · POST /properties/{id}/exports/deal-sheet · POST /properties/{id}/exports/net-sheet
POST /realized-deals · GET /calibration
```

---

## 17. Background jobs

One worker process, Postgres-backed queue, `SELECT ... FOR UPDATE SKIP LOCKED`.

| Job | Concurrency | Notes |
|---|---|---|
| `ingest_document` | 4 | hash, text, classify, section |
| `ocr_document` | 2 | 10-min timeout, partial fallback (RAM-heavy, keep it low) |
| `resolve_identity` | 4 | Postgres advisory lock on `apn_key`/`address_hash` prevents duplicate-property races |
| `extract_unit` | 12 | IO-bound; respects the budget check before each call |
| `recompute_property` | 8 | normalize → underwrite → score, one idempotent entry point |
| `rank_scope` | 1 | bulk SQL after each batch + nightly |
| `detect_changes` | 4 | on re-ingest |
| `nightly` | 1 | re-rank, refresh aggregates, backup, prune temp files |

**Critical:** `recompute_property(property_id, reason)` is the single re-entry point for everything downstream of the ledger, and it must be safe to run any number of times. Assumption or scoring changes enqueue it in chunks of 500 with visible progress.

---

## 18. Prompts and schemas

**System prompt (versioned in git, hashed into `prompt_version`):**
```
You are a document extraction engine for real estate records. You extract facts.
You do not analyze, estimate, calculate, or infer.

1. Return only data present in the text. If absent, return null with a null_reason.
   Never estimate, never fill from typical values.
2. Every object needs page_number and a verbatim snippet (<=200 chars) copied exactly
   from the text. Snippets are verified automatically; a fabricated snippet voids the object.
3. Do not do arithmetic. No sums, no balances, no conversions. Give value_raw exactly as
   written plus a mechanical numeric parse.
4. For liens/judgments set attachment_basis to "recorded_against_property" ONLY IF the text
   ties the instrument to this parcel by APN, legal description, parcel recording reference,
   or the subject address. If it only names a person: "owner_named_only". If unclear:
   "unknown". This has financial consequences; when in doubt use "unknown".
5. If a field appears with different values, return each occurrence separately. Do not choose.
6. extraction_confidence reflects legibility only, not your belief about correctness.

Subject: {address} | APN: {apn} | Doc: {report_type} | Pages {p_start}-{p_end}
```

**Lien unit schema (each unit type has its own, same shape):**
```json
{
  "type":"object","required":["liens"],"additionalProperties":false,
  "properties":{"liens":{"type":"array","items":{
    "type":"object","additionalProperties":false,
    "required":["lien_type","attachment_basis","page_number","snippet","extraction_confidence"],
    "properties":{
      "lien_type":{"enum":["federal_tax","state_tax","judgment","hoa","mechanics","property_tax","child_support","ucc","other","unknown"]},
      "creditor_raw":{"type":["string","null"]},
      "debtor_name_raw":{"type":["string","null"]},
      "amount_raw":{"type":["string","null"]},
      "amount_parsed":{"type":["number","null"]},
      "recording_date":{"type":["string","null"],"format":"date"},
      "recording_doc_number":{"type":["string","null"]},
      "status":{"enum":["active","released","satisfied","expired","unknown"]},
      "attachment_basis":{"enum":["recorded_against_property","owner_named_only","unknown"]},
      "attachment_evidence":{"type":["string","null"]},
      "attachment_confidence":{"type":"number","minimum":0,"maximum":1},
      "page_number":{"type":"integer","minimum":1},
      "snippet":{"type":"string","maxLength":200},
      "extraction_confidence":{"type":"number","minimum":0,"maximum":1},
      "null_reason":{"enum":["not_present","illegible","redacted","conflicting_in_source",null]}
    }}}}
}
```
Other units: `property_core`, `ownership`, `mortgages`, `foreclosure`, `bankruptcy`, `valuation`, `comparables`, `listings`, `tax`, `rental`, `condition_signals` (enums only, no dollars).

**Evaluation:** a 40-document gold set (mixed vendors, scanned and digital, plus the nasty cases: releases, rescissions, trusts, multiple owners, partial APNs). `make eval` prints per-field accuracy and grounding-failure rate. Run it before adopting a prompt or model change. That's the whole harness — no CI gates, no dashboards.

---

## 19. Cost control

1. **Pre-flight estimate** *(new).* After classification and sectioning — which are free — show token counts, per-model split, dollar estimate, and time estimate. Nothing hits the API until you confirm. This is the single most valuable addition for a self-funded operator.
2. **Hard budget.** Per-batch and monthly caps stored in settings; the worker checks before every call. Hitting the cap **pauses** the batch and asks, rather than failing or overspending.
3. **Live cost meter** on the batch screen and a lifetime total in settings.
4. **Free levers, already built in:** file-hash dedupe · sectioning · cheap-model routing for low-ambiguity units · skipping unchanged documents · prompt caching of the shared system prompt · batch API for overnight runs (typically ~50% cheaper — a checkbox, worth having).
5. **Re-extraction from cached page text** *(new).* Improved a prompt? Re-run any subset from the stored page text — no re-upload, no re-OCR, no re-classification. Facts get a new `prompt_version`; old ones are superseded, not deleted, so you can compare.

**Rough unit economics** (verify against current pricing): a 25-page combined report ≈ 30k input / 6k output tokens after sectioning, ~$0.10–0.18 with model routing. 400 documents ≈ $40–70.

---

## 20. Backups, exports, portability

- **Nightly:** `pg_dump` + `restic` snapshot of `./documents` to B2/S3, encrypted, 30 daily + 12 monthly retained. A `make restore` script that has actually been tested. For a private tool this matters more than any security control.
- **Full export:** one command produces `properties.csv`, `liens.csv`, `mortgages.csv`, `scores.csv`, `facts.jsonl` plus the documents folder. No lock-in, and it doubles as your analysis escape hatch into a spreadsheet.
- **List export:** filtered CSV of the current view, with a column selector — for mailing lists, skip-trace vendors, or your partner's spreadsheet.

---

## 21. Security (proportionate)

- Single password (argon2) + TOTP, or put Cloudflare Access / Tailscale in front and skip app auth entirely. A second user account with a `read_only` boolean is the entire permission model.
- HTTPS via Caddy automatic TLS if exposed; otherwise localhost-only.
- Documents served through the app after an auth check, never from a public directory.
- Disk encryption on the host; encrypted backups.
- **Practical/legal note kept in the product:** this data is for property acquisition analysis, not for credit, insurance, employment, or tenant screening decisions, and outreach to homeowners in foreclosure is regulated in many states. The tool sends no communications.

That's it. No SOC 2, no pen tests, no RBAC matrix.

---

## 22. Calibration loop *(new — the only real long-term edge)*

When a deal closes (or dies), record actuals in `realized_deals`: purchase price, actual repairs, actual days held, sale price, actual costs.

The **Calibration page** then shows:
- Predicted vs. actual repairs, as a scatter and a single correction factor per condition level → suggested new $/sqft defaults.
- Predicted vs. actual sale price → suggested reweighting of value candidates (e.g., "your comps estimate beats the vendor AVM by 3.1% on average; raise its weight").
- Predicted vs. actual holding period.
- Your gut rating (`1–5` from triage) vs. the Overall Score → tells you whether the model is capturing what you actually care about.

Suggestions are **proposals**, applied only when you accept them, and they create a new `assumption_set` version. Ten closed deals will make these defaults better than any vendor's. It's a few hundred lines of code and one page.

---

## 23. Scope

**MVP (~6–8 weeks for one competent full-stack dev, ~10–12 part-time)**
Upload + watched folder · dedupe · digital text extraction (OCR behind a flag) · rules-based classification + LLM fallback · identity resolution · sectioned extraction with grounding validation · fact ledger + resolver · financial engine with three scenarios · **cash and fix-and-flip only** · seller proceeds + offer grid · four scores + overall · ranking, filters, saved views · portfolio table with keyboard triage · deal page with evidence drawer and offer slider · flags with inline resolution · notes/tags/status · CSV export · cost estimate + budget · backups.

*Not in MVP:* AI analyst, wholesale/rental/foreclosure/subject-to, change detection, compare view, map, deal sheet PDFs, calibration.

**Next (in this order, each ~1 week)**
1. Deal sheet + net sheet PDFs (immediate practical value)
2. Remaining strategies (wholesale, rental, foreclosure, subject-to flagging)
3. AI analyst (all three routes — cheap now that the schema exists, high perceived value)
4. Compare view + map
5. Change detection via re-ingest + What Changed page
6. Assumption sandbox with preview
7. Calibration loop
8. County assessor enrichment for your two or three main counties; on-demand property-data API for shortlisted properties only

**Probably never (for this use case):** MLS, title API, permit data, webhooks, public API, mobile apps, multi-tenancy, SOC 2, scheduled email reports.

---

## 24. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Lien attachment misclassification** | **Severe** — wrong equity, wrong offer | Three-state field, excluded from confirmed by default, mandatory flag >$10k, gold-set coverage of ambiguous liens, title order before closing |
| Vendor template drift silently degrading extraction | High | Section match-rate on the Problems page, page-chunk fallback, spot-check 10 documents per batch |
| Mortgage balance estimation dominating equity error | High | Prefer published trustee bid, propagate low confidence, widen bands, never show derived balances as recorded |
| Bad property merges | High | APN-first, reversible, conflict flags gate ranking, never hard-delete |
| Trusting scores without opening evidence | High | Confidence gates, forced ranges, a two-field verify prompt before a property can be marked `pursue` |
| Garbage AVMs anchoring value | High | Multi-source weighting, dispersion penalty, confidence hit when there's only one source |
| API cost surprise | Medium | Pre-flight estimate, hard budget, live meter |
| Text-to-SQL plausible-but-wrong answers | Medium | Semantic views, SQL shown, read-only role, LIMIT + timeout; comparisons handled deterministically |
| OCR quality on bad scans | Medium | Confidence caps, flags, DCS gate |
| Single machine dies | Medium | Tested nightly backups; the whole system is `docker compose up` + a restore |

---

## 25. Build order

Each step ends with something you can use. Don't reorder 1–6 — everything depends on the ledger being right.

1. Schema + migrations + the `jobs` table.
2. Upload → disk → dedupe → PyMuPDF text → per-page text files. *Demo: 50 PDFs, readable page text.*
3. Classification (rules first) + sectioning. *Demo: a 32-page report split into 19 typed units.*
4. Identity resolution + merge/unmerge + conflict flags. *Demo: 200 PDFs collapse into 63 correct properties.*
5. LLM extraction + schema validation + grounding check + fact ledger. **Build the 40-document gold set here, before tuning anything.**
6. Resolver + dedupe + derived balances + conflict detection. *Demo: one normalized record with competing values visible.*
7. Financial engine, with unit tests written from hand-computed fixtures. *Demo: equity ranges.*
8. Cash + flip + seller proceeds grid. *Demo: MAO and a proceeds table.*
9. Scoring + ranking. *Demo: 200 properties ranked.*
10. Portfolio table + filters + saved views + keyboard triage. *Demo: the actual daily workflow.*
11. Deal page + evidence drawer + scenario toggle + offer slider.
12. Flags + inline resolution + recompute. *Demo: resolving a lien question moves a property 14 ranks.*
13. Notes/tags/status/next action, Problems page, cost estimate + budget, backups. **Ship — start using it daily.**
14. Then work the "Next" list in §23, reordered by whatever annoyed you most in the first two weeks of real use.

---

## 26. The two rules, restated

1. **The LLM extracts; deterministic code calculates.** No dollar figure you see originates in a language model. Every one is reproducible from stored inputs, an assumption version, and an engine version.
2. **Uncertainty is displayed, not resolved.** Missing data is `null` with a reason. Conflicting data is kept and shown. A person-level lien is never a property lien until something recorded says so. Low confidence caps the score rather than hiding behind it.

Everything else is implementation detail. Those two rules are the product.
