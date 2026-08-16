from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from contracts import FlagRequest
from db.models import Flag


def persist_flags(session: Session, requests: list[FlagRequest]) -> list[Flag]:
    """Insert flag rows, skipping any whose dedupe_key already exists (open or resolved).

    Re-running a fact producer must not duplicate flags (spec WP-9). Does not commit —
    the caller owns the transaction.
    """
    if not requests:
        return []
    property_ids = {request.property_id for request in requests}
    existing = set(session.scalars(
        select(Flag.dedupe_key).where(Flag.property_id.in_(property_ids))).all())
    created: list[Flag] = []
    seen: set[str] = set()
    for request in requests:
        if request.dedupe_key in existing or request.dedupe_key in seen:
            continue
        seen.add(request.dedupe_key)
        flag = Flag(property_id=request.property_id, flag_type=request.flag_type.value,
                    payload=request.payload, financial_impact_usd=request.financial_impact_usd,
                    status="open", dedupe_key=request.dedupe_key)
        session.add(flag)
        created.append(flag)
    session.flush()
    return created


def open_flags(session: Session, property_id) -> list[Flag]:
    """Open flags for a property, sorted by financial impact descending (spec §12)."""
    rows = session.scalars(
        select(Flag).where(Flag.property_id == property_id, Flag.status == "open")).all()
    return sorted(rows, key=lambda flag: flag.financial_impact_usd if flag.financial_impact_usd is not None else Decimal("-1"),
                  reverse=True)
