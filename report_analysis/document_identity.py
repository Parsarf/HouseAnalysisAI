"""Conservative identity matching for document entities (no fuzzy auto-merge)."""
import hashlib
from uuid import uuid4

from sqlalchemy import or_, text

from db import models as dbm
from identity.service import normalize_address, normalize_apn


def resolve_identity(session, report, address, *, apn=None, fips=None, zip5=None,
                     city=None, state=None):
    identity = normalize_address(address, zip5)
    if not identity.house_number or (not zip5 and not (city and state)):
        raise ValueError("A street address and ZIP or city/state are required to resolve identity")
    address_hash = identity.address_hash if zip5 else hashlib.sha256(
        f"{identity.address_key}|{city.upper()}|{state.upper()}".encode()
    ).hexdigest()
    apn_key = normalize_apn(apn, fips) if apn and fips else None
    if session.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256((apn_key or address_hash).encode()).digest()[:8],
                             "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    clauses = [dbm.Property.address_hash == address_hash]
    if apn_key:
        clauses.append(dbm.Property.apn_key == apn_key)
    matches = session.query(dbm.Property).filter(
        dbm.Property.merged_into_id.is_(None), or_(*clauses),
    ).all()
    if len(matches) > 1:
        raise ValueError("Address and parcel identifiers point to different properties")
    if matches:
        row = matches[0]
        existing = normalize_address(row.address_line1 or "", row.zip5)
        if existing.address_key != identity.address_key or (
            apn and row.apn and normalize_apn(apn) != normalize_apn(row.apn)
        ):
            raise ValueError("Conflicting address, unit, or parcel identifier")
        return row  # In particular, do not restore archived properties on reanalysis.
    row = dbm.Property(id=uuid4(), address_line1=address, address_key=identity.address_key,
                       address_hash=address_hash, apn=apn, apn_key=apn_key,
                       fips_county=fips, zip5=zip5, city=city, state=state)
    session.add(row)
    session.flush()
    return row
