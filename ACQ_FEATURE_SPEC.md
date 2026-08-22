# ACQ — Owner Intelligence, Chat, Archive & Outreach

Feature spec and implementation prompt. Written against branch `agent/build-complete-acq-ui`
at tip `18b20ca`.

---

## 1. Decisions locked

| Decision | Choice |
|---|---|
| Chat data access | Full — structured record always in context, documents via retrieval tool |
| Delete semantics | Archive (reversible, data retained) |
| Owner liens/bankruptcies | **Reference only** — displayed, never fed to scoring/underwriting/offer grid |
| Multiple emails | Show all candidates, user picks before Gmail opens |
| Email tone | Cash offer only — no mention of foreclosure, auction, or distress |

---

## 2. What already exists — reuse, do not rebuild

This feature needs far less new code than it appears. Before writing anything, read these:

| Need | Already exists |
|---|---|
| LLM calls with retry/timeout/cost | `report_analysis/provider.py` |
| Spend caps and pausing | `ops/budget.py`, `ops/db_budget.py` |
| Owner records, mailing address | `db/models.py:214` `Owner` |
| Property↔owner join | `db/models.py:228` `PropertyOwner` |
| Person liens w/ attachment probability | `db/models.py:317` `Lien` — has `owner_id`, `attachment_basis`, `attachment_confidence` |
| Bankruptcy w/ repeat-filing fields | `db/models.py:358` `BankruptcyEvent` — has `filing_sequence`, `is_repeat` (currently unpopulated) |
| Address/APN/owner matching + merge review | `identity/service.py` |
| Cross-property comparison math | `analyst/comparison.py`, `analyst/compset.py` |
| Document text extraction | `ingestion/`, `report_analysis/` |
| Routing (2 routes, no react-router) | `web/src/router.tsx` |

**New schema is limited to:** an `owner_contacts` table (multi-valued phones/emails with a source
and confidence), `properties.archived_at`, and a `documents.doc_kind` discriminator. Everything
else maps onto existing tables.

---

## 3. Feature: owner document ingestion and linking

### 3.1 The two document types

`57_Cottage_AV_07_07_2` (Property Profile) and `57_Cottage_AV_owner_s` (Owner/skip-trace) are the
two shapes. They must be distinguished at ingest and routed to different extraction schemas.

**Classification signal:** the property profile contains an APN, a subject address, and a
"Property Details" / "Tax Assessment" block. The owner document leads with a person name, a
"Person Type" / "Ownership Role" block, and has no subject-property address at all. Classify on
these structural markers, not on the filename.

Add `doc_kind` to the document/report record: `"property_profile" | "owner_profile"`.

### 3.2 The join problem

**The owner document contains no property address and no APN.** For the sample pair, the only
overlapping fields are:

- Owner name — `MARLENE C LEWIS` vs `LEWIS,MARLENE C` (order reversed)
- Mailing address — `2549 EASTBLUFF DR # 279, NEWPORT BEACH, CA 92660` (exact)

The mailing address is *not* the subject property (subject is in Aliso Viejo; owner resides in
Newport Beach). Joining on property address is impossible; the join is on **owner identity**.

**Linking rules — route all of this through `identity/service.py`, do not build a second matcher:**

| Evidence | Action |
|---|---|
| Normalized name match **and** exact normalized mailing address match | Candidate link, high confidence → still surfaces in merge review for one-click confirm |
| Name match + filename hint agreement | Candidate link, moderate confidence → merge review |
| Name match only | Candidate link, low confidence → merge review, never auto-confirm |
| No name match | No link; owner doc lands in an unlinked queue |

Name normalization must handle `LAST,FIRST MIDDLE` ↔ `FIRST MIDDLE LAST` and middle-initial
presence/absence. Filename is a *tiebreaker only* — it is user-controlled and must never be a
primary key.

An owner may own multiple properties (`PropertyOwner` is already many-to-many). Linking an owner
document attaches to the **owner**, and therefore surfaces on every property that owner holds.

### 3.3 What to extract from the owner document

Map onto existing tables:

