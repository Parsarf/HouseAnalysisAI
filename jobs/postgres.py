import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

ENQUEUE_SQL = text("""
INSERT INTO jobs (id, name, payload, dedupe_key, max_attempts)
VALUES (:id, :name, CAST(:payload AS jsonb), :dedupe_key, :max_attempts)
ON CONFLICT (dedupe_key) DO UPDATE SET
  name = EXCLUDED.name,
  payload = CASE WHEN jobs.status IN ('queued','running') THEN jobs.payload ELSE EXCLUDED.payload END,
  status = CASE WHEN jobs.status IN ('queued','running') THEN jobs.status ELSE 'queued' END,
  attempts = CASE WHEN jobs.status IN ('queued','running') THEN jobs.attempts ELSE 0 END,
  run_after = CASE WHEN jobs.status IN ('queued','running') THEN jobs.run_after ELSE now() END,
  locked_at = CASE WHEN jobs.status IN ('queued','running') THEN jobs.locked_at ELSE NULL END,
  completed_at = CASE WHEN jobs.status IN ('queued','running') THEN jobs.completed_at ELSE NULL END,
  last_error = CASE WHEN jobs.status IN ('queued','running') THEN jobs.last_error ELSE NULL END
RETURNING id, status
""")

CLAIM_SQL = text("""
WITH next_job AS (
  SELECT id FROM jobs
  WHERE status = 'queued' AND run_after <= now()
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE jobs SET status='running', locked_at=now(), attempts=attempts+1
WHERE id IN (SELECT id FROM next_job)
RETURNING id, name, payload, attempts, max_attempts
""")

CLAIMABLE_SUMMARY_SQL = text("""
SELECT name, count(*) AS count
FROM jobs
WHERE status = 'queued' AND run_after <= now()
GROUP BY name
ORDER BY name
""")


@dataclass(frozen=True)
class EnqueueResult:
    id: UUID
    status: str


class PostgresJobQueue:
    """Production queue primitive. Claiming is atomic and concurrency-safe."""

    def enqueue(self, session: Session, name: str, payload: str, dedupe_key: str, max_attempts: int = 5) -> UUID:
        return self.enqueue_with_status(
            session, name, payload, dedupe_key, max_attempts=max_attempts
        ).id

    def enqueue_with_status(
        self, session: Session, name: str, payload: str, dedupe_key: str,
        max_attempts: int = 5,
    ) -> EnqueueResult:
        row = session.execute(ENQUEUE_SQL, {"id": uuid4(), "name": name, "payload": payload, "dedupe_key": dedupe_key, "max_attempts": max_attempts}).mappings().one()
        result = EnqueueResult(id=row["id"], status=row["status"])
        log.info("queue job upserted", extra={
            "event": "queue_job_upserted",
            "job_id": result.id,
            "job_name": name,
            "job_status": result.status,
            "dedupe_key": dedupe_key,
        })
        return result

    def claim(self, session: Session) -> dict | None:
        row = session.execute(CLAIM_SQL).mappings().first()
        return dict(row) if row else None

    def claimable_summary(self, session: Session) -> dict[str, int]:
        rows = session.execute(CLAIMABLE_SUMMARY_SQL).mappings().all()
        return {str(row["name"]): int(row["count"]) for row in rows}

    def recover_stale(self, session: Session, *, minutes: int = 15) -> int:
        """Release jobs orphaned by a worker crash or Railway redeploy."""
        result = session.execute(text("""
          UPDATE jobs SET status='queued', locked_at=NULL, run_after=now(),
            last_error=COALESCE(last_error, 'worker lease expired; retrying')
          WHERE status='running'
            AND (locked_at IS NULL OR locked_at < now() - (:minutes * interval '1 minute'))
        """), {"minutes": minutes})
        return int(getattr(result, "rowcount", 0) or 0)

    def fail(self, session: Session, job_id: UUID, attempts: int, max_attempts: int, error: str) -> None:
        dead = attempts >= max_attempts
        delay = min(3600, 2 ** attempts)
        session.execute(text("""
          UPDATE jobs SET status=:status, last_error=:error,
            run_after=:run_after, locked_at=NULL, completed_at=CASE WHEN :dead THEN now() ELSE NULL END
          WHERE id=:id
        """), {"status": "dead" if dead else "queued", "error": error, "run_after": datetime.now(UTC) + timedelta(seconds=delay), "dead": dead, "id": job_id})

    def complete(self, session: Session, job_id: UUID) -> None:
        session.execute(text("UPDATE jobs SET status='complete', locked_at=NULL, completed_at=now() WHERE id=:id"), {"id": job_id})
