import base64
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import analysis as analysis_store
from api import routes_chat, routes_properties
from api.app import app
from api.deps import get_queue, get_session
from auth.dependencies import make_session
from chat import ChatTurn
from common.settings import settings
from contracts import (
    AddressBlock,
    EquityBlock,
    NormalizedProperty,
    Scenario,
    UnderwritingResult,
    ValueBlock,
)
from db import models as dbm
from outreach import Draft
from report_analysis.normalizer import (
    canonical_to_normalized,
    identity_address,
    underwrite_canonical,
    validate_and_normalize,
)
from report_analysis.provider import (
    PermanentProviderError,
    ProviderAnalysis,
    ProviderTimeout,
    WholePdfProviderClient,
)
from report_analysis.schemas import PropertyReportExtraction, canonical_schema
from report_analysis.service import analyze_report


def canonical_payload(**overrides):
    payload = {
        "property_identity": {
            "address_line1": "57 Cottage Ln", "city": "Aliso Viejo", "state": "CA",
            "zip5": "92656", "full_address": "57 Cottage Ln, Aliso Viejo, CA 92656",
            "apn": "931-762-13", "county": "Orange", "fips": None,
        },
        "property_details": {
            "property_type": "condominium", "beds": 3, "baths": 2.5, "sq_ft": 1789,
            "lot_sq_ft": None, "lot_acres": None, "year_built": 1994, "units": 1,
            "garage_spaces": 2, "zoning": None, "subdivision": None,
            "legal_description": None,
        },
        "ownership": {
            "owner_names": ["MARLENE C LEWIS"], "mailing_address": None,
            "transfer_date": None, "purchase_amount": None, "transfer_type": None,
            "owner_occupied": None,
        },
        "valuation": {
            "estimated_value": 1198501, "estimated_value_as_of": "2026-03-01",
            "estimated_value_confidence": 0.88, "assessed_value": None,
            "land_value": None, "improvement_value": None,
            "comparable_sales_value": None, "comparable_listing_value": None,
            "reported_equity": None,
        },
        "tax": {
            "annual_taxes": 9258, "tax_rate": None, "tax_year": 2025,
            "tax_rate_area": None,
        },
        "loans": [{
            "position": 1, "original_amount": None, "estimated_balance": 626383,
            "recorded_date": None, "document_number": None, "lender": None,
            "status": "active", "source_page": 1, "confidence": 0.9,
        }],
        "liens": [],
        "foreclosure": {
            "in_foreclosure": True, "stage": "Auction", "trustee_sale_number": None,
            "current_sale_date": None, "original_sale_date": "3/19/2026",
            "sale_time": None, "sale_place": None, "published_bid": 626383,
            "opening_bid": None, "winning_bid": None, "default_amount": None,
            "trustee": None, "trustee_phone": None, "source_page": 1, "confidence": 0.9,
        },
        "transaction_history": [{
            "type": "transfer", "date": "2020-01-02", "document_number": "2020-1",
            "party_names": ["MARLENE C LEWIS"], "amount": 700000,
            "source_page": 1, "confidence": 0.85,
        }],
        "listing_history": [],
        "rental": {"estimated_rent": None, "rent_per_sq_ft": None},
        "additional_facts": [],
        "source_references": [{
            "field_path": "property_identity.address_line1", "source_page": 1,
            "confidence": 0.99, "evidence": "57 COTTAGE LN",
        }],
    }
    payload.update(overrides)
    return payload


def owner_payload():
    return {
        "person": {
            "full_name": "LEWIS,MARLENE C", "age": 72, "gender": "female",
            "mailing_address": "2549 EASTBLUFF DR # 279, NEWPORT BEACH, CA 92660",
        },
        "contacts": [
            {"kind": "phone", "value": "949-555-0101", "rank": 1,
             "source": "skip_trace", "confidence": 0.91},
            {"kind": "email", "value": "marlene@example.com", "rank": 1,
             "source": "skip_trace", "confidence": 0.85},
            {"kind": "email", "value": "emanuellewis@hotmail.com", "rank": 2,
             "source": "skip_trace", "confidence": 0.42},
        ],
        "bankruptcies": [
            {"chapter": "13", "case_number": "A", "court": "CACB",
             "filing_date": "2018-05-10", "status": "Dismissed", "discharge_date": None},
            {"chapter": "13", "case_number": "B", "court": "CACB",
             "filing_date": "2026-03-19", "status": "Dismissed", "discharge_date": None},
        ],
        "liens": [{
            "type": "federal_tax", "amount": 140294, "recorded_date": "2026-05-05",
            "document_number": "LIEN-1", "holder": "IRS", "status": "open",
            "confidence": 0.95,
        }],
        "source_references": [],
    }


def _assert_strict(node, path="$", *, root=None):
    root = node if root is None else root
    if not isinstance(node, dict):
        return
    if "$ref" in node:
        target = root
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        _assert_strict(target, node["$ref"], root=root)
        return
    if node.get("type") == "object" or "properties" in node:
        assert set(node.get("required", [])) == set(node.get("properties", {})), path
        assert node.get("additionalProperties") is False, path
    for key, value in node.items():
        if key == "$defs":
            continue
        if isinstance(value, dict):
            _assert_strict(value, f"{path}.{key}", root=root)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                _assert_strict(item, f"{path}.{key}[{index}]", root=root)


def test_canonical_schema_is_recursively_strict_and_nullable():
    schema = canonical_schema()
    _assert_strict(schema)
    options = schema["$defs"]["Valuation"]["properties"]["estimated_value"]["anyOf"]
    assert {"number", "null"} == {option["type"] for option in options}


