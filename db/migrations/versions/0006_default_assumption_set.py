"""Seed baseline underwriting assumptions for deterministic analysis."""

import json
from pathlib import Path

from alembic import op
from sqlalchemy import String, bindparam, text

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


def _insert_statement():
    # Existing installations retain their configured assumptions. A fresh
    # installation receives the same validated baseline used by the finance
    # regression fixtures so whole-PDF analysis can compute deterministically.
    statement = text(f"""
    INSERT INTO assumption_sets
      (id, name, is_default, params, version, effective_from)
    SELECT
      '{DEFAULT_ASSUMPTION_SET_ID}'::uuid,
      'default',
      true,
      CAST(:default_params AS jsonb),
      1,
      CURRENT_DATE
    WHERE NOT EXISTS (SELECT 1 FROM assumption_sets);
    """)
    return statement.bindparams(bindparam(
        "default_params", value=_default_params(), type_=String(), literal_execute=True,
    ))


def upgrade():
    op.execute(_insert_statement())


def downgrade():
    op.execute(f"""
    DELETE FROM assumption_sets
    WHERE id = '{DEFAULT_ASSUMPTION_SET_ID}'::uuid
      AND name = 'default'
      AND version = 1;
    """)
