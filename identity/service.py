import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common.locks import acquire_advisory_lock
from db.models import Property, Report


@dataclass(frozen=True)
class Identity:
    apn_key: str | None
    address_key: str
    address_hash: str
    house_number: str | None
    zip5: str | None


def normalize_apn(apn: str | None, fips: str | None = None) -> str | None:
    if not apn:
        return None
    return f"{fips or ''}{re.sub(r'[^A-Za-z0-9]', '', apn).upper()}"


def normalize_address(address: str, zip5: str | None = None) -> Identity:
    value = re.sub(r"[^A-Za-z0-9 ]", " ", address.upper())
    value = re.sub(r"\s+", " ", value).strip()
    replacements = {"STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "BOULEVARD": "BLVD", "DRIVE": "DR", "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}
    for source, target in replacements.items():
        value = re.sub(rf"\b{source}\b", target, value)
    key = f"{value}|{zip5 or ''}"
    return Identity(None, key, hashlib.sha256(key.encode()).hexdigest(), re.match(r"\d+", value).group(0) if re.match(r"\d+", value) else None, zip5)


def resolve_property(session: Session, address: str, apn: str | None = None, fips: str | None = None, zip5: str | None = None) -> Property:
    identity = normalize_address(address, zip5)
    apn_key = normalize_apn(apn, fips)
    acquire_advisory_lock(session, apn_key or identity.address_hash)
    query = select(Property).where(Property.merged_into_id.is_(None), or_(Property.apn_key == apn_key, Property.address_hash == identity.address_hash))
    existing = session.execute(query).scalars().first()
    if existing:
        if apn_key and existing.apn_key == apn_key and existing.zip5 and zip5 and existing.zip5 != zip5:
            raise ValueError("identity_conflict")
        return existing
    row = Property(apn=apn, apn_key=apn_key, fips_county=fips, address_line1=address, address_key=identity.address_key, address_hash=identity.address_hash, zip5=zip5)
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        existing = session.execute(query).scalars().first()
        if existing:
            return existing
        raise
    return row


def attach_report(session: Session, report: Report, address: str, apn: str | None = None, fips: str | None = None, zip5: str | None = None) -> Property:
    property_row = resolve_property(session, address, apn, fips, zip5)
    report.property_id = property_row.id
    return property_row