- **Person** → `Owner` (name, age, gender, mailing address). Age/gender are new optional columns
  or go in a JSON detail column — do not create a parallel person table.
- **Phones and emails** → new `owner_contacts` rows: `(owner_id, kind, value, rank, source,
  confidence)`. The sample has 3 phones and 3 emails. These are skip-trace outputs of varying
  reliability — `emanuellewis@hotmail.com` on a record for *Marlene* Lewis is very likely a
  relative or stale association. Never collapse to a single "the" email.
- **Bankruptcy cases** → `BankruptcyEvent` with `owner_id`, `chapter`, `case_number`,
  `filing_date`, `status`. **Populate `filing_sequence` and `is_repeat`** — these columns exist and
  are exactly what this data is for.
- **Involuntary person liens** → `Lien` with `owner_id` set and `property_id` NULL,
  `attachment_basis = "owner_named_only"`.

### 3.4 The serial-filing signal (display only)

The sample owner has **five Chapter 13 filings, all dismissed**, each interleaved with a
foreclosure action:

| Foreclosure | BK filed | Outcome |
|---|---|---|
| NOD 2/6/2013 → NTS 5/30/2013 | 7/1/2013 | Dismissed |
| NOD 1/2/2018 → NTS 3/30/2018 | 5/10/2018 | Dismissed |
| NOD 5/6/2022 → NTS 11/18/2022 | 12/26/2022 | Dismissed |
| NTS 8/2/2024 | 9/11/2024 | Dismissed |
| NTS 2/3/2026, sale set **3/19/2026** | **3/19/2026** | Dismissed |

The 2026 petition was filed the same day as the scheduled trustee's sale.

Compute a **serial-filing indicator** on the deal page: count of dismissed filings, and whether
any filing date falls within N days before a scheduled sale date (default 7). Render it as a
prominent flag with the interleaved timeline above.

Per the locked decision this is **reference only** — it must not alter scores, strategy viability,
holding-period assumptions, or the offer grid. Add a test asserting that populating bankruptcy
events changes no scoring output.

### 3.5 Person-lien equity impact (display only)

The sample carries an open federal tax lien of **$140,294** (recorded 5/5/2026). Property-profile
equity reads $572,118 ($1,198,501 − $626,383). If that lien attaches, equity is **$431,824** — a
24% reduction.

Render on the deal page as a flag: *"Owner-level lien not included in underwriting — equity if
attached: $431,824."* Compute it in the serializer, display it beside the engine equity figure,
and keep it out of `finance/engine.py` entirely. Test that `potential_weighted` is unchanged by
the presence of `owner_named_only` liens with NULL `property_id`.

### 3.6 Extraction bugs this pair exposes

- **Lot size**: the profile reads `Lot Sq Ft 407,747 / Lot Acres 9.36` for a condo — that is the
  whole HOA parcel. Do not populate unit lot size from it on condo (`CND`) property types, or
  per-square-foot logic breaks.
- **APN collision**: the legal description contains `AP 639-062-15` while the actual APN is
  `931-762-13`. The parser must not read a parcel number out of the legal description block.
- **Cancelled listing**: MLS cancelled 5/20/2026 at $999,000 against a $1,198,501 AVM. Capture it
  in `Listing` — a cancelled listing well below AVM is real evidence about both motivation and
  value, and the chat should be able to cite it.

---

## 4. Feature: property chat

### 4.1 Architecture

One endpoint, `POST /api/chat`, streaming. Two context tiers:

**Always in context (small, cheap):**
the normalized record, underwriting result, equity by scenario, scores, strategy results with
metrics, flags, owner summary, and — when the user is on a property — that property's identifiers.
This is a few KB and covers most questions without touching a PDF.

**Retrieved on demand (tools the model calls):**

| Tool | Purpose |
|---|---|
| `get_document_text(report_id, page_range)` | Pull raw passages when asked what a document actually says |
| `list_documents(property_id)` | Enumerate available docs and kinds |
| `compare_properties(property_ids[], scenario)` | **Calls `analyst/comparison.py`** — never let the model do comparison math |
| `search_portfolio(filters)` | Find properties by criteria for "which of my properties…" questions |
| `get_owner_profile(property_id)` | Owner, contacts, liens, bankruptcies |

