"""Seed baseline underwriting assumptions for deterministic analysis."""

import json
from pathlib import Path

from alembic import op

revision = "0006_default_assumption_set"
down_revision = "0005_duplicate_report_references"
branch_labels = None
depends_on = None

DEFAULT_ASSUMPTION_SET_ID = "10000000-0000-0000-0000-000000000001"


def _default_params() -> str:
    fixture = Path(__file__).parents[3] / "fixtures" / "assumptions" / "default.json"
    payload = json.loads(fixture.read_text())
    for key in ("id", "version", "name"):
        payload.pop(key, None)
    return json.dumps(payload, separators=(",", ":"))


def upgrade():
    # Existing installations retain their configured assumptions. A fresh
    # installation receives the same validated baseline used by the finance
    # regression fixtures so whole-PDF analysis can compute deterministically.
    params = _default_params().replace("'", "''")
    op.execute(f"""
    INSERT INTO assumption_sets
      (id, name, is_default, params, version, effective_from)
    SELECT
      '{DEFAULT_ASSUMPTION_SET_ID}'::uuid,
      'default',
      true,
      '{params}'::jsonb,
      1,
      CURRENT_DATE
    WHERE NOT EXISTS (SELECT 1 FROM assumption_sets);
    """)


def downgrade():
    op.execute(f"""
    DELETE FROM assumption_sets
    WHERE id = '{DEFAULT_ASSUMPTION_SET_ID}'::uuid
      AND name = 'default'
      AND version = 1;
    """)
