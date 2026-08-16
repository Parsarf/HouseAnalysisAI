# ACQ Build Status

| Package | Status | Evidence | Blocker |
|---|---|---|---|
| WP-0 foundation | RUNNING | canonical contracts, migration wiring, CI config | dependencies/Postgres not executed |
| WP-1 ingestion | RUNNING | streamed upload, PDF storage, page accessors | OCR, fixtures, full failure tests |
| WP-2 classification | RUNNING | basic rules and section unit type | DB signatures, tokenizer, fixtures |
| WP-3 identity | RUNNING | APN/address normalization and advisory-lock path | concurrency/database tests |
| WP-4 extraction | RUNNING | prompt/version/replay/grounding foundation | provider client, schemas, gold set |
| WP-5 normalization | BLOCKED | minimal resolver only | full fact fixtures and entity resolver |
| WP-6/7/8 numeric | RUNNING | deterministic underwriting/strategies/scoring core | exact fixtures and formula audit |
| WP-9 flags | RUNNING | flag contracts, persistence schema, collectors | resolution workflow |
| WP-10 pipeline | RUNNING | queue worker and computation composition | full fan-in/persistence |
| WP-11 API | RUNNING | auth, upload, batch, property endpoints | full endpoint contract |
| WP-12 frontend | RUNNING | portfolio shell/upload/list | full portfolio workflow |
| WP-13 deal page | BLOCKED | not implemented | WP-11 analysis payload |
| WP-14 analyst | BLOCKED | package boundary only | post-MVP |
| WP-15 exports | RUNNING | streaming CSV | PDF/full export |
| WP-16 changes | BLOCKED | basic diff helper | post-MVP |
| WP-17 calibration | BLOCKED | package boundary only | post-MVP |
| WP-18 ops | RUNNING | budget/backup scripts | restore and deployment tests |

## Fixture status

`HUMAN_FIXTURE_INPUT_REQUIRED`: authoritative anonymized PDFs, labels, and reviewed recorded model responses are not present. Synthetic numeric fixtures can be added independently; Gates A and B require owner-supplied source material.
