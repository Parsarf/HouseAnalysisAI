"""Owner-profile identity matching and persistence."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy.orm import Session

from db import models as dbm

if TYPE_CHECKING:
    from report_analysis.schemas import OwnerProfileExtraction

from .service import (
    OwnerLinkCandidate,
    normalize_owner_name,
    owner_link_candidates,
)


def persist_owner_profile(
    session: Session, extraction: OwnerProfileExtraction, *, report: dbm.Report,
) -> tuple[dbm.Owner, list[OwnerLinkCandidate]]:
    person = extraction.person
    candidates = owner_link_candidates(session, person.full_name, person.mailing_address, report.file_path)
    owner = dbm.Owner(
        id=uuid4(), full_name=person.full_name,
        name_normalized=normalize_owner_name(person.full_name), entity_type="person",
        mailing_address=person.mailing_address, age=person.age, gender=person.gender,
    )
    session.add(owner)
    session.flush()
    seen_contacts: set[tuple[str, str, str]] = set()
    for index, contact in enumerate(extraction.contacts, start=1):
        kind = contact.kind.casefold()
        source = contact.source or "skip_trace"
        identity = (kind, contact.value.strip().casefold(), source.casefold())
        if identity in seen_contacts:
            continue
        seen_contacts.add(identity)
        session.add(dbm.OwnerContact(
            owner_id=owner.id, kind=kind, value=contact.value.strip(),
            rank=contact.rank or index, source=source,
            confidence=Decimal(str(contact.confidence)) if contact.confidence is not None else None,
        ))
    bankruptcies = sorted(
        extraction.bankruptcies,
        key=lambda item: item.filing_date or date.max,
    )
    for sequence, event in enumerate(bankruptcies, start=1):
        session.add(dbm.BankruptcyEvent(
            owner_id=owner.id, property_id=None, chapter=event.chapter,
            case_number=event.case_number, court=event.court,
            filing_date=event.filing_date,
            status=event.status.casefold() if event.status else None,
            discharge_date=event.discharge_date,
            filing_sequence=sequence, is_repeat=sequence > 1,
        ))
    for lien in extraction.liens:
        session.add(dbm.Lien(
            owner_id=owner.id, property_id=None, lien_type=(lien.type or "other").casefold(),
            creditor_raw=lien.holder, amount=Decimal(str(lien.amount)) if lien.amount is not None else None,
            recording_date=lien.recorded_date,
            recording_doc_number=lien.document_number,
            status=(lien.status or "unknown").casefold(),
            attachment_basis="owner_named_only",
            attachment_confidence=Decimal(str(
                lien.confidence if lien.confidence is not None else 0.5,
            )),
            confidence=Decimal(str(lien.confidence)) if lien.confidence is not None else None,
        ))
    session.flush()
    return owner, candidates
