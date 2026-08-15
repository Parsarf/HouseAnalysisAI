"""Initial ACQ schema."""
from pathlib import Path
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).parents[3].joinpath("schema.sql").read_text())


def downgrade():
    op.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
