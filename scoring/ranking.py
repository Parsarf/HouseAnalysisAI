"""Ranking job and scoring-config loading (spec section 11.1).

This module is the only DB-touching part of ``scoring``; the engine stays
pure. Tables are written with ``sqlalchemy.text()`` SQL against the
``db/schema.sql`` definitions (``scores``, ``scoring_configs``, ``rankings``)
so no ORM models are required.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

# Single statement per WP-8: snapshot the previous ranks for the scope, take
# the latest rankable score per property, then insert the new snapshot with
# prev_rank carried forward. Unrankable gates (insufficient_data,
# open_gating_flag) are excluded; needs_review rows stay rankable.
RANKINGS_SQL = text(
    """
    WITH active_config AS (
        SELECT COALESCE(
            (
                SELECT id
                FROM scoring_configs
                WHERE is_active
                ORDER BY version DESC NULLS LAST, id DESC
                LIMIT 1
            ),
            (
                SELECT scoring_config_id
                FROM scores
                WHERE scoring_config_id IS NOT NULL
                ORDER BY computed_at DESC NULLS LAST, id DESC
                LIMIT 1
            )
        ) AS id
    ),
    scope_properties AS (
        SELECT p.id AS property_id
        FROM properties p
        WHERE p.merged_into_id IS NULL
          AND p.archived_at IS NULL
          AND (
              (:scope_type = 'portfolio' AND CAST(:scope_id AS uuid) IS NULL)
              OR (
                  :scope_type = 'batch'
                  AND CAST(:scope_id AS uuid) IS NOT NULL
                  AND EXISTS (
                      SELECT 1
                      FROM reports report
                      WHERE report.property_id = p.id
                        AND report.batch_id = CAST(:scope_id AS uuid)
                  )
              )
          )
    ),
    previous AS (
        SELECT r.property_id, r.rank
        FROM rankings r
        WHERE r.scope_type = :scope_type
          AND r.scope_id IS NOT DISTINCT FROM CAST(:scope_id AS uuid)
          AND r.ranked_at = (
              SELECT MAX(r2.ranked_at)
              FROM rankings r2
              WHERE r2.scope_type = :scope_type
                AND r2.scope_id IS NOT DISTINCT FROM CAST(:scope_id AS uuid)
          )
    ),
    latest AS (
        SELECT DISTINCT ON (s.property_id) s.property_id, s.overall
        FROM scores s
        JOIN active_config config ON config.id = s.scoring_config_id
        JOIN scope_properties scoped ON scoped.property_id = s.property_id
        WHERE NOT (COALESCE(s.gates_applied, '{}'::text[]) && ARRAY['insufficient_data', 'open_gating_flag']::text[])
          AND s.overall IS NOT NULL
        ORDER BY s.property_id, s.computed_at DESC NULLS LAST, s.id DESC
    )
    INSERT INTO rankings (id, scope_type, scope_id, property_id, rank, prev_rank, score, ranked_at)
    SELECT gen_random_uuid(), :scope_type, CAST(:scope_id AS uuid), latest.property_id,
           RANK() OVER (ORDER BY latest.overall DESC, latest.property_id),
           previous.rank,
           latest.overall,
           now()
    FROM latest
    LEFT JOIN previous ON previous.property_id = latest.property_id
    """
)

ACTIVE_CONFIG_SQL = text(
    """
    SELECT id, weights, bounds, distress_points, gates, version
    FROM scoring_configs
    WHERE is_active
    ORDER BY version DESC NULLS LAST
    LIMIT 1
    """
)


@dataclass(frozen=True)
class RankedRow:
    property_id: UUID
    rank: int
    prev_rank: int | None
    score: Decimal


def compute_ranks(scores: Mapping[UUID, Decimal] | Iterable[tuple[UUID, Decimal]],
                  previous: Mapping[UUID, int] | None = None) -> list[RankedRow]:
    """Pure mirror of the RANK() window in RANKINGS_SQL (overall DESC,
    property_id tie-break). Used by offline tests and by callers that already
    hold the scores in memory."""
    previous = previous or {}
    items = scores.items() if isinstance(scores, Mapping) else scores
    ordered = sorted(items, key=lambda item: (-item[1], item[0].int))
    return [
        RankedRow(property_id=property_id, rank=index + 1, prev_rank=previous.get(property_id), score=value)
        for index, (property_id, value) in enumerate(ordered)
    ]


def rank_scope(conn: Connection, scope_type: str, scope_id: UUID | None = None) -> int:
    """Materialize one rankings snapshot for a scope in a single SQL
    statement. Currently supported scopes are batch and whole portfolio;
    saved views require their filter membership to be resolved first. Returns
    the number of rows written."""
    if scope_type == "portfolio":
        if scope_id is not None:
            raise ValueError("portfolio ranking does not accept a scope_id")
    elif scope_type == "batch":
        if scope_id is None:
            raise ValueError("batch ranking requires a scope_id")
    else:
        # Saved-view membership must first be evaluated through the validated
        # filter grammar. Ranking the whole portfolio under a saved-view label
        # would be silently and materially wrong.
        raise ValueError(f"unsupported ranking scope: {scope_type}")
    result = conn.execute(RANKINGS_SQL, {"scope_type": scope_type, "scope_id": scope_id})
    return max(0, result.rowcount or 0)


def load_active_scoring_config(conn: Connection) -> tuple[UUID, dict[str, Any]] | None:
    """Load the active scoring_configs row as (id, config-dict) suitable for
    ``score(..., config=...)``. Risk points live under a "risk" key inside the
    distress_points jsonb column, since the schema has no dedicated column.
    Returns None when no active config exists (callers fall back to the
    in-code DEFAULT_CONFIG)."""
    row = conn.execute(ACTIVE_CONFIG_SQL).mappings().first()
    if row is None:
        return None
    distress_points = dict(row["distress_points"] or {})
    risk_points = distress_points.pop("risk", None)
    config: dict[str, Any] = {
        "weights": row["weights"] or {},
        "bounds": row["bounds"] or {},
        "distress_points": distress_points,
        "gates": row["gates"] or {},
        "version": row["version"],
    }
    if risk_points:
        config["risk_points"] = risk_points
    return row["id"], config
