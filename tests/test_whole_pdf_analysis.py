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
from api import routes_properties
from api.app import app
from api.deps import get_queue, get_session
from auth.dependencies import make_session
from common.settings import settings
from contracts import NormalizedProperty
from db import models as dbm
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
      underwriting_status TEXT, last_recomputed_at DATETIME, created_at DATETIME,
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
        dbm.ExtractionUnit.__table__, dbm.ExtractedFact.__table__,
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
