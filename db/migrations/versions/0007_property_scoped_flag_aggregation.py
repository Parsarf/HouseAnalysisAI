"""Make flags property-scoped and consolidate legacy scenario duplicates."""

from alembic import op

revision = "0007_property_scoped_flag_aggregation"
down_revision = "0006_default_assumption_set"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE flags ADD COLUMN IF NOT EXISTS logical_key text")
    op.execute("ALTER TABLE flags ADD COLUMN IF NOT EXISTS fingerprint text")
    op.execute("ALTER TABLE flags ADD COLUMN IF NOT EXISTS superseded_by uuid REFERENCES flags(id)")

    # Make legacy dedupe keys property-aware before any new unique/index rules
    # are applied. Existing keys are retained as suffixes for auditability.
    op.execute("""
    UPDATE flags
    SET dedupe_key = property_id::text || ':' || dedupe_key
    WHERE property_id IS NOT NULL
      AND dedupe_key NOT LIKE property_id::text || ':%'
    """)
    op.execute("""
    UPDATE flags
    SET logical_key = CASE
        WHEN flag_type = 'short_sale_candidate' THEN 'short_sale_candidate'
        WHEN dedupe_key LIKE property_id::text || ':%'
          THEN substring(dedupe_key from char_length(property_id::text) + 2)
        ELSE dedupe_key
    END
    WHERE logical_key IS NULL
    """)
    # Keep every old row for auditability, but remove duplicate open rows from
    # the active queue. The highest-impact row remains the canonical winner.
    op.execute("""
    WITH ranked AS (
        SELECT id,
               first_value(id) OVER (
                   PARTITION BY property_id, flag_type, logical_key
                   ORDER BY COALESCE(financial_impact_usd, -1) DESC, id
               ) AS winner_id,
               row_number() OVER (
                   PARTITION BY property_id, flag_type, logical_key
                   ORDER BY COALESCE(financial_impact_usd, -1) DESC, id
               ) AS row_number
        FROM flags
        WHERE status = 'open' AND logical_key IS NOT NULL
    )
    INSERT INTO history (id, entity_type, entity_id, action, before, after, at)
    SELECT gen_random_uuid(), 'flag', id, 'flag_superseded_duplicate', '{}'::jsonb,
           jsonb_build_object('status', 'resolved', 'resolution', 'superseded_duplicate'), now()
    FROM ranked
    WHERE row_number > 1
    """)
    op.execute("""
    WITH ranked AS (
        SELECT id,
               first_value(id) OVER (
                   PARTITION BY property_id, flag_type, logical_key
                   ORDER BY COALESCE(financial_impact_usd, -1) DESC, id
               ) AS winner_id,
               row_number() OVER (
                   PARTITION BY property_id, flag_type, logical_key
                   ORDER BY COALESCE(financial_impact_usd, -1) DESC, id
               ) AS row_number
        FROM flags
        WHERE status = 'open' AND logical_key IS NOT NULL
    )
    UPDATE flags AS duplicate
    SET status = 'resolved',
        resolution = 'superseded_duplicate',
        resolved_at = now(),
        note = 'Legacy duplicate consolidated during property-scoped flag migration.',
        superseded_by = ranked.winner_id
    FROM ranked
    WHERE duplicate.id = ranked.id
      AND ranked.row_number > 1
    """)
    op.execute("""
    CREATE INDEX IF NOT EXISTS flags_logical_key_idx
      ON flags(property_id, flag_type, logical_key)
    """)
    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS flags_open_property_logical_uq
      ON flags(property_id, flag_type, logical_key)
      WHERE status = 'open' AND logical_key IS NOT NULL
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS flags_open_property_logical_uq")
    op.execute("DROP INDEX IF EXISTS flags_logical_key_idx")
    op.execute("ALTER TABLE flags DROP COLUMN IF EXISTS superseded_by")
    op.execute("ALTER TABLE flags DROP COLUMN IF EXISTS fingerprint")
    op.execute("ALTER TABLE flags DROP COLUMN IF EXISTS logical_key")
