"""Initial ACQ schema."""
from pathlib import Path

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).parents[2].joinpath("schema.sql").read_text())


def downgrade():
    op.execute("""
    DROP TABLE IF EXISTS saved_views, history, settings, realized_deals, property_notes,
      change_events, flags, rankings, scores, scoring_configs, offer_scenarios,
      deal_scenarios, assumption_sets, comparable_sales, listings, valuations,
      bankruptcy_events, foreclosure_events, liens, mortgages, field_resolutions,
      extracted_facts, extraction_units, document_signatures, reports,
      property_owners, owners, properties, batches, jobs CASCADE;
    DROP EXTENSION IF EXISTS pg_trgm;
    """)