### 4.2 The hard rule

**Every number the model states must come from the structured record or a tool result. The model
never computes.** If the chat recomputes a spread from PDF text and it disagrees with
`strategies/engine.py`, both become untrustworthy. Put this in the system prompt explicitly, and
instruct the model to cite the source (field name or document page) for any figure it gives.

### 4.3 Cost control — required before ship

This is the first **unbounded, user-driven** LLM cost surface in the system. Everything else is
one bounded call per report.

- Per-session token cap and per-day spend cap, enforced through `ops/budget.py`.
- Conversation history trimmed to a fixed window; do not resend full documents each turn.
- Cache retrieved document text within a session so repeat questions don't re-fetch.
- Log cost per turn the way report analysis already does.

### 4.4 UI

New route `/chat` in `web/src/router.tsx` (the file's own comment anticipates growth; keep the
`Link`/`usePath`/`navigate` surface). Plus an entry point from the deal page — a "Ask about this
property" button that opens chat with that property pre-selected.

Property selector: multi-select chips from the portfolio, so comparison questions are unambiguous.
Chat with zero properties selected is allowed — portfolio-wide questions route through
`search_portfolio`.

Follow the existing visual language in `web/src/style.css` and reuse `Money`, `ConfidenceBadge`,
and `ScoreBar` when rendering figures in responses. This is an added surface in a coherent
application, not a place for a new visual identity.

---

## 5. Feature: archive

`properties.archived_at timestamptz NULL`. Migration 0009.

- Filtered out of: portfolio queries, CSV/full exports, scoring runs, ranking, calibration sets,
  and portfolio aggregates.
- Archive action on the portfolio page row, with an undo toast. Confirm dialog names the property.
- Archived properties remain reachable by direct URL, with an archived banner and a restore action.
- A "Show archived" filter toggle in `FilterBar`.

