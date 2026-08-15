from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session


ENQUEUE_SQL = text("""
INSERT INTO jobs (id, name, payload, dedupe_key, max_attempts)
VALUES (:id, :name, CAST(:payload AS jsonb), :dedupe_key, :max_attempts)
ON CONFLICT (dedupe_key) DO UPDATE SET name = jobs.name
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


class PostgresJobQueue:
    """Production queue primitive. Claiming is atomic and concurrency-safe."""

    def enqueue(self, session: Session, name: str, payload: str, dedupe_key: str, max_attempts: int = 5) -> UUID:
        row = session.execute(ENQUEUE_SQL, {"id": uuid4(), "name": name, "payload": payload, "dedupe_key": dedupe_key, "max_attempts": max_attempts}).mappings().one()
        return row["id"]

    def claim(self, session: Session) -> dict | None:
        row = session.execute(CLAIM_SQL).mappings().first()
        return dict(row) if row else None

    def fail(self, session: Session, job_id: UUID, attempts: int, max_attempts: int, error: str) -> None:
        dead = attempts >= max_attempts
        delay = min(3600, 2 ** attempts)
        session.execute(text("""
          UPDATE jobs SET status=:status, last_error=:error,
            run_after=:run_after, locked_at=NULL, completed_at=CASE WHEN :dead THEN now() ELSE NULL END
          WHERE id=:id
        """), {"status": "dead" if dead else "queued", "error": error, "run_after": datetime.now(timezone.utc) + timedelta(seconds=delay), "dead": dead, "id": job_id})

    def complete(self, session: Session, job_id: UUID) -> None:
        session.execute(text("UPDATE jobs SET status='complete', locked_at=NULL, completed_at=now() WHERE id=:id"), {"id": job_id})
