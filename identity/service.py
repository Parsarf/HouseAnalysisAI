import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common.locks import acquire_advisory_lock
from contracts import FlagRequest, FlagType
from db.models import Property, Report

from .models import MergeReportMove

# Spec §4.5: pg_trgm similarity within the same ZIP. >= 0.92 with the same
# house number merges; the 0.80-0.92 band creates separately and flags
# possible_duplicate.
FUZZY_MERGE_THRESHOLD = 0.92
FUZZY_DUPLICATE_THRESHOLD = 0.80
FUZZY_SQL_PREFILTER = 0.5  # coarse pg_trgm cutoff so the trigram index is used; Python rescoring decides

DIRECTIONALS = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}

# USPS Publication 28 street-suffix abbreviations (common subset, table-driven
# in-repo so the usaddress dependency is not required).
STREET_SUFFIXES = {
    "ALLEY": "ALY", "AVENUE": "AVE", "BEND": "BND", "BOULEVARD": "BLVD",
    "BRANCH": "BR", "BRIDGE": "BR", "CANYON": "CYN", "CAUSEWAY": "CSWY",
    "CENTER": "CTR", "CIRCLE": "CIR", "CLIFF": "CLF", "COURT": "CT",
    "COVE": "CV", "CREEK": "CRK", "CROSSING": "XING", "DRIVE": "DR",
    "EXPRESSWAY": "EXPY", "EXTENSION": "EXT", "FREEWAY": "FWY", "GLEN": "GLN",
    "GREEN": "GRN", "GROVE": "GRV", "HEIGHTS": "HTS", "HIGHWAY": "HWY",
    "HILL": "HL", "HOLLOW": "HOLW", "JUNCTION": "JCT", "LAKE": "LK",
    "LANE": "LN", "LOOP": "LOOP", "MANOR": "MNR", "MEADOW": "MDW",
    "MOUNT": "MT", "PARK": "PARK", "PARKWAY": "PKWY", "PASS": "PASS",
    "PATH": "PATH", "PIKE": "PIKE", "PLACE": "PL", "PLAZA": "PLZ",
    "POINT": "PT", "RIDGE": "RDG", "ROAD": "RD", "ROW": "ROW", "RUN": "RUN",
    "SHORE": "SHR", "SPRING": "SPG", "SQUARE": "SQ", "STREET": "ST",
    "TERRACE": "TER", "TRACE": "TRCE", "TRAIL": "TRL", "TUNNEL": "TUNL",
    "TURNPIKE": "TPKE", "VALLEY": "VLY", "VIEW": "VW", "VILLAGE": "VLG",
    "WALK": "WALK", "WAY": "WAY",
}

UNIT_DESIGNATORS = {
    "APARTMENT": "APT", "APT": "APT", "SUITE": "STE", "STE": "STE",
    "UNIT": "UNIT", "BUILDING": "BLDG", "BLDG": "BLDG", "FLOOR": "FL",
    "FL": "FL", "ROOM": "RM", "RM": "RM", "DEPARTMENT": "DEPT", "DEPT": "DEPT",
    "LOT": "LOT", "SPACE": "SPC", "SPC": "SPC", "TRAILER": "TRLR", "TRLR": "TRLR",
}

_SUFFIX_ABBREVIATIONS = set(STREET_SUFFIXES.values())


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
    if zip5:
        zip5 = zip5.strip()[:5] or None
    value = address.upper().replace("#", " UNIT ")
    value = re.sub(r"[^A-Z0-9 ]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\bP O BOX\b", "PO BOX", value)
    value = re.sub(r"\bPOBOX\b", "PO BOX", value)
    tokens = value.split() if value else []
    unit = None
    for index, token in enumerate(tokens):
        if token in UNIT_DESIGNATORS and index + 1 < len(tokens):
            unit = f"{UNIT_DESIGNATORS[token]} {tokens[index + 1]}"
            del tokens[index:index + 2]
            break
    tokens = [DIRECTIONALS.get(token, token) for token in tokens]
    tokens = [STREET_SUFFIXES.get(token, token) for token in tokens]
    if tokens[:2] == ["PO", "BOX"]:
        house_number = tokens[2] if len(tokens) > 2 else None
        street, suffix = "PO BOX", ""
    else:
        house_number = tokens[0] if tokens and re.fullmatch(r"\d+[A-Z]?", tokens[0]) else None
        street_tokens = tokens[1:] if house_number else tokens
        if len(street_tokens) > 1 and street_tokens[-1] in _SUFFIX_ABBREVIATIONS:
            street, suffix = " ".join(street_tokens[:-1]), street_tokens[-1]
        else:
            street, suffix = " ".join(street_tokens), ""
    # Spec §4.5: address_key = number|street|suffix|unit|zip5
    key = "|".join([house_number or "", street, suffix, unit or "", zip5 or ""])
    return Identity(None, key, hashlib.sha256(key.encode()).hexdigest(), house_number, zip5)


