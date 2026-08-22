"""Add owner contacts, archive state, and report document kind."""

from alembic import op

revision = "0009_owner_chat_archive_outreach"
down_revision = "0008_realized_deal_created_at"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE properties ADD COLUMN IF NOT EXISTS archived_at timestamptz")
    op.execute("CREATE INDEX IF NOT EXISTS ix_properties_archived_at ON properties(archived_at)")
    op.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS doc_kind varchar(30)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_reports_doc_kind ON reports(doc_kind)")
    op.execute("ALTER TABLE owners ADD COLUMN IF NOT EXISTS age integer")
    op.execute("ALTER TABLE owners ADD COLUMN IF NOT EXISTS gender varchar(30)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS owner_contacts (
            id uuid PRIMARY KEY,
            owner_id uuid NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
            kind varchar(20) NOT NULL,
            value varchar(255) NOT NULL,
            rank integer,
            source varchar(120) NOT NULL,
            confidence numeric(5,4),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT owner_contacts_identity_uq UNIQUE(owner_id, kind, value, source)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_owner_contacts_owner_id ON owner_contacts(owner_id)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS owner_contacts")
    op.execute("DROP INDEX IF EXISTS ix_reports_doc_kind")
    op.execute("ALTER TABLE reports DROP COLUMN IF EXISTS doc_kind")
    op.execute("ALTER TABLE owners DROP COLUMN IF EXISTS gender")
    op.execute("ALTER TABLE owners DROP COLUMN IF EXISTS age")
    op.execute("DROP INDEX IF EXISTS ix_properties_archived_at")
    op.execute("ALTER TABLE properties DROP COLUMN IF EXISTS archived_at")
