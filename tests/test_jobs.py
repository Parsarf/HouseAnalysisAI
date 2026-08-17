from uuid import uuid4

from jobs import InMemoryJobQueue, JobStatus
from jobs.postgres import PostgresJobQueue


class MappingResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def one(self):
        return self.rows[0]

    def all(self):
        return self.rows


class RecordingSession:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((statement, params))
        return self.results.pop(0)


def test_queue_deduplicates_and_completes():
    queue = InMemoryJobQueue()
    first = queue.enqueue("demo", {"id": 1}, "same")
    assert queue.enqueue("demo", {"id": 2}, "same").id == first.id
    queue.run_once({"demo": lambda payload: None})
    assert first.status == JobStatus.COMPLETE


def test_queue_retries_then_dead_letters():
    queue = InMemoryJobQueue()
    job = queue.enqueue("bad", {})
    job.max_attempts = 2
    for _ in range(2):
        queue.run_once({"bad": lambda payload: (_ for _ in ()).throw(RuntimeError("boom"))})
    assert job.status == JobStatus.DEAD


def test_postgres_enqueue_reports_actual_upsert_status(caplog):
    job_id = uuid4()
    session = RecordingSession([MappingResult([{"id": job_id, "status": "queued"}])])

    with caplog.at_level("INFO"):
        result = PostgresJobQueue().enqueue_with_status(
            session, "extract_unit", '{"unit_id":"1"}', "extract_unit:1"
        )

    assert result.id == job_id
    assert result.status == "queued"
    record = next(record for record in caplog.records if record.message == "queue job upserted")
    assert record.job_id == job_id
    assert record.job_name == "extract_unit"
    assert record.job_status == "queued"


def test_postgres_claimable_summary_groups_due_queued_jobs():
    session = RecordingSession([MappingResult([
        {"name": "extract_unit", "count": 2},
        {"name": "ingest_document", "count": 1},
    ])])

    summary = PostgresJobQueue().claimable_summary(session)

    assert summary == {"extract_unit": 2, "ingest_document": 1}