def _trigrams(value: str) -> set[str]:
    # pg_trgm-style: per alphanumeric word, pad with two leading spaces and one
    # trailing space, then take all 3-character substrings.
    grams = set()
    for word in re.findall(r"[a-z0-9]+", value.lower()):
        padded = f"  {word} "
        for i in range(len(padded) - 2):
            grams.add(padded[i:i + 3])
    return grams


def trigram_similarity(left: str, right: str) -> float:
    # Pure Python Jaccard over trigram sets — mirrors pg_trgm similarity and is
    # testable offline.
    grams_left, grams_right = _trigrams(left), _trigrams(right)
    if not grams_left or not grams_right:
        return 0.0
    shared = len(grams_left & grams_right)
    return shared / (len(grams_left) + len(grams_right) - shared)


def _is_postgres(session: Session) -> bool:
    try:
        return session.get_bind().dialect.name == "postgresql"
    except Exception:
        return False


def _street_portion(address_key: str | None) -> str:
    if not address_key:
        return ""
    return address_key.rsplit("|", 1)[0]


def _house_number_of(property_row: Property) -> str | None:
    if property_row.address_key:
        number = property_row.address_key.split("|", 1)[0]
        if number:
            return number
    if property_row.address_line1:
        match = re.match(r"\s*(\d+[A-Za-z]?)", property_row.address_line1)
        if match:
            return match.group(1).upper()
    return None


def _conflicts_with(existing: Property, identity: Identity) -> bool:
    if existing.zip5 and identity.zip5 and existing.zip5 != identity.zip5:
        return True
    existing_number = _house_number_of(existing)
    return bool(existing_number and identity.house_number and existing_number != identity.house_number)


def _conflict_flags(existing: Property, incoming: Property, identity: Identity, apn_key: str) -> list[FlagRequest]:
    payload = {
        "apn_key": apn_key,
        "existing_zip5": existing.zip5,
        "incoming_zip5": identity.zip5,
        "existing_house_number": _house_number_of(existing),
        "incoming_house_number": identity.house_number,
    }
    return [
        FlagRequest(property_id=flagged.id, flag_type=FlagType.IDENTITY_CONFLICT,
                    payload={**payload, "other_property_id": str(other.id)},
                    financial_impact_usd=None, raised_by="identity",
                    dedupe_key=f"identity-conflict:{apn_key}:{flagged.id}")
        for flagged, other in ((existing, incoming), (incoming, existing))
    ]


def _find_fuzzy_duplicate(session: Session, identity: Identity, apn_key: str | None) -> tuple[Property, float] | None:
    if not identity.zip5:
        return None
    query = select(Property).where(
        Property.merged_into_id.is_(None),
        Property.zip5 == identity.zip5,
        Property.address_key.isnot(None),
    )
    if _is_postgres(session):
        # Uses the properties trigram index; exact scoring happens in Python.
        query = query.where(func.similarity(Property.address_key, identity.address_key) >= FUZZY_SQL_PREFILTER)
    best, best_score = None, 0.0
    subject = _street_portion(identity.address_key)
    for candidate in session.execute(query).scalars():
        if apn_key and candidate.apn_key and candidate.apn_key != apn_key:
            continue  # APN disagreement: never fuzzy-merge across differing APNs
        score = trigram_similarity(subject, _street_portion(candidate.address_key))
        if score > best_score:
            best, best_score = candidate, score
    if best is None or best_score < FUZZY_DUPLICATE_THRESHOLD:
        return None
    return best, best_score


def _create_property(session: Session, query, address: str, apn: str | None, apn_key: str | None,
                     fips: str | None, identity: Identity, zip5: str | None) -> tuple[Property, bool]:
    row = Property(apn=apn, apn_key=apn_key, fips_county=fips, address_line1=address,
                   address_key=identity.address_key, address_hash=identity.address_hash, zip5=zip5)
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        existing = session.execute(query).scalars().first()
        if existing is not None:
            return existing, False
        raise
    return row, True


