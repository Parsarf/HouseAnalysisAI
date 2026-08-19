# ACQ Build Status (2026-08-19)

The API, worker, whole-PDF analysis path, deterministic engines, flags
aggregation, migrations, and SPA are implemented. Remaining work is primarily
environment-dependent validation, CI typing debt, and human fixture review.

| Package | Status | Evidence | Blocker |
|---|---|---|---|
| WP-0 foundation | RUNNING | canonical contracts, migration wiring, CI config | dependencies/Postgres not executed |
| WP-1 ingestion | REVIEW | streamed upload, PDF storage, OCR, failure codes, idempotent unit creation | human PDFs; Postgres end-to-end |
| WP-2 classification | REVIEW | DB-backed signatures, confidence, typed section units, match rate | human document fixture coverage |
| WP-3 identity | RUNNING | APN/address normalization and advisory-lock path | concurrency/database tests |
| WP-4 extraction | REVIEW | provider client, schemas, retries, replay, grounding, persistence and budget gate | live provider credentials; reviewed gold responses |
| WP-5 normalization | REVIEW | fact precedence, entity resolution, conflicts, derived balances and data quality | broader source fixture coverage |
| WP-6/7/8 numeric | REVIEW | deterministic underwriting/strategies/scoring/ranking core | score golden schema reconciliation |
| WP-9 flags | RUNNING | flag contracts, persistence schema, collectors | resolution workflow |
| WP-10 pipeline | REVIEW | queue worker, classification→extraction→fan-in, persistence, recompute and ranking | Postgres end-to-end |
| WP-11 API | REVIEW | auth, upload, batch, property, analysis, flags, notes, exports and filters | full Postgres endpoint contract |
| WP-12 frontend | REVIEW | portfolio shell, upload/list, filters, deal navigation and shared API layer | authenticated browser QA |
| WP-13 deal page | REVIEW | analysis payload, evidence, timeline, offer simulator and deal sheet UI | authenticated browser QA |
| WP-14 analyst | BLOCKED | package boundary only | post-MVP |
| WP-15 exports | RUNNING | streaming CSV | PDF/full export |
| WP-16 changes | BLOCKED | basic diff helper | post-MVP |
| WP-17 calibration | BLOCKED | package boundary only | post-MVP |
| WP-18 ops | RUNNING | budget/backup scripts | restore and deployment tests |

## Fixture status

`HUMAN_FIXTURE_INPUT_REQUIRED`: authoritative anonymized PDFs, labels, and reviewed recorded model responses are not present. Synthetic numeric fixtures can be added independently; Gates A and B require owner-supplied source material.

## Latest checkpoint

Commit `42dcfbb` wires ingestion classification to persisted extraction units and
Postgres jobs, and configures the worker to call the provider-backed extraction
adapter without double-writing facts. Validation: 424 tests passed (excluding
12 stale score-golden comparisons), frontend production build passed. The
score-golden mismatch is retained as an explicit review blocker rather than
silently replacing the independent fixtures.
