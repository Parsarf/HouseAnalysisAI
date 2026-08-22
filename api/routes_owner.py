"""Owner-profile review and property-scoped owner intelligence."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from auth.dependencies import User, current_user, write_user
from common.errors import AcqError, ErrorCode
from common.settings import settings
from db import models as dbm
from identity.service import confirm_owner_link, owner_link_candidates

from .deps import get_session
from .serializers import dump

router = APIRouter(prefix="/api", tags=["owners"])


def _owner_ids_for_property(session: Session, property_id: UUID) -> list[UUID]:
    return [row[0] for row in session.query(dbm.PropertyOwner.owner_id).filter(
        dbm.PropertyOwner.property_id == property_id,
        dbm.PropertyOwner.is_current.is_not(False),
    ).all()]


def owner_profile_payload(session: Session, property_id: UUID) -> dict:
    owner_ids = _owner_ids_for_property(session, property_id)
    owners = session.query(dbm.Owner).filter(dbm.Owner.id.in_(owner_ids)).all() if owner_ids else []
    contacts = session.query(dbm.OwnerContact).filter(
        dbm.OwnerContact.owner_id.in_(owner_ids),
    ).order_by(dbm.OwnerContact.rank.asc().nullslast(), dbm.OwnerContact.id).all() if owner_ids else []
    liens = session.query(dbm.Lien).filter(
        dbm.Lien.owner_id.in_(owner_ids), dbm.Lien.property_id.is_(None),
    ).order_by(dbm.Lien.recording_date.desc().nullslast()).all() if owner_ids else []
    bankruptcies = session.query(dbm.BankruptcyEvent).filter(
        dbm.BankruptcyEvent.owner_id.in_(owner_ids), dbm.BankruptcyEvent.property_id.is_(None),
    ).order_by(dbm.BankruptcyEvent.filing_date.asc().nullslast()).all() if owner_ids else []
    foreclosure_dates = [row[0] for row in session.query(dbm.ForeclosureEvent.current_sale_date).filter(
        dbm.ForeclosureEvent.property_id == property_id,
        dbm.ForeclosureEvent.current_sale_date.isnot(None),
    ).all()]
    dismissed = [event for event in bankruptcies if (event.status or "").casefold() == "dismissed"]
    sale_window = timedelta(days=max(0, settings.serial_filing_sale_window_days))
    near_sale = any(
        event.filing_date and sale_date
        and sale_date - sale_window <= event.filing_date <= sale_date
        for event in bankruptcies for sale_date in foreclosure_dates
    )
    timeline = [{
        "kind": "bankruptcy", "date": event.filing_date, "label": f"Chapter {event.chapter or '?'} filed",
        "status": event.status, "case_number": event.case_number,
    } for event in bankruptcies]
    timeline.extend({
        "kind": "foreclosure", "date": event.event_date or event.current_sale_date,
        "label": event.event_type or event.stage_after_event or "Foreclosure event",
        "sale_date": event.current_sale_date,
    } for event in session.query(dbm.ForeclosureEvent).filter(
        dbm.ForeclosureEvent.property_id == property_id,
    ).all())
    timeline.sort(key=lambda item: (item["date"] is None, item["date"] or ""))
    owner_by_id = {owner.id: owner for owner in owners}
    open_liens = [
        lien for lien in liens
        if (lien.status or "unknown").casefold() not in {"closed", "paid", "released", "satisfied"}
    ]
    return {
        "owners": [{
            "id": owner.id, "full_name": owner.full_name,
            "mailing_address": owner.mailing_address, "age": owner.age,
            "gender": owner.gender, "is_absentee": owner.is_absentee,
        } for owner in owners],
        "contacts": [{
            "id": contact.id, "owner_id": contact.owner_id, "kind": contact.kind,
            "value": contact.value, "rank": contact.rank, "source": contact.source,
            "confidence": contact.confidence,
            "association_warning": (
                "may be a relative or stale association"
                if contact.kind == "email"
                and (contact_owner := owner_by_id.get(contact.owner_id)) is not None
                and contact_owner.full_name.split()[0].casefold() not in contact.value.casefold()
                else None
            ),
        } for contact in contacts],
        "liens": [{
            "id": lien.id, "type": lien.lien_type, "amount": lien.amount,
            "recording_date": lien.recording_date, "status": lien.status,
            "attachment_basis": lien.attachment_basis,
        } for lien in liens],
        "bankruptcies": [{
            "id": event.id, "chapter": event.chapter, "case_number": event.case_number,
            "filing_date": event.filing_date, "status": event.status,
            "filing_sequence": event.filing_sequence, "is_repeat": event.is_repeat,
        } for event in bankruptcies],
        "serial_filing": {
            "dismissed_count": len(dismissed), "near_scheduled_sale": near_sale,
            "window_days": max(0, settings.serial_filing_sale_window_days),
        },
        "timeline": timeline,
        "owner_lien_total": sum((lien.amount or Decimal(0) for lien in open_liens), Decimal(0)),
    }


@router.get("/properties/{property_id}/owner-profile")
def get_owner_profile(property_id: UUID, session: Session = Depends(get_session),
                      user: User = Depends(current_user)) -> dict:
    if session.get(dbm.Property, property_id) is None:
        raise AcqError(ErrorCode.NOT_FOUND, "property not found")
    return dump(owner_profile_payload(session, property_id))


@router.get("/owner-profiles/unlinked")
def unlinked_owner_profiles(session: Session = Depends(get_session),
                            user: User = Depends(current_user)) -> dict:
    rows = session.query(dbm.ReportExtraction, dbm.Report).join(
        dbm.Report, dbm.Report.id == dbm.ReportExtraction.report_id,
    ).filter(
        dbm.Report.doc_kind == "owner_profile",
        dbm.Report.property_id.is_(None),
        dbm.Report.duplicate_of.is_(None),
        dbm.ReportExtraction.status == "complete",
    ).all()
    items = []
    for extraction, report in rows:
        normalized = extraction.normalized_json or {}
        if normalized.get("linked"):
            continue
        owner_id = normalized.get("owner_id")
        owner = session.get(dbm.Owner, UUID(str(owner_id))) if owner_id else None
        candidates = [{
            "owner_id": str(candidate.owner_id),
            "confidence": candidate.confidence,
            "reasons": candidate.reasons,
            "property_ids": [str(value) for value in candidate.property_ids],
        } for candidate in owner_link_candidates(
            session, owner.full_name, owner.mailing_address, report.file_path,
        ) if candidate.owner_id != owner.id] if owner else []
        candidate_ids = [UUID(str(item["owner_id"])) for item in candidates if item.get("owner_id")]
        candidate_owners = session.query(dbm.Owner).filter(
            dbm.Owner.id.in_(candidate_ids),
        ).all() if candidate_ids else []
        candidate_names = {str(row.id): row.full_name for row in candidate_owners}
        items.append({
            "report_id": extraction.report_id,
            "file_name": report.file_path.rsplit("/", 1)[-1],
            "owner_id": owner_id,
            "owner_name": owner.full_name if owner else None,
            "link_candidates": [
                {**candidate, "owner_name": candidate_names.get(str(candidate.get("owner_id")))}
                for candidate in candidates
            ],
        })
    return dump({"items": items})


@router.post("/owner-profiles/{report_id}/link")
def link_owner_profile(report_id: UUID, body: dict, session: Session = Depends(get_session),
                       user: User = Depends(write_user)) -> dict:
    extraction = session.query(dbm.ReportExtraction).filter(
        dbm.ReportExtraction.report_id == report_id,
    ).first()
    normalized = extraction.normalized_json if extraction is not None else None
    if not isinstance(normalized, dict) or not normalized.get("owner_id"):
        raise AcqError(ErrorCode.NOT_FOUND, "owner profile not found")
    assert extraction is not None
    source_owner_id = UUID(str(normalized["owner_id"]))
    try:
        target_owner_id = UUID(str(body.get("owner_id")))
    except ValueError as exc:
        raise AcqError(ErrorCode.INVALID_INPUT, "a valid owner_id is required") from exc
    source_owner = session.get(dbm.Owner, source_owner_id)
    report = session.get(dbm.Report, report_id)
    if source_owner is None or report is None:
        raise AcqError(ErrorCode.NOT_FOUND, "owner profile not found")
    candidates = owner_link_candidates(
        session,
        source_owner.full_name,
        source_owner.mailing_address,
        report.file_path,
    )
    if target_owner_id not in {
        candidate.owner_id for candidate in candidates if candidate.owner_id != source_owner_id
    }:
        raise AcqError(ErrorCode.INVALID_INPUT, "owner_id is not a reviewed link candidate")
    target = confirm_owner_link(session, source_owner_id, target_owner_id)
    extraction.normalized_json = {**normalized, "owner_id": str(target.id), "linked": True}
    return {"report_id": str(report_id), "owner_id": str(target.id), "linked": True}
