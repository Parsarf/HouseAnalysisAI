"""Versioned multi-entity PDF analysis; legacy records are retained."""
from alembic import op

revision = "0010_document_entities"
down_revision = "0009_owner_chat_archive_outreach"
branch_labels = None
depends_on = None

DDL = """
CREATE TABLE document_analysis_runs (
 id uuid PRIMARY KEY, report_id uuid NOT NULL REFERENCES reports(id), budget_batch_id uuid REFERENCES batches(id), generation integer NOT NULL,
 version varchar(40) NOT NULL, status varchar(30) NOT NULL, page_count integer NOT NULL DEFAULT 0,
 coverage jsonb NOT NULL DEFAULT '[]', issues jsonb NOT NULL DEFAULT '[]',
 cost_usd numeric(14,6) NOT NULL DEFAULT 0, created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(report_id, generation)
);
CREATE INDEX ix_document_analysis_runs_report_id ON document_analysis_runs(report_id);
CREATE TABLE document_analysis_chunks (
 id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES document_analysis_runs(id), key varchar(255) NOT NULL,
 pages jsonb NOT NULL DEFAULT '[]', status varchar(30) NOT NULL, payload jsonb, error text,
 cost_usd numeric(14,6) NOT NULL DEFAULT 0, attempts integer NOT NULL DEFAULT 0, UNIQUE(run_id, key)
);
CREATE INDEX ix_document_analysis_chunks_run_id ON document_analysis_chunks(run_id);
CREATE TABLE report_entity_extractions (
 id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES document_analysis_runs(id),
 report_id uuid NOT NULL REFERENCES reports(id), entity_key varchar(255) NOT NULL,
 kind varchar(20) NOT NULL, role varchar(20) NOT NULL,
 property_id uuid REFERENCES properties(id), owner_id uuid REFERENCES owners(id),
 source_pages jsonb NOT NULL DEFAULT '[]', raw_json jsonb, normalized_json jsonb,
 issues jsonb NOT NULL DEFAULT '[]', status varchar(30) NOT NULL, active boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(run_id, entity_key)
);
CREATE INDEX ix_report_entity_extractions_run_id ON report_entity_extractions(run_id);
CREATE INDEX ix_report_entity_extractions_report_id ON report_entity_extractions(report_id);
CREATE INDEX ix_report_entity_extractions_property_id ON report_entity_extractions(property_id);
CREATE INDEX ix_report_entity_extractions_owner_id ON report_entity_extractions(owner_id);
"""


def upgrade():
    op.execute(DDL)
    # A legacy extraction is a versioned, usable baseline, not a new provider request.
    op.execute("""
    INSERT INTO document_analysis_runs
      (id, report_id, generation, version, status, page_count)
    SELECT id, report_id, 0, 'legacy-v1', status, 0 FROM report_extractions;
    INSERT INTO report_entity_extractions
      (id, run_id, report_id, entity_key, kind, role, property_id, raw_json,
       normalized_json, status, active)
    SELECT id, id, report_id, 'legacy', 'property', 'subject', property_id, raw_json,
      normalized_json, status, status = 'complete'
    FROM report_extractions WHERE property_id IS NOT NULL;
    """)


def downgrade():
    op.execute("DROP TABLE report_entity_extractions; DROP TABLE document_analysis_chunks; DROP TABLE document_analysis_runs;")
