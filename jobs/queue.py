from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable
from uuid import UUID, uuid4


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    DEAD = "dead"


@dataclass
class Job:
    name: str
    payload: dict
    dedupe_key: str | None = None
    id: UUID = field(default_factory=uuid4)
    status: JobStatus = JobStatus.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class InMemoryJobQueue:
    """Deterministic development queue; production adapter uses Postgres SKIP LOCKED."""
    def __init__(self):
        self.jobs: list[Job] = []

    def enqueue(self, name: str, payload: dict, dedupe_key: str | None = None) -> Job:
        if dedupe_key and any(j.dedupe_key == dedupe_key and j.status not in (JobStatus.DEAD, JobStatus.FAILED)
                              for j in self.jobs):
            return next(j for j in self.jobs if j.dedupe_key == dedupe_key)
        job = Job(name=name, payload=payload, dedupe_key=dedupe_key)
        self.jobs.append(job)
        return job

    def run_once(self, handlers: dict[str, Callable[[dict], None]]) -> Job | None:
        job = next((j for j in self.jobs if j.status == JobStatus.QUEUED), None)
        if job is None:
            return None
        job.status, job.attempts = JobStatus.RUNNING, job.attempts + 1
        try:
            handlers[job.name](job.payload)
        except Exception as exc:
            job.error = str(exc)
            job.status = JobStatus.DEAD if job.attempts >= job.max_attempts else JobStatus.QUEUED
        else:
            job.status = JobStatus.COMPLETE
        return job