def test_sparse_extra_and_missing_foreclosure_data_are_safe():
    payload = canonical_payload()
    payload["valuation"]["estimated_value"] = None
    payload["valuation"]["estimated_value_as_of"] = None
    payload["tax"]["annual_taxes"] = None
    payload["loans"] = []
    payload["foreclosure"]["current_sale_date"] = None
    payload["additional_facts"] = [{
        "category": "legal", "label": "Unusual notice", "value": "Recorded notice",
        "numeric_value": None, "date_value": None, "source_page": 1, "confidence": 0.8,
    }]
    result = validate_and_normalize(payload)
    assert result.extraction.valuation.estimated_value is None
    assert result.extraction.loans == []
    assert result.extraction.foreclosure.current_sale_date is None
    assert result.extraction.additional_facts[0].label == "Unusual notice"
    normalized = canonical_to_normalized(result.extraction, uuid4())
    assert normalized.valuation_candidates == []
    assert normalized.mortgages == []


def test_condo_common_parcel_lot_is_not_used_for_unit():
    payload = canonical_payload()
    payload["property_details"]["property_type"] = "CND"
    payload["property_details"]["lot_sq_ft"] = 407747
    payload["property_details"]["lot_acres"] = 9.36
    result = validate_and_normalize(payload)
    assert result.extraction.property_details.lot_sq_ft is None
    assert result.extraction.property_details.lot_acres is None
    assert any(issue["code"] == "condo_common_parcel_lot_ignored" for issue in result.issues)


def test_apn_candidate_from_legal_description_is_rejected():
    payload = canonical_payload()
    payload["property_identity"]["apn"] = "639-062-15"
    payload["property_details"]["legal_description"] = "TRACT 123 AP 639-062-15"
    result = validate_and_normalize(payload)
    assert result.extraction.property_identity.apn is None
    assert any(issue["code"] == "apn_legal_description_collision" for issue in result.issues)


def test_independently_sourced_apn_is_not_rejected_when_legal_repeats_it():
    payload = canonical_payload()
    payload["property_identity"]["apn"] = "931-762-13"
    payload["property_details"]["legal_description"] = "UNIT 57 AP 931-762-13"
    payload["source_references"].append({
        "field_path": "property_identity.apn", "source_page": 1,
        "confidence": 0.99, "evidence": "Property summary APN field",
    })
    result = validate_and_normalize(payload)
    assert result.extraction.property_identity.apn == "931-762-13"


def test_cancelled_listing_is_preserved():
    payload = canonical_payload()
    payload["listing_history"] = [{
        "type": "listing", "status": "cancelled", "as_of": "2026-05-20",
        "dom": 42, "price": 999000, "source_page": 4, "confidence": 0.95,
    }]
    normalized = canonical_to_normalized(validate_and_normalize(payload).extraction, uuid4())
    assert normalized.listings[0].status == "cancelled"
    assert normalized.listings[0].price.value == Decimal(999000)


def test_missing_debt_is_null_and_missing_valuation_never_creates_equity():
    assumptions = json.loads(Path("fixtures/assumptions/default.json").read_text())
    from contracts import AssumptionSet

    missing_debt = canonical_payload()
    missing_debt["loans"] = []
    missing_debt["foreclosure"] = {
        key: None for key in missing_debt["foreclosure"]
    }
    record = canonical_to_normalized(
        validate_and_normalize(missing_debt).extraction, uuid4(),
    )
    result = underwrite_canonical(record, AssumptionSet.model_validate(assumptions))
    assert result.status == "insufficient_data"
    assert result.unavailable_reason == "missing_debt_data"
    assert result.liabilities.confirmed is None
    assert result.equity == {}

    missing_value = canonical_payload()
    missing_value["valuation"] = {
        key: None for key in missing_value["valuation"]
    }
    record = canonical_to_normalized(
        validate_and_normalize(missing_value).extraction, uuid4(),
    )
    result = underwrite_canonical(record, AssumptionSet.model_validate(assumptions))
    assert result.status == "insufficient_data"
    assert result.unavailable_reason == "no_valuation_candidates"
    assert result.equity == {}


def test_identity_requires_grounded_street_address_and_never_fabricates():
    extraction = PropertyReportExtraction.model_validate(canonical_payload())
    assert identity_address(extraction) == "57 Cottage Ln"
    payload = canonical_payload()
    payload["property_identity"] = {
        **payload["property_identity"], "address_line1": None, "full_address": None,
    }
    assert identity_address(PropertyReportExtraction.model_validate(payload)) is None


def test_canonical_normalization_does_not_invent_attachment_or_report_freshness():
    report_date = date(2026, 2, 1)
    payload = canonical_payload()
    payload["ownership"]["owner_occupied"] = False
    payload["liens"] = [{
        "type": " Judgment ", "amount": 25000, "recorded_date": "1/2/2025",
        "document_number": None, "holder": None, "status": " SATISFIED ",
        "source_page": 2, "confidence": 0.8,
    }]
    payload["foreclosure"]["current_sale_date"] = "2026-12-15"
    validated = validate_and_normalize(payload)
    record = canonical_to_normalized(validated.extraction, uuid4(), report_date=report_date)

    assert record.liens[0].attachment_basis.value == "unknown"
    assert record.liens[0].status == "satisfied"
    assert record.ownership.is_absentee is True
    # A future auction is an event, not evidence that the source report is future-dated.
    assert record.data_quality.newest_report_date == report_date


