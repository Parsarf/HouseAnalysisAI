"""Offline checks that db/models.py, db/schema.sql, and the alembic migrations agree.

No live Postgres: metadata is inspected in-process and the migrations are
compiled with `alembic upgrade/downgrade --sql` (offline mode).
"""
import re
import subprocess
import sys
from pathlib import Path

from sqlalchemy import UniqueConstraint

from common.db import Base
import db.models  # noqa: F401  (populates Base.metadata)
import identity.models  # noqa: F401  (maps identity_merge_report_moves on the shared Base)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = REPO_ROOT / "db" / "schema.sql"


def _schema_tables() -> set[str]:
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA_SQL.read_text()))


def test_models_cover_every_table_in_schema_sql():
    tables = _schema_tables()
    assert len(tables) >= 30
    missing = tables - set(Base.metadata.tables)
    assert not missing, f"tables in schema.sql missing from db/models.py: {sorted(missing)}"


def test_new_config_tables_in_schema_and_models():
    for table in ("lender_aliases", "historical_rate_index", "regional_cost_index", "transfer_tax_rates", "prompt_versions"):
        assert table in _schema_tables()
        assert table in Base.metadata.tables


def test_reports_and_scores_new_columns():
    reports_cols = set(Base.metadata.tables["reports"].columns.keys())
    assert {"failure_reason", "section_match_rate"} <= reports_cols
    scores_cols = set(Base.metadata.tables["scores"].columns.keys())
    assert {"engine_version", "resolution_version"} <= scores_cols


def test_unique_constraints_and_index():
    deal_ucs = [c for c in Base.metadata.tables["deal_scenarios"].constraints
                if isinstance(c, UniqueConstraint) and c.name == "deal_scenarios_uq"]
    assert deal_ucs and [c.name for c in deal_ucs[0].columns] == [
        "property_id", "strategy", "scenario", "assumption_set_id", "engine_version"]
    rank_ucs = [c for c in Base.metadata.tables["rankings"].constraints
                if isinstance(c, UniqueConstraint) and c.name == "rankings_uq"]
    assert rank_ucs and [c.name for c in rank_ucs[0].columns] == [
        "scope_type", "scope_id", "property_id", "ranked_at"]
    idx_names = {i.name for i in Base.metadata.tables["extracted_facts"].indexes}
    assert "extracted_facts_report_idx" in idx_names


def _alembic_sql(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_migration_chain_and_offline_upgrade_compiles():
    sql = _alembic_sql("upgrade", "head", "--sql")
    for fragment in ("lender_aliases", "historical_rate_index", "regional_cost_index",
                     "transfer_tax_rates", "prompt_versions", "failure_reason",
                     "section_match_rate", "engine_version", "resolution_version",
                     "deal_scenarios_uq", "rankings_uq", "extracted_facts_report_idx"):
        assert fragment in sql, f"{fragment} missing from offline upgrade SQL"


def test_migration_downgrade_compiles_and_reverses():
    sql = _alembic_sql("downgrade", "head:base", "--sql")
    for fragment in ("DROP TABLE IF EXISTS prompt_versions", "DROP CONSTRAINT IF EXISTS deal_scenarios_uq",
                     "DROP CONSTRAINT IF EXISTS rankings_uq", "DROP INDEX IF EXISTS extracted_facts_report_idx",
                     "DROP COLUMN IF EXISTS failure_reason", "DROP COLUMN IF EXISTS engine_version"):
        assert fragment in sql, f"{fragment} missing from offline downgrade SQL"
