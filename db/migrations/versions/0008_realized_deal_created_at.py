"""Add creation timestamp used by calibration ordering."""

from alembic import op

revision = "0008_realized_deal_created_at"
down_revision = "0007_property_scoped_flag_aggregation"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE realized_deals
        ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
    """)


def downgrade():
    op.execute("ALTER TABLE realized_deals DROP COLUMN IF EXISTS created_at;")