def test_canonical_normalization_rejects_nonfinite_nonpositive_and_bad_years():
    payload = canonical_payload()
    payload["valuation"]["estimated_value"] = float("inf")
    payload["property_details"]["sq_ft"] = 0
    payload["property_details"]["year_built"] = 1200
    result = validate_and_normalize(payload)

    assert result.extraction.valuation.estimated_value is None
    assert result.extraction.property_details.sq_ft is None
    assert result.extraction.property_details.year_built is None
    assert {issue["code"] for issue in result.issues} >= {
        "nonfinite_value_rejected", "nonpositive_value_rejected", "implausible_year_rejected",
    }


def test_provider_sends_original_pdf_to_responses_api(tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4\nwhole document")
    calls = []

    def transport(method, url, headers, body, timeout):
        request = json.loads(body)
        calls.append((method, url, headers, request, timeout))
        return 200, {
            "model": "gpt-4o-mini", "output_text": json.dumps(canonical_payload()),
            "usage": {"input_tokens": 1000, "output_tokens": 500},
        }

    result = WholePdfProviderClient(
        api_key="test", transport=transport, sleep=lambda _: None,
    ).analyze_pdf(pdf)
    request = calls[0][3]
    assert calls[0][1].endswith("/responses")
    assert request["input"][0]["content"][0]["type"] == "input_file"
    file_data = request["input"][0]["content"][0]["file_data"]
    prefix = "data:application/pdf;base64,"
    assert file_data.startswith(prefix)
    assert base64.b64decode(file_data.removeprefix(prefix)) == pdf.read_bytes()
    assert request["text"]["format"]["strict"] is True
    assert "temperature" not in request
    assert result.payload["property_identity"]["apn"] == "931-762-13"
    assert result.cost_usd == Decimal("0.000450")


def test_provider_timeout_retries_then_fails_and_400_is_permanent(tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4\nwhole document")
    timeout_calls = []

    def timeout_transport(*args):
        timeout_calls.append(args)
        raise TimeoutError("slow provider")

    client = WholePdfProviderClient(
        api_key="test", transport=timeout_transport, max_retries=3,
        timeout=12, sleep=lambda _: None,
    )
    with pytest.raises(ProviderTimeout):
        client.analyze_pdf(pdf)
    assert len(timeout_calls) == 3
    assert {call[-1] for call in timeout_calls} == {12}

    rejected = []

    def reject(*args):
        rejected.append(args)
        return 400, {"error": {"message": "invalid schema"}}

    with pytest.raises(PermanentProviderError):
        WholePdfProviderClient(
            api_key="test", transport=reject, max_retries=5, sleep=lambda _: None,
        ).analyze_pdf(pdf)
    assert len(rejected) == 1


class MemoryQueue:
    def __init__(self):
        self.jobs = {}

    def enqueue(self, session, name, payload, dedupe_key, max_attempts=5):
        row = self.jobs.get(dedupe_key)
        if row is None:
            row = {"id": uuid4(), "name": name, "payload": payload, "status": "queued"}
            self.jobs[dedupe_key] = row
        elif row["status"] not in {"queued", "running"}:
            row.update(payload=payload, status="queued")
        return row["id"]


class StubProvider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def analyze_pdf(self, path, *, log_context=None):
        self.calls += 1
        assert Path(path).read_bytes().startswith(b"%PDF-")
        return ProviderAnalysis(
            self.payload, "gpt-4o-mini", 1000, 500, Decimal("0.000450"), 100, 1,
        )


def _create_property_table(engine):
    columns = """
      id CHAR(32) PRIMARY KEY, apn TEXT, apn_key TEXT, fips_county TEXT,
      address_line1 TEXT, city TEXT, state TEXT, zip5 TEXT, address_key TEXT,
      address_hash TEXT, lat NUMERIC, lng NUMERIC, property_type TEXT, beds NUMERIC,
      baths NUMERIC, sqft NUMERIC, lot_sqft NUMERIC, year_built INTEGER, units INTEGER,
      pipeline_status TEXT, tags TEXT, next_action TEXT, next_action_date DATE,
      gut_rating INTEGER, is_watchlisted BOOLEAN, merged_into_id CHAR(32),
      underwriting_status TEXT, last_recomputed_at DATETIME, archived_at DATETIME, created_at DATETIME,
      updated_at DATETIME
    """
    with engine.begin() as connection:
        connection.exec_driver_sql(f"CREATE TABLE properties ({columns})")


@pytest.fixture()
def whole_pdf_harness(monkeypatch, tmp_path):
    sqlite3.register_adapter(UUID, lambda value: value.hex)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    _create_property_table(engine)
    for table in (
        dbm.Batch.__table__, dbm.Report.__table__, dbm.ReportExtraction.__table__,
        dbm.ExtractionUnit.__table__, dbm.ExtractedFact.__table__, dbm.Owner.__table__,
        dbm.PropertyOwner.__table__, dbm.OwnerContact.__table__, dbm.Lien.__table__,
        dbm.BankruptcyEvent.__table__, dbm.ForeclosureEvent.__table__,
        dbm.ChangeEvent.__table__, dbm.OfferScenario.__table__, dbm.PropertyNote.__table__,
        dbm.Setting.__table__, dbm.Valuation.__table__,
    ):
        table.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def transaction():
        with factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def api_session():
        with transaction() as session:
            yield session

    def resolve_identity(session, report, address, apn=None, fips=None, zip5=None):
        row = session.query(dbm.Property).filter(
            dbm.Property.address_line1 == address,
            dbm.Property.merged_into_id.is_(None),
        ).first()
        created = row is None
        if row is None:
            property_id = uuid4()
            session.execute(text("""
                INSERT INTO properties
                  (id, address_line1, apn, fips_county, zip5, pipeline_status,
                   is_watchlisted, created_at, updated_at)
                VALUES (:id, :address, :apn, :fips, :zip5, 'new', false,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """), {"id": property_id.hex, "address": address, "apn": apn,
                    "fips": fips, "zip5": zip5})
            row = session.get(dbm.Property, property_id)
        row.identity_created = created
        row.identity_flags = []
        report.property_id = row.id
        return row

    queue = MemoryQueue()
    monkeypatch.setattr(settings, "analysis_pipeline", "whole_pdf")
    monkeypatch.setattr(settings, "document_root", tmp_path / "documents")
    monkeypatch.setattr(settings, "storage_backend", "filesystem")
    monkeypatch.setattr(analysis_store, "load_underwriting", lambda *args: None)
    monkeypatch.setattr(analysis_store, "load_strategies", lambda *args: [])
    monkeypatch.setattr(analysis_store, "load_offers", lambda *args: None)
    monkeypatch.setattr(analysis_store, "load_scores", lambda *args: None)
    monkeypatch.setattr(analysis_store, "load_flags", lambda *args: [])
    monkeypatch.setattr(analysis_store, "load_timeline", lambda *args: [])
    monkeypatch.setattr(routes_properties, "_latest_scores", lambda *args: {})
    monkeypatch.setattr(routes_properties, "_latest_ranks", lambda *args: {})
    monkeypatch.setattr(routes_properties, "_open_flag_counts", lambda *args: {})
    app.dependency_overrides[get_session] = api_session
    app.dependency_overrides[get_queue] = lambda: queue
    client = TestClient(app)
    client.cookies.set("session_cookie", make_session("owner", False, settings.session_secret))
    yield SimpleNamespace(
        client=client, queue=queue, transaction=transaction, factory=factory,
        resolve_identity=resolve_identity,
    )
    app.dependency_overrides.clear()


def _upload(client, content=b"%PDF-1.4\nrepresentative property profile"):
    response = client.post(
        "/api/uploads",
        files=[("files", ("report.pdf", content, "application/pdf"))],
    )
    assert response.status_code == 200
    return response.json()


def test_whole_pdf_upload_to_property_analysis_and_duplicate_reuse(whole_pdf_harness):
    first = _upload(whole_pdf_harness.client)
    report_id = UUID(first["report_ids"][0])
    assert whole_pdf_harness.queue.jobs[f"analyze:{report_id}"]["name"] == "analyze_report"
    provider = StubProvider(canonical_payload())
    computations = []

    def compute(record, **kwargs):
        computations.append(record)
        return SimpleNamespace(underwriting=SimpleNamespace(status="ok"))

    property_id = analyze_report(
        report_id, batch_id=UUID(first["batch_id"]), provider=provider,
        session_factory=whole_pdf_harness.transaction, compute=compute,
        identity_resolver=whole_pdf_harness.resolve_identity,
    )
    assert property_id is not None
    with whole_pdf_harness.factory() as session:
        report = session.get(dbm.Report, report_id)
        extraction = session.query(dbm.ReportExtraction).filter_by(report_id=report_id).one()
        assert report.property_id == property_id
        assert report.status == "complete"
        assert extraction.status == "complete"
        assert extraction.raw_json["property_identity"]["apn"] == "931-762-13"
        assert extraction.normalized_json["property"]["property_id"] == str(property_id)
        assert session.query(dbm.ExtractionUnit).count() == 0
    batch = whole_pdf_harness.client.get(f"/api/batches/{first['batch_id']}").json()
    assert batch["status"] == "complete"
    assert batch["results"][0]["property_id"] == str(property_id)
    portfolio = whole_pdf_harness.client.get("/api/properties").json()
    assert [item["id"] for item in portfolio["items"]] == [str(property_id)]
    analysis = whole_pdf_harness.client.get(f"/api/properties/{property_id}/analysis").json()
    assert analysis["normalized"]["apn"] == "931-762-13"
    assert analysis["normalized"]["valuation_candidates"][0]["value"]["value"] == "1198501.0"
    assert isinstance(computations[0], NormalizedProperty)

    second = _upload(whole_pdf_harness.client)
    duplicate_report_id = UUID(second["report_ids"][0])
    assert duplicate_report_id != report_id
    duplicate_property = analyze_report(
        duplicate_report_id, batch_id=UUID(second["batch_id"]), provider=provider,
        session_factory=whole_pdf_harness.transaction, compute=compute,
        identity_resolver=whole_pdf_harness.resolve_identity,
    )
    assert duplicate_property == property_id
    assert provider.calls == 1
    duplicate_batch = whole_pdf_harness.client.get(f"/api/batches/{second['batch_id']}").json()
    assert duplicate_batch["status"] == "complete"
    assert duplicate_batch["property_ids"] == [str(property_id)]
    original_batch = whole_pdf_harness.client.get(f"/api/batches/{first['batch_id']}").json()
    assert original_batch["status"] == "complete"
    assert original_batch["property_ids"] == [str(property_id)]
    with whole_pdf_harness.factory() as session:
        duplicate_report = session.get(dbm.Report, duplicate_report_id)
        duplicate_extraction = session.query(dbm.ReportExtraction).filter_by(
            report_id=duplicate_report_id,
        ).one()
        assert duplicate_report.duplicate_of == report_id
        assert duplicate_report.batch_id == UUID(second["batch_id"])
        assert duplicate_extraction.raw_json == extraction.raw_json
        assert duplicate_extraction.cost_usd == Decimal(0)


def test_owner_profile_extracts_all_rows_and_requires_link_confirmation(
    whole_pdf_harness, monkeypatch,
):
    target_owner_id = uuid4()
    property_id = uuid4()
    with whole_pdf_harness.transaction() as session:
        session.execute(text("""
            INSERT INTO properties
              (id, address_line1, pipeline_status, is_watchlisted, created_at, updated_at)
            VALUES (:id, '57 Cottage Ln', 'new', false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """), {"id": property_id.hex})
        session.add(dbm.Owner(
            id=target_owner_id, full_name="MARLENE C LEWIS", name_normalized="MARLENE LEWIS",
            entity_type="person",
            mailing_address="2549 Eastbluff Dr 279, Newport Beach CA 92660",
        ))
        session.add(dbm.PropertyOwner(
            property_id=property_id, owner_id=target_owner_id, is_current=True,
        ))
        session.add(dbm.ForeclosureEvent(
            property_id=property_id, event_type="nts", event_date=date(2026, 2, 3),
            current_sale_date=date(2026, 3, 19), stage_after_event="auction",
        ))

    uploaded = _upload(whole_pdf_harness.client, b"%PDF-1.4\nowner profile")
    report_id = UUID(uploaded["report_ids"][0])
    monkeypatch.setattr(
        "report_analysis.service.classify_pdf", lambda _path: ("owner_profile", 0.98),
    )
    computed = []
    result = analyze_report(
        report_id, batch_id=UUID(uploaded["batch_id"]), provider=StubProvider(owner_payload()),
        session_factory=whole_pdf_harness.transaction,
        compute=lambda *args, **kwargs: computed.append(args),
    )
    assert result is None
    assert computed == []

    pending = whole_pdf_harness.client.get("/api/owner-profiles/unlinked")
    assert pending.status_code == 200
    (profile,) = pending.json()["items"]
    assert profile["owner_name"] == "LEWIS,MARLENE C"
    (candidate,) = profile["link_candidates"]
    assert candidate["owner_id"] == str(target_owner_id)
    assert candidate["confidence"] == "high"
    assert candidate["reasons"] == ["normalized_name", "mailing_address"]

    linked = whole_pdf_harness.client.post(
        f"/api/owner-profiles/{report_id}/link", json={"owner_id": str(target_owner_id)},
    )
    assert linked.status_code == 200
    assert linked.json()["linked"] is True
    assert whole_pdf_harness.client.get("/api/owner-profiles/unlinked").json()["items"] == []
    with whole_pdf_harness.factory() as session:
        report = session.get(dbm.Report, report_id)
        assert report.doc_kind == "owner_profile"
        assert report.property_id is None
        target_owner = session.get(dbm.Owner, target_owner_id)
        assert target_owner is not None
        assert (target_owner.age, target_owner.gender) == (72, "female")
        assert session.query(dbm.OwnerContact).filter_by(owner_id=target_owner_id).count() == 3
        bankruptcies = session.query(dbm.BankruptcyEvent).filter_by(
            owner_id=target_owner_id,
        ).order_by(dbm.BankruptcyEvent.filing_sequence).all()
        assert [(row.filing_sequence, row.is_repeat) for row in bankruptcies] == [
            (1, False), (2, True),
        ]
        (lien,) = session.query(dbm.Lien).filter_by(owner_id=target_owner_id).all()
        assert lien.property_id is None
        assert lien.attachment_basis == "owner_named_only"

    profile_response = whole_pdf_harness.client.get(
        f"/api/properties/{property_id}/owner-profile",
    )
    assert profile_response.status_code == 200
    profile_data = profile_response.json()
    assert len(profile_data["contacts"]) == 3
    assert profile_data["owner_lien_total"] == "140294.00"
    assert profile_data["serial_filing"] == {
        "dismissed_count": 2, "near_scheduled_sale": True, "window_days": 7,
    }
    assert [item["kind"] for item in profile_data["timeline"]] == [
        "bankruptcy", "foreclosure", "bankruptcy",
    ]
    normalized = NormalizedProperty(
        property_id=property_id, address=AddressBlock(), resolution_version="test",
    )
    underwriting = UnderwritingResult(
        property_id=property_id, assumption_set_id=uuid4(), engine_version="test", status="ok",
        value=ValueBlock(v_expected=Decimal("1198501")),
        equity={Scenario.EXPECTED: EquityBlock(adjusted=Decimal("572118"))},
    )
    monkeypatch.setattr(analysis_store, "load_normalized", lambda *args: normalized)
    monkeypatch.setattr(analysis_store, "load_underwriting", lambda *args: underwriting)
    analysis = whole_pdf_harness.client.get(f"/api/properties/{property_id}/analysis").json()
    assert analysis["owner_profile"]["owner_liens_included_in_underwriting"] is False
    assert analysis["owner_profile"]["equity_if_owner_liens_attach"] == "431824.00"


def test_property_profile_creates_owner_identity_used_by_owner_profile_review(
    whole_pdf_harness, monkeypatch,
):
    property_upload = _upload(whole_pdf_harness.client, b"%PDF-1.4\nproperty profile")
    property_report_id = UUID(property_upload["report_ids"][0])
    property_payload = canonical_payload()
    property_payload["ownership"]["owner_names"] = ["MARLENE C LEWIS"]
    property_payload["ownership"]["mailing_address"] = (
        "2549 Eastbluff Dr 279, Newport Beach CA 92660"
    )
    analyze_report(
        property_report_id,
        batch_id=UUID(property_upload["batch_id"]),
        provider=StubProvider(property_payload),
        session_factory=whole_pdf_harness.transaction,
        compute=lambda *_args, **_kwargs: SimpleNamespace(
            underwriting=SimpleNamespace(status="ok"),
        ),
        identity_resolver=whole_pdf_harness.resolve_identity,
    )
    with whole_pdf_harness.factory() as session:
        property_row = session.get(dbm.Report, property_report_id)
        assert property_row is not None
        property_id = property_row.property_id
        assert property_id is not None
        (canonical_owner,) = session.query(dbm.Owner).all()
        assert canonical_owner.name_normalized == "MARLENE LEWIS"
        assert session.get(dbm.PropertyOwner, (property_id, canonical_owner.id)) is not None

    owner_upload = _upload(whole_pdf_harness.client, b"%PDF-1.4\nowner profile")
    owner_report_id = UUID(owner_upload["report_ids"][0])
    monkeypatch.setattr(
        "report_analysis.service.classify_pdf", lambda _path: ("owner_profile", 0.98),
    )
    analyze_report(
        owner_report_id,
        batch_id=UUID(owner_upload["batch_id"]),
        provider=StubProvider(owner_payload()),
        session_factory=whole_pdf_harness.transaction,
        compute=lambda *_args, **_kwargs: SimpleNamespace(
            underwriting=SimpleNamespace(status="ok"),
        ),
    )
    (candidate,) = whole_pdf_harness.client.get(
        "/api/owner-profiles/unlinked",
    ).json()["items"][0]["link_candidates"]
    assert candidate["owner_id"] == str(canonical_owner.id)
    assert candidate["property_ids"] == [str(property_id)]
    assert candidate["confidence"] == "high"


def test_owner_profile_arriving_first_still_requires_review_after_property_arrives(
    whole_pdf_harness, monkeypatch,
):
    monkeypatch.setattr(
        "report_analysis.service.classify_pdf", lambda _path: ("owner_profile", 0.98),
    )
    owner_upload = _upload(whole_pdf_harness.client, b"%PDF-1.4\nowner first")
    owner_report_id = UUID(owner_upload["report_ids"][0])
    analyze_report(
        owner_report_id, batch_id=UUID(owner_upload["batch_id"]),
        provider=StubProvider(owner_payload()),
        session_factory=whole_pdf_harness.transaction,
    )
    assert whole_pdf_harness.client.get(
        "/api/owner-profiles/unlinked",
    ).json()["items"][0]["link_candidates"] == []

    monkeypatch.setattr(
        "report_analysis.service.classify_pdf", lambda _path: ("property_profile", 0.98),
    )
    property_upload = _upload(whole_pdf_harness.client, b"%PDF-1.4\nproperty second")
    property_payload = canonical_payload()
    property_payload["ownership"]["mailing_address"] = (
        "2549 Eastbluff Dr 279, Newport Beach CA 92660"
    )
    analyze_report(
        UUID(property_upload["report_ids"][0]),
        batch_id=UUID(property_upload["batch_id"]),
        provider=StubProvider(property_payload),
        session_factory=whole_pdf_harness.transaction,
        compute=lambda *_args, **_kwargs: SimpleNamespace(
            underwriting=SimpleNamespace(status="ok"),
        ),
        identity_resolver=whole_pdf_harness.resolve_identity,
    )
    pending = whole_pdf_harness.client.get("/api/owner-profiles/unlinked").json()["items"]
    (candidate,) = pending[0]["link_candidates"]
    assert candidate["confidence"] == "high"
    assert candidate["owner_id"] != pending[0]["owner_id"]
    with whole_pdf_harness.factory() as session:
        # The owner-profile record remains unlinked until the explicit review action.
        assert session.query(dbm.Owner).count() == 2
        assert session.query(dbm.PropertyOwner).count() == 1


def test_archive_filters_portfolio_but_direct_url_and_restore_remain_available(
    whole_pdf_harness,
):
    property_id = uuid4()
    with whole_pdf_harness.transaction() as session:
        session.execute(text("""
            INSERT INTO properties
              (id, address_line1, pipeline_status, is_watchlisted, created_at, updated_at)
            VALUES (:id, '10 Archive Way', 'new', false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """), {"id": property_id.hex})

    archived = whole_pdf_harness.client.post(f"/api/properties/{property_id}/archive")
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    assert whole_pdf_harness.client.get("/api/properties").json()["items"] == []
    archived_items = whole_pdf_harness.client.get(
        "/api/properties?show_archived=true",
    ).json()["items"]
    assert [item["id"] for item in archived_items] == [str(property_id)]
    assert archived_items[0]["archived_at"] is not None
    assert whole_pdf_harness.client.get(f"/api/properties/{property_id}").status_code == 200
    recompute = whole_pdf_harness.client.post(f"/api/properties/{property_id}/recompute")
    assert recompute.status_code == 400
    assert recompute.json()["error"]["message"] == "restore this property before recomputing it"

    restored = whole_pdf_harness.client.post(f"/api/properties/{property_id}/restore")
    assert restored.status_code == 200
    assert [item["id"] for item in whole_pdf_harness.client.get(
        "/api/properties",
    ).json()["items"]] == [str(property_id)]
    with whole_pdf_harness.factory() as session:
        changes = session.query(dbm.ChangeEvent).filter_by(property_id=property_id).all()
        assert [row.change_type for row in changes] == ["archived", "restored"]


def test_outreach_draft_uses_grid_contacts_and_persists_edits(
    whole_pdf_harness, monkeypatch,
):
    property_id = uuid4()
    owner_id = uuid4()
    with whole_pdf_harness.transaction() as session:
        session.execute(text("""
            INSERT INTO properties
              (id, address_line1, city, state, zip5, beds, baths, pipeline_status,
               is_watchlisted, created_at, updated_at)
            VALUES (:id, '57 Cottage Ln', 'Aliso Viejo', 'CA', '92656', 3, 2.5,
                    'new', false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """), {"id": property_id.hex})
        session.add(dbm.Owner(
            id=owner_id, full_name="MARLENE C LEWIS", name_normalized="MARLENE LEWIS",
            entity_type="person", mailing_address="2549 Eastbluff Dr, Newport Beach CA",
        ))
        session.add(dbm.PropertyOwner(
            property_id=property_id, owner_id=owner_id, is_current=True,
        ))
        session.add_all([
            dbm.OwnerContact(owner_id=owner_id, kind="email", value="marlene@example.com",
                             rank=1, source="skip_trace", confidence=Decimal("0.85")),
            dbm.OwnerContact(owner_id=owner_id, kind="email", value="relative@example.com",
                             rank=2, source="skip_trace", confidence=Decimal("0.40")),
        ])
        session.add(dbm.OfferScenario(
            property_id=property_id, offer_price=Decimal("950000"), scenario="expected",
            confirmed_payoffs=Decimal("626383"), potential_payoffs=Decimal(0),
            closing_costs=Decimal("10000"), proceeds_low=Decimal("313617"),
            proceeds_expected=Decimal("313617"), proceeds_high=Decimal("313617"),
            buyer_basis=Decimal("960000"), profit=Decimal("238501"),
            roi=Decimal("0.248438"), is_short_sale=False,
        ))

    class SafeProvider:
        def generate(self, context, *, prior_draft=None, instruction=None):
            assert context["offer_price"] == Decimal("950000")
            return Draft(
                "Cash offer for 57 Cottage Ln",
                "We can offer $950,000 cash with no financing contingency and flexible timing.",
            )

    monkeypatch.setattr(routes_properties, "OutreachProviderClient", SafeProvider)
    response = whole_pdf_harness.client.post(
        f"/api/properties/{property_id}/outreach-draft", json={},
    )
    assert response.status_code == 200
    draft = response.json()
    assert draft["recipient_selected"] is None
    assert [item["value"] for item in draft["recipients"]] == [
        "marlene@example.com", "relative@example.com",
    ]
    assert draft["recipients"][1]["association_warning"]

    unsafe = whole_pdf_harness.client.patch(
        f"/api/properties/{property_id}/outreach-drafts/{draft['draft_id']}",
        json={"subject": draft["subject"], "body": "Your foreclosure prompted $950,000.",
              "status": "draft"},
    )
    assert unsafe.status_code == 400
    saved = whole_pdf_harness.client.patch(
        f"/api/properties/{property_id}/outreach-drafts/{draft['draft_id']}",
        json={"subject": draft["subject"], "body": draft["body"],
              "recipient": "marlene@example.com", "status": "sent"},
    )
    assert saved.status_code == 200
    assert saved.json()["status"] == "sent"
    assert saved.json()["sent_at"] is not None
    with whole_pdf_harness.factory() as session:
        (note,) = session.query(dbm.PropertyNote).filter_by(property_id=property_id).all()
        assert '"recipient": "marlene@example.com"' in note.body


def test_chat_stream_has_server_session_and_contacts_are_tool_only(
    whole_pdf_harness, monkeypatch,
):
    property_id = uuid4()
    owner_id = uuid4()
    with whole_pdf_harness.transaction() as session:
        session.execute(text("""
            INSERT INTO properties
              (id, address_line1, pipeline_status, is_watchlisted, created_at, updated_at)
            VALUES (:id, '57 Cottage Ln', 'new', false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """), {"id": property_id.hex})
        session.add(dbm.Owner(
            id=owner_id, full_name="MARLENE C LEWIS", name_normalized="MARLENE LEWIS",
            entity_type="person",
        ))
        session.add(dbm.PropertyOwner(
            property_id=property_id, owner_id=owner_id, is_current=True,
        ))
        session.add(dbm.OwnerContact(
            owner_id=owner_id, kind="email", value="marlene@example.com",
            rank=1, source="skip_trace", confidence=Decimal("0.85"),
        ))

    class ToolAwareProvider:
        def __init__(self):
            self.contexts = []
            self.owner_tool_results = []

        def complete(self, messages, structured_context, tool_results, **kwargs):
            self.contexts.append(structured_context)
            gathered = {}
            if "owner" in messages[-1]["content"].casefold():
                gathered["get_owner_profile"] = kwargs["execute_tool"](
                    "get_owner_profile", {"property_id": str(property_id)},
                )
                self.owner_tool_results.append(gathered["get_owner_profile"])
            return ChatTurn(
                "The supplied record supports that answer.", 40, 12,
                Decimal("0.001"), "fake", gathered,
            )

    provider = ToolAwareProvider()
    monkeypatch.setattr(routes_chat, "ChatProviderClient", lambda: provider)
    first = whole_pdf_harness.client.post("/api/chat", json={
        "messages": [{"role": "user", "content": "Summarize this property"}],
        "property_ids": [str(property_id)],
    })
    assert first.status_code == 200
    done = [line for line in first.text.splitlines() if '"done": true' in line]
    assert len(done) == 1
    session_id = json.loads(done[0].removeprefix("data: "))["session_id"]
    assert "marlene@example.com" not in json.dumps(provider.contexts[0], default=str)
    assert "Eastbluff" not in json.dumps(provider.contexts[0], default=str)

    second = whole_pdf_harness.client.post("/api/chat", json={
        "session_id": session_id,
        "messages": [{"role": "user", "content": "What owner contact is available?"}],
        "property_ids": [str(property_id)],
    })
    assert second.status_code == 200
    assert provider.owner_tool_results[0]["contacts"][0]["value"] == "marlene@example.com"


def test_multiple_reports_in_one_batch_resolve_to_one_property(whole_pdf_harness):
    response = whole_pdf_harness.client.post(
        "/api/uploads",
        files=[
            ("files", ("profile.pdf", b"%PDF-1.4\nproperty profile", "application/pdf")),
            ("files", ("notice.pdf", b"%PDF-1.4\nforeclosure notice", "application/pdf")),
        ],
    )
    assert response.status_code == 200
    uploaded = response.json()
    report_ids = [UUID(value) for value in uploaded["report_ids"]]
    provider = StubProvider(canonical_payload())

    for index, report_id in enumerate(report_ids):
        analyze_report(
            report_id, batch_id=UUID(uploaded["batch_id"]), provider=provider,
            session_factory=whole_pdf_harness.transaction,
            compute=lambda *args, **kwargs: SimpleNamespace(
                underwriting=SimpleNamespace(status="ok"),
            ),
            identity_resolver=whole_pdf_harness.resolve_identity,
        )
        batch = whole_pdf_harness.client.get(
            f"/api/batches/{uploaded['batch_id']}",
        ).json()
        assert batch["status"] == ("analyzing" if index == 0 else "complete")

    with whole_pdf_harness.factory() as session:
        reports = session.query(dbm.Report).filter(
            dbm.Report.id.in_(report_ids),
        ).all()
        assert len({report.property_id for report in reports}) == 1
        assert session.query(dbm.Property).count() == 1
        assert session.query(dbm.ReportExtraction).filter(
            dbm.ReportExtraction.report_id.in_(report_ids),
        ).count() == 2
        assert session.query(dbm.ExtractionUnit).count() == 0
    assert provider.calls == 2


def test_whole_pdf_no_identity_is_visible_and_preserved(whole_pdf_harness):
    uploaded = _upload(whole_pdf_harness.client, b"%PDF-1.4\nno identity")
    payload = canonical_payload()
    payload["property_identity"] = {
        **payload["property_identity"], "address_line1": None, "full_address": None,
    }
    report_id = UUID(uploaded["report_ids"][0])
    result = analyze_report(
        report_id, batch_id=UUID(uploaded["batch_id"]), provider=StubProvider(payload),
        session_factory=whole_pdf_harness.transaction,
        compute=lambda *args, **kwargs: pytest.fail("unresolved report was computed"),
        identity_resolver=whole_pdf_harness.resolve_identity,
    )
    assert result is None
    batch = whole_pdf_harness.client.get(f"/api/batches/{uploaded['batch_id']}").json()
    assert batch["status"] == "unresolved_identity"
    assert batch["unresolved_reports"][0]["identity"]["apn"] == "931-762-13"
    with whole_pdf_harness.factory() as session:
        extraction = session.query(dbm.ReportExtraction).filter_by(report_id=report_id).one()
        assert extraction.raw_json is not None
        assert extraction.status == "unresolved_identity"


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("ACQ_TEST_POSTGRES_URL"),
    reason="ACQ_TEST_POSTGRES_URL is required for the PostgreSQL whole-PDF integration test",
)
def test_real_postgres_report_job_resolves_property_without_units(tmp_path):
    from ingestion import register_pdf
    from jobs.postgres import PostgresJobQueue

    engine = create_engine(os.environ["ACQ_TEST_POSTGRES_URL"], pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def transaction():
        with factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    pdf = tmp_path / "postgres-whole.pdf"
    pdf.write_bytes(b"%PDF-1.4\nreal postgres whole document")
    batch_id = uuid4()
    queue = PostgresJobQueue()
    with transaction() as session:
        session.add(dbm.Batch(
            id=batch_id, name="whole-pdf-postgres", file_count=1,
            total_count=1, status="analyzing",
        ))
        report, _created = register_pdf(
            session, pdf, tmp_path / "documents", batch_id=batch_id,
        )
        job_id = queue.enqueue(
            session, "analyze_report",
            json.dumps({"report_id": str(report.id), "batch_id": str(batch_id)}),
            f"analyze:{report.id}",
        )
        report_id = report.id

    property_id = None
    try:
        with transaction() as session:
            job = queue.claim(session)
            assert job["id"] == job_id
            assert job["name"] == "analyze_report"
        property_id = analyze_report(
            report_id, batch_id=batch_id, provider=StubProvider(canonical_payload()),
            session_factory=transaction,
            compute=lambda *args, **kwargs: SimpleNamespace(
                underwriting=SimpleNamespace(status="ok"),
            ),
        )
        with transaction() as session:
            report = session.get(dbm.Report, report_id)
            batch = session.get(dbm.Batch, batch_id)
            extraction = session.query(dbm.ReportExtraction).filter_by(
                report_id=report_id,
            ).one()
            assert property_id is not None
            assert report.property_id == property_id
            assert report.status == "complete"
            assert extraction.status == "complete"
            assert batch.status == "complete"
            assert session.query(dbm.ExtractionUnit).filter_by(report_id=report_id).count() == 0
    finally:
        with transaction() as session:
            session.query(dbm.ExtractedFact).filter_by(report_id=report_id).delete()
            session.query(dbm.ReportExtraction).filter_by(report_id=report_id).delete()
            session.query(dbm.Job).filter_by(id=job_id).delete()
            session.query(dbm.Report).filter_by(id=report_id).delete()
            if property_id is not None:
                session.query(dbm.Property).filter_by(id=property_id).delete()
            session.query(dbm.Batch).filter_by(id=batch_id).delete()
