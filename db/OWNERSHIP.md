# Table ownership and migration ranges

Each package may write only to its assigned tables. All packages may read shared tables.

| Package | Tables | Alembic revisions |
|---|---|---:|
| WP-1 ingestion | batches, reports | 100–119 |
| WP-2 classification | extraction_units, document_signatures | 120–139 |
| WP-3 identity | properties identity, property_owners, owners, identity_merge_report_moves | 140–159 |
| WP-4 extraction | extracted_facts | 160–179 |
| WP-5 normalization | field_resolutions, mortgages, liens, foreclosure_events, bankruptcy_events, valuations, listings, comparable_sales | 180–219 |
| WP-6/7 finance | assumption_sets, deal_scenarios, offer_scenarios | 220–239 |
| WP-8 scoring | scores, scoring_configs, rankings | 240–259 |
| WP-9 flags | flags | 260–269 |
| WP-11 API | saved_views, property_notes | 270–289 |
| WP-16 changes | change_events | 290–299 |
| WP-17 calibration | realized_deals | 300–309 |
| WP-18 ops | settings, history | 310–319 |

The initial schema migration is owned by WP-0. No package may edit another package's tables or reuse its revision range.

The cross-cutting schema-completion migration `0002_schema_items` (config tables, versioning columns, uniqueness constraints) is owned by WP-0 and sits outside the per-package ranges.

`identity_merge_report_moves` is owned by WP-3 identity and is mapped in `identity/models.py` (`MergeReportMove`) on the shared Base — `db/models.py` deliberately does not duplicate that mapping. Its table is created by the WP-0 migration `0003_identity_merge_moves` (kept outside the 140–159 range so the migration chain stays linear).