**Identity edge case — decide explicitly in `identity/service.py`:** when a new report resolves to
an archived property, **auto-restore it and record a `ChangeEvent`** ("restored — new report
received"). Do not silently create a duplicate, and do not leave it hidden while new data lands.
Test both branches.

---

## 6. Feature: email draft generator

### 6.1 Entry points

From chat (*"draft an email for this property"*) and a button on the deal page. Both call the same
endpoint, `POST /api/properties/{id}/outreach-draft`.

### 6.2 Content rules — non-negotiable

Per the locked decision, the draft **leads with a cash offer and does not mention foreclosure,
auction, default, bankruptcy, or financial distress in any form.** Put this in the system prompt as
a hard constraint and add a validation pass over the generated text that rejects drafts containing
distress vocabulary, regenerating once before surfacing an error.

What the draft *should* be built from — the credible, specific material:

- A concrete number and how it was reached (the offer grid already produces this)
- Certainty and speed of close — cash, no financing contingency, flexible timing
- Correct property identification (address, unit type, beds/baths)
- A clear, low-pressure exit

Specificity is what converts here, not pressure. An offer that demonstrates you have actually
underwritten the asset outperforms a persuasion template, and it is the version that survives
scrutiny later.

### 6.3 Recipient selection

Render **all** candidate emails with their source and confidence, unselected by default. The user
picks before anything opens. Never auto-select. Where the name on an address diverges from the
owner name, label it (*"may be a relative or stale association"*).

If no email exists: produce the draft anyway, offer copy-to-clipboard, and show the mailing address
for physical outreach.

### 6.4 The Gmail hand-off

Use the Gmail compose URL, not `mailto:` — `mailto:` truncates past ~2,000 characters in most
browsers and hands off to whatever the OS default client is:

```
https://mail.google.com/mail/?view=cm&fs=1&to={to}&su={subject}&body={body}
```

All parameters `encodeURIComponent`-escaped. Provide a `mailto:` fallback link and a copy button.
Body is plain text — compose URLs do not carry HTML.

### 6.5 Editing

The draft returns as editable text with a revision affordance: the user can edit inline, or ask for
changes in chat (*"make it shorter," "raise the offer to 950k," "less formal"*). Revisions go
through the same endpoint with the prior draft and the instruction, so the offer figure stays
traceable to the offer grid rather than drifting freely.

Persist drafts against the property (reuse `PropertyNote` or a small `OutreachDraft` table) so
there is a record of what was sent and when.

### 6.6 Compliance surface

Surface **owner-occupancy** prominently on the deal page. It is `Owner Occupied: No` in the sample,
which places the property outside portions of California Civil Code §1695 (owner-occupied
residences in foreclosure). This materially affects what outreach is permitted, so it should be a
visible flag rather than a buried field.

This spec is not legal advice. Have counsel review the outreach template against CC §§1695 and
2945 before first send, and add a configurable disclosure block to the template so a lawyer's
required language can be inserted without a code change.

---

## 7. Data protection

The owner documents introduce personal data about people who have not contacted you — names,
ages, phone numbers, email addresses, bankruptcy history, liens.

- Owner contact data is **excluded from CSV and full exports by default**; if included, it must be
  an explicit opt-in flag on the export request.
- `get_owner_profile` is the only path by which contact data enters LLM context, so it is not sent
  on turns that don't need it.
- Archived and purged properties must purge associated contact data.
- Access to owner contact endpoints respects the existing `read_only` role.

---

## 8. Build order

1. `doc_kind` classification + owner-document extraction schema → `Owner`, `owner_contacts`,
   `Lien(owner_id)`, `BankruptcyEvent`
2. Identity linking through the existing merge-review flow + auto-restore branch
3. Archive (migration 0009, filters, UI, restore)
4. Deal-page display: serial-filing timeline, lien equity-impact flag, owner-occupancy flag,
   contact panel
5. Chat endpoint + tools + budget caps
6. Chat UI and property selector
7. Email draft generation, recipient picker, Gmail hand-off, revision loop

Steps 1–4 deliver value with no LLM cost and no new failure modes. Step 5 is where the cost
controls must already exist.

---

## 9. Implementation prompt

> Implement the feature set in `ACQ_FEATURE_SPEC.md` on branch `agent/build-complete-acq-ui`
> (tip `18b20ca`), in the build order in §8, one reviewable commit per numbered step.
>
> Before writing code, read `identity/service.py`, `report_analysis/provider.py`, `ops/budget.py`,
> `analyst/comparison.py`, `db/models.py` (Owner, PropertyOwner, Lien, BankruptcyEvent), and
> `web/src/router.tsx`. This feature reuses far more than it adds — new schema is limited to
> `owner_contacts`, `properties.archived_at`, and `documents.doc_kind`.
>
> Hard constraints:
> - Owner liens and bankruptcies are **display only**. They must not affect scoring, underwriting,
>   strategy viability, or the offer grid. Add tests asserting this.
> - Chat must never compute figures. Numbers come from the structured record or tool results, and
>   comparisons route through `analyst/comparison.py`.
> - Chat cost is capped per session and per day through `ops/budget.py` before the endpoint ships.
> - Email drafts must not reference foreclosure, auction, default, bankruptcy, or distress. Validate
>   generated text against a distress-vocabulary denylist.
> - Owner contact data is excluded from exports by default and enters LLM context only via
>   `get_owner_profile`.
> - The owner→property join goes through `identity/service.py` merge review. Filename is a
>   tiebreaker, never a key. Name-only matches never auto-confirm.
>
> Also fix the extraction issues in §3.6: condo lot size, APN-from-legal-description, and cancelled
> listing capture.
>
> Validation gate — all must pass: `pytest` (0 failures), `ruff check`, `lint-imports`,
> `make typecheck` (0 errors), `cd web && npx tsc --noEmit && npm run build`, and
> `alembic upgrade head && alembic check` from an empty database.
>
> Report per-step status with `file:line` evidence, the exact pytest line verbatim including any
> failures, and the exact mypy total.