def resolve_property(session: Session, address: str, apn: str | None = None, fips: str | None = None, zip5: str | None = None) -> Property:
    identity = normalize_address(address, zip5)
    apn_key = normalize_apn(apn, fips)
    if _is_postgres(session):
        acquire_advisory_lock(session, apn_key or identity.address_hash)
    # Guard: with apn_key None, Property.apn_key == None would compile to
    # `apn_key IS NULL` and match every APN-less property.
    clauses = [Property.address_hash == identity.address_hash]
    if apn_key:
        clauses.append(Property.apn_key == apn_key)
    query = select(Property).where(Property.merged_into_id.is_(None), or_(*clauses))
    existing = session.execute(query).scalars().first()
    if existing is not None:
        if apn_key and existing.apn_key == apn_key and _conflicts_with(existing, identity):
            # APN matches but ZIP or house number differs: do not merge. The new
            # row keeps the raw APN but no apn_key, so the unique backstop on
            # apn_key is not tripped and no future auto-merge can occur.
            row, created = _create_property(session, query, address, apn, None, fips, identity, zip5)
            row.identity_flags = _conflict_flags(existing, row, identity, apn_key) if created else []
            return row
        existing.identity_flags = []
        return existing
    duplicate = _find_fuzzy_duplicate(session, identity, apn_key)
    flags: list[FlagRequest] = []
    if duplicate is not None:
        candidate, score = duplicate
        if (score >= FUZZY_MERGE_THRESHOLD and identity.house_number
                and _house_number_of(candidate) == identity.house_number):
            candidate.identity_flags = []
            return candidate
        row, created = _create_property(session, query, address, apn, apn_key, fips, identity, zip5)
        if created:
            flags.append(FlagRequest(
                property_id=row.id, flag_type=FlagType.POSSIBLE_DUPLICATE,
                payload={"other_property_id": str(candidate.id), "similarity": round(score, 4), "zip5": zip5},
                financial_impact_usd=None, raised_by="identity",
                dedupe_key=f"possible-duplicate:{candidate.id}:{identity.address_hash}"))
        row.identity_flags = flags
        return row
    row, _ = _create_property(session, query, address, apn, apn_key, fips, identity, zip5)
    row.identity_flags = []
    return row


def attach_report(session: Session, report: Report, address: str, apn: str | None = None, fips: str | None = None, zip5: str | None = None) -> Property:
    property_row = resolve_property(session, address, apn, fips, zip5)
    report.property_id = property_row.id
    return property_row


def _coerce_property(session: Session, value: Property | UUID) -> Property:
    if isinstance(value, Property):
        return value
    row = session.get(Property, value)
    if row is None:
        raise ValueError(f"unknown property: {value}")
    return row


def merge(session: Session, source: Property | UUID, target: Property | UUID) -> Property:
    # Soft pointer + report re-parenting; every move is recorded so unmerge()
    # can restore it. Never hard-deletes.
    source_row = _coerce_property(session, source)
    target_row = _coerce_property(session, target)
    if source_row.id == target_row.id:
        raise ValueError("cannot merge a property into itself")
    if source_row.merged_into_id is not None:
        raise ValueError("source property is already merged")
    if target_row.merged_into_id is not None:
        raise ValueError("target property is itself merged into another property")
    source_row.merged_into_id = target_row.id
    reports = session.execute(select(Report).where(Report.property_id == source_row.id)).scalars().all()
    for report in reports:
        report.property_id = target_row.id
        session.add(MergeReportMove(source_property_id=source_row.id, target_property_id=target_row.id, report_id=report.id))
    session.flush()
    return target_row


def unmerge(session: Session, property_row: Property | UUID, enqueue: Callable[[str, dict], object] | None = None) -> Property:
    # Reverses merge(): clears the soft pointer, re-parents reports that were
    # moved by merge(), and (when enqueue is given) enqueues a recompute for both.
    row = _coerce_property(session, property_row)
    target_id = row.merged_into_id
    if target_id is None:
        raise ValueError("property is not merged")
    row.merged_into_id = None
    moves = session.execute(
        select(MergeReportMove).where(MergeReportMove.source_property_id == row.id, MergeReportMove.restored_at.is_(None))
    ).scalars().all()
    for move in moves:
        report = session.get(Report, move.report_id)
        if report is not None and report.property_id == move.target_property_id:
            report.property_id = row.id
        move.restored_at = datetime.now(timezone.utc)
    session.flush()
    if enqueue is not None:
        for property_id in {row.id, target_id}:
            enqueue("recompute_property", {"property_id": str(property_id), "reason": "unmerge"})
    return row
