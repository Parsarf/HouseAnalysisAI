"""Allow immutable duplicate report references in separate batches."""

from alembic import op

revision = "0005_duplicate_report_references"
down_revision = "0004_report_extractions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE reports DROP CONSTRAINT IF EXISTS reports_sha256_key;
    DROP INDEX IF EXISTS ix_reports_sha256;
    CREATE INDEX IF NOT EXISTS ix_reports_sha256 ON reports(sha256);
    CREATE UNIQUE INDEX IF NOT EXISTS reports_sha256_original_uq
      ON reports(sha256) WHERE duplicate_of IS NULL;
    """)


def downgrade():
    op.execute("""
    DROP INDEX IF EXISTS reports_sha256_original_uq;
    DROP INDEX IF EXISTS ix_reports_sha256;
    DELETE FROM reports WHERE duplicate_of IS NOT NULL;
    ALTER TABLE reports ADD CONSTRAINT reports_sha256_key UNIQUE (sha256);
    """)
