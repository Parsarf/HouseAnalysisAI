"""Schema completion: config tables, versioning columns, uniqueness constraints.

Adds the small config tables the engines require (lender_aliases,
historical_rate_index, regional_cost_index, transfer_tax_rates,
prompt_versions), the failure/versioning columns on reports and scores,
the unique constraints on deal_scenarios and rankings, and the
extracted_facts(report_id) index. All statements are idempotent so this
is a no-op on databases already created from the current schema.sql.
"""
from alembic import op

revision = "0002_schema_items"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE reports ADD COLUMN IF NOT EXISTS failure_reason text;
    ALTER TABLE reports ADD COLUMN IF NOT EXISTS section_match_rate numeric(5,4);
    ALTER TABLE scores ADD COLUMN IF NOT EXISTS engine_version text;
    ALTER TABLE scores ADD COLUMN IF NOT EXISTS resolution_version text;

    CREATE TABLE IF NOT EXISTS lender_aliases (
      id uuid PRIMARY KEY, alias text NOT NULL UNIQUE, canonical_name text NOT NULL);
    CREATE TABLE IF NOT EXISTS historical_rate_index (
      id uuid PRIMARY KEY, year integer NOT NULL, loan_type text NOT NULL,
      rate numeric(7,6) NOT NULL, UNIQUE(year, loan_type));
    CREATE TABLE IF NOT EXISTS regional_cost_index (
      id uuid PRIMARY KEY, region_key text NOT NULL UNIQUE,
      index_value numeric(10,6) NOT NULL, effective_from date);
    CREATE TABLE IF NOT EXISTS transfer_tax_rates (
      id uuid PRIMARY KEY, lookup_key text NOT NULL UNIQUE, rate numeric(9,6) NOT NULL,
      flat_amount numeric(14,2), notes text);
    CREATE TABLE IF NOT EXISTS prompt_versions (
      id uuid PRIMARY KEY, version text NOT NULL UNIQUE, unit_type text,
      prompt_path text, prompt_hash text, created_at timestamptz DEFAULT now());

    CREATE INDEX IF NOT EXISTS extracted_facts_report_idx ON extracted_facts(report_id);

    ALTER TABLE deal_scenarios DROP CONSTRAINT IF EXISTS deal_scenarios_uq;
    ALTER TABLE deal_scenarios ADD CONSTRAINT deal_scenarios_uq
      UNIQUE(property_id, strategy, scenario, assumption_set_id, engine_version);
    ALTER TABLE rankings DROP CONSTRAINT IF EXISTS rankings_uq;
    ALTER TABLE rankings ADD CONSTRAINT rankings_uq
      UNIQUE(scope_type, scope_id, property_id, ranked_at);
    """)


def downgrade():
    op.execute("""
    ALTER TABLE rankings DROP CONSTRAINT IF EXISTS rankings_uq;
    ALTER TABLE deal_scenarios DROP CONSTRAINT IF EXISTS deal_scenarios_uq;
    DROP INDEX IF EXISTS extracted_facts_report_idx;
    DROP TABLE IF EXISTS prompt_versions, transfer_tax_rates, regional_cost_index,
      historical_rate_index, lender_aliases;
    ALTER TABLE scores DROP COLUMN IF EXISTS resolution_version;
    ALTER TABLE scores DROP COLUMN IF EXISTS engine_version;
    ALTER TABLE reports DROP COLUMN IF EXISTS section_match_rate;
    ALTER TABLE reports DROP COLUMN IF EXISTS failure_reason;
    """)
