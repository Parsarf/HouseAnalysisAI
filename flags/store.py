"""Flag persistence and recomputation reconciliation.

Flags are human-facing findings. They are therefore reconciled by logical issue,
not appended once per calculation row. Offer scenarios remain in their own
table; this module only controls the review queue.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from contracts import FlagRequest
from db.models import Flag
from flags.workflow import _write_history


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _logical_key(request: FlagRequest) -> str:
    logical = request.logical_key or request.payload.get("logical_key") or request.dedupe_key
    property_prefix = f"{request.property_id}:"
    return logical[len(property_prefix):] if logical.startswith(property_prefix) else str(logical)


def _normalized(request: FlagRequest) -> tuple[str, str, str]:
    logical = _logical_key(request)
    dedupe = f"{request.property_id}:{logical}"
    fingerprint = request.fingerprint or _fingerprint(request.payload)
    return logical, dedupe, fingerprint


def _row_logical(flag: Flag) -> str:
    if flag.logical_key:
        return flag.logical_key
    if flag.flag_type == "short_sale_candidate":
        return "short_sale_candidate"
    prefix = f"{flag.property_id}:"
    return flag.dedupe_key.removeprefix(prefix)


def _record(session: Session, flag: Flag, action: str, before: dict, after: dict) -> None:
    _write_history(session, flag.id, action, before=before, after=after)


def _supersede(session: Session, flag: Flag, *, winner: Flag | None = None, reason: str) -> None:
    if flag.status != "open":
        return
    before = {"status": flag.status, "resolution": flag.resolution}
    flag.status = "resolved"
    flag.resolution = reason
    flag.resolved_at = datetime.now(UTC)
    flag.note = "Automatically closed because the recomputed finding is no longer current."
    if winner is not None:
        flag.superseded_by = winner.id
    _record(session, flag, f"flag_{reason}", before, {
        "status": flag.status, "resolution": flag.resolution,
        "superseded_by": str(winner.id) if winner else None,
    })


def _new_flag(request: FlagRequest, logical: str, dedupe: str, fingerprint: str) -> Flag:
    return Flag(property_id=request.property_id, flag_type=request.flag_type.value,
                payload=request.payload, financial_impact_usd=request.financial_impact_usd,
                status="open", dedupe_key=dedupe, logical_key=logical,
                fingerprint=fingerprint)


def persist_flags(session: Session, requests: list[FlagRequest]) -> list[Flag]:
    """Append requests idempotently, preserving the legacy producer contract.

    Recompute callers should use :func:`sync_flags`, which also closes findings
    that disappeared. This function remains append-only for identity/import
    producers that intentionally submit a partial set.
    """
    if not requests:
        return []
    deduped: dict[str, tuple[FlagRequest, str, str, str]] = {}
    for request in requests:
        logical, dedupe, fingerprint = _normalized(request)
        deduped.setdefault(dedupe, (request, logical, dedupe, fingerprint))
    existing = set(session.scalars(select(Flag.dedupe_key).where(
        Flag.dedupe_key.in_(list(deduped)),
    )).all())
    created: list[Flag] = []
    for dedupe, (request, logical, _, fingerprint) in deduped.items():
        if dedupe in existing:
            continue
        flag = _new_flag(request, logical, dedupe, fingerprint)
        session.add(flag)
        created.append(flag)
    session.flush()
    return created


def sync_flags(session: Session, property_id, requests: list[FlagRequest]) -> list[Flag]:
    """Reconcile one property's current findings with persisted flags.

    Open flags are updated in place when their logical issue remains current;
    missing findings are resolved as ``superseded_recompute``. A manually
    resolved finding stays resolved for the same fingerprint. If a finding was
    superseded by a recompute and later returns, it is reopened; a materially
    changed manually-resolved finding gets a new historical row.
    """
    property_uuid = UUID(str(property_id))
    property_id = str(property_uuid)
    normalized: dict[tuple[str, str], tuple[FlagRequest, str, str, str]] = {}
    for request in requests:
        if str(request.property_id) != property_id:
            continue
        logical, dedupe, fingerprint = _normalized(request)
        normalized.setdefault((request.flag_type.value, logical),
                              (request, logical, dedupe, fingerprint))

    rows = session.scalars(select(Flag).where(Flag.property_id == property_uuid)).all()
    groups: dict[tuple[str, str], list[Flag]] = {}
    for row in rows:
        logical = _row_logical(row)
        row.logical_key = logical
        groups.setdefault((row.flag_type, logical), []).append(row)

    # Collapse legacy duplicate open rows before applying current findings.
    for group_rows in groups.values():
        open_rows = [row for row in group_rows if row.status == "open"]
        if len(open_rows) <= 1:
            continue
        legacy_winner = max(open_rows, key=lambda row: row.financial_impact_usd or Decimal(-1))
        for duplicate in open_rows:
            if duplicate is not legacy_winner:
                _supersede(session, duplicate, winner=legacy_winner, reason="superseded_duplicate")

    current_groups = set(normalized)
    for key, group_rows in groups.items():
        if key not in current_groups:
            for row in group_rows:
                _supersede(session, row, reason="superseded_recompute")

    changed_or_created: list[Flag] = []
    for key, (request, logical, base_dedupe, fingerprint) in normalized.items():
        group_rows = groups.get(key, [])
        open_rows = [row for row in group_rows if row.status == "open"]
        winner: Flag | None = open_rows[0] if open_rows else None
        if winner is not None:
            before = {"payload": winner.payload, "financial_impact_usd": winner.financial_impact_usd,
                      "fingerprint": winner.fingerprint}
            winner.payload = request.payload
            winner.financial_impact_usd = request.financial_impact_usd
            winner.logical_key = logical
            winner.fingerprint = fingerprint
            if not any(row is not winner and row.dedupe_key == base_dedupe for row in group_rows):
                winner.dedupe_key = base_dedupe
            if before != {"payload": winner.payload, "financial_impact_usd": winner.financial_impact_usd,
                          "fingerprint": winner.fingerprint}:
                _record(session, winner, "flag_recomputed", before, {
                    "payload": winner.payload, "financial_impact_usd": winner.financial_impact_usd,
                    "fingerprint": winner.fingerprint,
                })
            changed_or_created.append(winner)
            continue

        same_fingerprint = [row for row in group_rows if row.fingerprint == fingerprint]
        reopened = next((row for row in same_fingerprint
                         if row.status == "resolved" and row.resolution == "superseded_recompute"), None)
        if reopened is not None:
            before = {"status": reopened.status, "resolution": reopened.resolution}
            reopened.status = "open"
            reopened.resolution = None
            reopened.resolved_at = None
            reopened.note = None
            reopened.payload = request.payload
            reopened.financial_impact_usd = request.financial_impact_usd
            reopened.dedupe_key = base_dedupe
            reopened.logical_key = logical
            reopened.fingerprint = fingerprint
            _record(session, reopened, "flag_reappeared", before, {"status": "open"})
            changed_or_created.append(reopened)
            continue
        # A manually resolved identical finding remains resolved. Only a new
        # fingerprint is eligible to create a new review item.
        if same_fingerprint:
            continue
        dedupe = base_dedupe
        if any(row.dedupe_key == dedupe for row in group_rows):
            dedupe = f"{base_dedupe}:v{fingerprint[:16]}"
        flag = _new_flag(request, logical, dedupe, fingerprint)
        session.add(flag)
        changed_or_created.append(flag)
    session.flush()
    return changed_or_created


def open_flags(session: Session, property_id) -> list[Flag]:
    """Open flags for a property, sorted by financial impact descending."""
    rows = session.scalars(
        select(Flag).where(Flag.property_id == property_id, Flag.status == "open")).all()
    return sorted(rows, key=lambda flag: flag.financial_impact_usd if flag.financial_impact_usd is not None else Decimal(-1),
                  reverse=True)
