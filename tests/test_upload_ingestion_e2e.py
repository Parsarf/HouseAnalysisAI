"""Upload-to-ingestion regressions using real API, ORM, worker, and S3 adapters."""

import importlib
import json
import os
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.app import app
from api.deps import get_queue, get_session
from auth.dependencies import make_session
from common.settings import settings
from common.storage import S3Storage
from db.models import Batch, ExtractionUnit, Job, Report
from ingestion import register_pdf
from ingestion import worker as ingestion_worker
from jobs.postgres import PostgresJobQueue
from pipeline import worker as pipeline_worker


class MemoryS3Client:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}

    def upload_file(self, filename, bucket, key):
        self.objects[(bucket, key)] = Path(filename).read_bytes()

    def put_object(self, *, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, *, Bucket, Key):
        return {"Body": BytesIO(self.objects[(Bucket, Key)])}

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[(bucket, key)])

    def list_objects_v2(self, *, Bucket, Prefix):
        return {
            "Contents": [
                {"Key": key}
                for bucket, key in self.objects
                if bucket == Bucket and key.startswith(Prefix)
            ]
        }


class TransactionalQueue:
    """Small PostgresJobQueue equivalent for cross-session API/worker tests."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}

    def enqueue(self, session, name, payload, dedupe_key, max_attempts=5):
        job = self.jobs.get(dedupe_key)
        if job is None:
            job = {
                "id": uuid4(), "name": name, "payload": payload,
                "dedupe_key": dedupe_key, "status": "queued", "attempts": 0,
                "max_attempts": max_attempts,
            }
            self.jobs[dedupe_key] = job
        elif job["status"] not in ("queued", "running"):
            job.update(payload=payload, status="queued", attempts=0)
        return job["id"]

    def claim(self, session):
        job = next((row for row in self.jobs.values() if row["status"] == "queued"), None)
        if job is None:
            return None
        job["status"] = "running"
        job["attempts"] += 1
        return dict(job)

    def complete(self, session, job_id):
        next(row for row in self.jobs.values() if row["id"] == job_id)["status"] = "complete"

    def fail(self, session, job_id, attempts, max_attempts, error):
        job = next(row for row in self.jobs.values() if row["id"] == job_id)
        job["status"] = "dead" if attempts >= max_attempts else "queued"
        job["last_error"] = error

    def recover_stale(self, session):
        return 0

    def claimable_summary(self, session):
        return {"ingest_document": sum(row["status"] == "queued" for row in self.jobs.values())}


def digital_pdf_bytes() -> bytes:
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_textbox(
        fitz.Rect(36, 36, 560, 760),
        "123 Main Street property valuation and parcel report. " * 25,
    )
    data = document.tobytes()
    document.close()
    return data


@pytest.fixture()
def upload_ingestion_harness(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Batch.__table__.create(engine)
    Report.__table__.create(engine)
    ExtractionUnit.__table__.create(engine)
    Job.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    storage = object.__new__(S3Storage)
    storage.bucket = "test-documents"
    storage.client = MemoryS3Client()
    queue = TransactionalQueue()

    @contextmanager
    def worker_session():
        with factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def api_session():
        with worker_session() as session:
            yield session

    api_app = importlib.import_module("api.app")
    monkeypatch.setattr(settings, "analysis_pipeline", "legacy")
    monkeypatch.setattr(settings, "document_root", tmp_path / "documents")
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(api_app, "get_document_storage", lambda: storage)
    monkeypatch.setattr(ingestion_worker, "get_document_storage", lambda: storage)
    monkeypatch.setattr(ingestion_worker, "db_session", worker_session)
    monkeypatch.setattr(pipeline_worker, "db_session", worker_session)
    app.dependency_overrides[get_session] = api_session
    app.dependency_overrides[get_queue] = lambda: queue

    client = TestClient(app)
    client.cookies.set("session_cookie", make_session("owner", False, settings.session_secret))
    worker = pipeline_worker.Worker(
        {"ingest_document": pipeline_worker._handle_ingest_document},
        queue=queue, session_factory=worker_session,
    )
    yield SimpleNamespace(
        client=client, queue=queue, worker=worker, session_factory=factory,
        storage=storage, pdf=digital_pdf_bytes(),
    )
    app.dependency_overrides.clear()


def upload(harness, name):
    response = harness.client.post(
        "/api/uploads",
        files=[("files", ("report.pdf", harness.pdf, "application/pdf"))],
        data={"batch_name": name},
    )
    assert response.status_code == 200
    return response.json()


def assert_batch_uploaded(harness, batch_id, report_id):
    assert harness.worker.run_once() is True
    response = harness.client.get(f"/api/batches/{batch_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "uploaded"
    with harness.session_factory() as session:
        report_uuid = UUID(report_id)
        report = session.get(Report, report_uuid)
        units = session.query(ExtractionUnit).filter(
            ExtractionUnit.report_id == report_uuid
        ).all()
        assert str(report.batch_id) == batch_id
        assert report.file_path.startswith("s3://test-documents/")
        assert report.status == "classified"
        assert units
        return len(units)


def test_fresh_unique_pdf_ingests_estimates_and_starts_extraction(
    upload_ingestion_harness, caplog,
):
    with caplog.at_level("INFO"):
        result = upload(upload_ingestion_harness, "fresh")
        report_id = result["report_ids"][0]
        assert_batch_uploaded(upload_ingestion_harness, result["batch_id"], report_id)

    report_id = result["report_ids"][0]
    job = upload_ingestion_harness.queue.jobs[f"ingest:{report_id}"]
    assert json.loads(job["payload"]) == {"report_id": report_id}
    assert job["status"] == "complete"
    with upload_ingestion_harness.session_factory() as session:
        units = session.query(ExtractionUnit).filter(
            ExtractionUnit.report_id == UUID(report_id)
        ).all()
        assert units
        assert {unit.status for unit in units} == {"queued"}
        unit_ids = {str(unit.id) for unit in units}
        assert {str(unit.report_id) for unit in units} == {report_id}

    with caplog.at_level("INFO"):
        estimate = upload_ingestion_harness.client.post(
            f"/api/batches/{result['batch_id']}/estimate"
        )
        assert estimate.status_code == 200
        started = upload_ingestion_harness.client.post(
            f"/api/batches/{result['batch_id']}/start"
        )
    assert started.status_code == 200
    assert started.json()["status"] == "running"
    extract_jobs = [
        queued for queued in upload_ingestion_harness.queue.jobs.values()
        if queued["name"] == "extract_unit"
    ]
    assert {json.loads(queued["payload"])["unit_id"] for queued in extract_jobs} == unit_ids
    assert {queued["status"] for queued in extract_jobs} == {"queued"}
    sectioned = next(record for record in caplog.records
                     if getattr(record, "event", None) == "sectioning_completed")
    assert sectioned.section_count == 1
    assert sectioned.existing_unit_count == 0
    assert str(sectioned.batch_id) == result["batch_id"]
    assert str(sectioned.report_id) == report_id
    created = next(record for record in caplog.records
                   if getattr(record, "event", None) == "units_created")
    assert created.units_created == 1
    assert set(created.unit_ids) == unit_ids
    assert created.unit_statuses == {"queued": 1}
    eligible = next(record for record in caplog.records
                    if getattr(record, "event", None) == "extraction_units_eligible")
    assert eligible.eligible_unit_count == 1
    assert set(eligible.unit_ids) == unit_ids
    assert eligible.unit_statuses == {"queued": 1}
    assert eligible.excluded_unit_statuses == {}
    events = {getattr(record, "event", None) for record in caplog.records}
    required = {
        "upload_received", "file_saved", "report_registered", "ingest_job_created",
        "worker_job_claimed", "document_materialized", "pdf_opened", "scan_detected",
        "classification_completed", "sectioning_completed", "units_created",
        "report_status_transition", "ingestion_transaction_committed",
        "batch_status_returned",
    }
    assert required <= events
    correlated = [
        record for record in caplog.records
        if getattr(record, "event", None) in required and hasattr(record, "batch_id")
    ]
    assert correlated
    assert all(str(record.batch_id) == result["batch_id"] for record in correlated)


def test_identical_pdf_reupload_as_new_batch_progresses_to_uploaded(
    upload_ingestion_harness, caplog,
):
    first = upload(upload_ingestion_harness, "first")
    report_id = first["report_ids"][0]
    original_unit_count = assert_batch_uploaded(
        upload_ingestion_harness, first["batch_id"], report_id,
    )

    with caplog.at_level("INFO"):
        second = upload(upload_ingestion_harness, "second")
    assert second["batch_id"] != first["batch_id"]
    assert second["report_ids"] == [report_id]
    assert upload_ingestion_harness.queue.jobs[f"ingest:{report_id}"]["status"] == "queued"

    assert assert_batch_uploaded(
        upload_ingestion_harness, second["batch_id"], report_id,
    ) == original_unit_count
    registered = next(
        record for record in reversed(caplog.records)
        if getattr(record, "event", None) == "report_registered"
    )
    assert registered.report_created is False
    assert str(registered.previous_batch_id) == first["batch_id"]
    assert str(registered.final_batch_id) == second["batch_id"]
    requeued = next(
        record for record in reversed(caplog.records)
        if getattr(record, "event", None) == "ingest_job_created"
    )
    assert str(requeued.batch_id) == second["batch_id"]
    assert str(requeued.report_id) == report_id


def test_reingested_nonqueued_units_are_eligible_for_start(upload_ingestion_harness):
    first = upload(upload_ingestion_harness, "first-extracted")
    report_id = first["report_ids"][0]
    assert_batch_uploaded(upload_ingestion_harness, first["batch_id"], report_id)
    with upload_ingestion_harness.session_factory() as session:
        original_units = session.query(ExtractionUnit).filter(
            ExtractionUnit.report_id == UUID(report_id)
        ).all()
        assert original_units
        for unit in original_units:
            unit.status = "extracted"
        original_unit_ids = {str(unit.id) for unit in original_units}
        session.commit()

    second = upload(upload_ingestion_harness, "second-reingested")
    assert second["report_ids"] == [report_id]
    assert_batch_uploaded(upload_ingestion_harness, second["batch_id"], report_id)
    with upload_ingestion_harness.session_factory() as session:
        persisted_units = session.query(ExtractionUnit).filter(
            ExtractionUnit.report_id == UUID(report_id)
        ).all()
        assert {str(unit.id) for unit in persisted_units} == original_unit_ids
        assert {unit.status for unit in persisted_units} == {"queued"}

    estimate = upload_ingestion_harness.client.post(
        f"/api/batches/{second['batch_id']}/estimate"
    )
    assert estimate.status_code == 200
    started = upload_ingestion_harness.client.post(
        f"/api/batches/{second['batch_id']}/start"
    )
    assert started.status_code == 200
    extract_jobs = [
        queued for queued in upload_ingestion_harness.queue.jobs.values()
        if queued["name"] == "extract_unit"
    ]
    assert {json.loads(queued["payload"])["unit_id"] for queued in extract_jobs} == original_unit_ids


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("ACQ_TEST_POSTGRES_URL"),
    reason="ACQ_TEST_POSTGRES_URL is required for the PostgreSQL queue integration test",
)
def test_real_postgres_queue_claims_fresh_and_duplicate_ingestion_jobs(monkeypatch, tmp_path):
    engine = create_engine(os.environ["ACQ_TEST_POSTGRES_URL"], pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    storage = object.__new__(S3Storage)
    storage.bucket = "test-documents"
    storage.client = MemoryS3Client()
    queue = PostgresJobQueue()
    pdf = tmp_path / "postgres.pdf"
    pdf.write_bytes(digital_pdf_bytes())
    batch_ids = [uuid4(), uuid4()]
    report_id = None
    job_id = None

    @contextmanager
    def database_session():
        with factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    monkeypatch.setattr(ingestion_worker, "get_document_storage", lambda: storage)
    monkeypatch.setattr(ingestion_worker, "db_session", database_session)
    monkeypatch.setattr(pipeline_worker, "db_session", database_session)
    worker = pipeline_worker.Worker(
        {"ingest_document": pipeline_worker._handle_ingest_document},
        queue=queue, session_factory=database_session,
    )

    try:
        with database_session() as session:
            session.add(Batch(
                id=batch_ids[0], name="postgres-fresh", file_count=1,
                total_count=1, status="ingesting",
            ))
            report, created = register_pdf(
                session, pdf, tmp_path / "unused", batch_id=batch_ids[0], storage=storage,
            )
            assert created is True
            report_id = report.id
            job_id = queue.enqueue(
                session, "ingest_document", json.dumps({"report_id": str(report.id)}),
                f"ingest:{report.id}",
            )

        with factory() as session:
            assert session.get(Job, job_id).status == "queued"
        assert worker.run_once() is True
        with factory() as session:
            assert session.get(Job, job_id).status == "complete"
            assert session.get(Report, report_id).status == "classified"
            assert session.get(Batch, batch_ids[0]).status == "uploaded"
            original_units = session.query(ExtractionUnit).filter(
                ExtractionUnit.report_id == report_id
            ).count()
            assert original_units > 0

        with database_session() as session:
            session.add(Batch(
                id=batch_ids[1], name="postgres-duplicate", file_count=1,
                total_count=1, status="ingesting",
            ))
            duplicate, created = register_pdf(
                session, pdf, tmp_path / "unused", batch_id=batch_ids[1], storage=storage,
            )
            assert created is False
            assert duplicate.id == report_id
            assert duplicate.batch_id == batch_ids[1]
            assert queue.enqueue(
                session, "ingest_document", json.dumps({"report_id": str(report_id)}),
                f"ingest:{report_id}",
            ) == job_id

        with factory() as session:
            assert session.get(Job, job_id).status == "queued"
        assert worker.run_once() is True
        with factory() as session:
            assert session.get(Job, job_id).status == "complete"
            assert session.get(Report, report_id).status == "classified"
            assert session.get(Report, report_id).batch_id == batch_ids[1]
            assert session.get(Batch, batch_ids[1]).status == "uploaded"
            assert session.query(ExtractionUnit).filter(
                ExtractionUnit.report_id == report_id
            ).count() == original_units
    finally:
        if report_id is not None:
            with database_session() as session:
                session.query(ExtractionUnit).filter(
                    ExtractionUnit.report_id == report_id
                ).delete(synchronize_session=False)
                session.query(Report).filter(Report.id == report_id).delete(
                    synchronize_session=False,
                )
                session.query(Job).filter(Job.id == job_id).delete(synchronize_session=False)
                session.query(Batch).filter(Batch.id.in_(batch_ids)).delete(
                    synchronize_session=False,
                )
