"""Canonical whole-PDF report extractions."""

from alembic import op

revision = "0004_report_extractions"
down_revision = "0003_identity_merge_moves"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE IF NOT EXISTS report_extractions (
      id uuid PRIMARY KEY,
      report_id uuid NOT NULL UNIQUE REFERENCES reports(id),
      property_id uuid REFERENCES properties(id),
      schema_version text NOT NULL,
      model text,
      raw_json jsonb,
      normalized_json jsonb,
      validation_issues jsonb NOT NULL DEFAULT '[]'::jsonb,
      status text NOT NULL DEFAULT 'analyzing',
      input_tokens integer,
      output_tokens integer,
      cost_usd numeric(14,6),
      duration_ms integer,
      retry_count integer NOT NULL DEFAULT 0,
      created_at timestamptz DEFAULT now(),
      updated_at timestamptz DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS report_extractions_property_idx
      ON report_extractions(property_id);
    """)


def downgrade():
    op.execute("""
    DROP INDEX IF EXISTS report_extractions_property_idx;
    DROP TABLE IF EXISTS report_extractions;
    """)
