"""Identity merge provenance: identity_merge_report_moves.

Provenance rows for reports re-parented by identity merge(); unmerge()
restores any move whose restored_at is still NULL. The table is mapped in
identity/models.py (MergeReportMove) on the shared Base — db/models.py
intentionally does not duplicate the mapping. Idempotent like 0002 so it
is a no-op on databases already created from the current schema.sql.
"""
from alembic import op

revision = "0003_identity_merge_moves"
down_revision = "0002_schema_items"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE IF NOT EXISTS identity_merge_report_moves (
      id uuid PRIMARY KEY,
      source_property_id uuid NOT NULL REFERENCES properties(id),
      target_property_id uuid NOT NULL REFERENCES properties(id),
      report_id uuid NOT NULL REFERENCES reports(id),
      moved_at timestamptz DEFAULT now(),
      restored_at timestamptz);
    CREATE INDEX IF NOT EXISTS ix_identity_merge_report_moves_source_property_id
      ON identity_merge_report_moves(source_property_id);
    CREATE INDEX IF NOT EXISTS ix_identity_merge_report_moves_report_id
      ON identity_merge_report_moves(report_id);
    """)


def downgrade():
    op.execute("""
    DROP INDEX IF EXISTS ix_identity_merge_report_moves_report_id;
    DROP INDEX IF EXISTS ix_identity_merge_report_moves_source_property_id;
    DROP TABLE IF EXISTS identity_merge_report_moves;
    """)
