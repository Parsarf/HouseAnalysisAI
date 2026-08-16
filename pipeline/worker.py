import logging
from collections.abc import Callable

from common.db import db_session
from jobs.postgres import PostgresJobQueue
from ingestion.worker import ingest_document

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, handlers: dict[str, Callable[[dict], None]]):
        self.handlers = handlers
        self.queue = PostgresJobQueue()

    def run_once(self) -> bool:
        with db_session() as session:
            job = self.queue.claim(session)
            if not job:
                return False
            try:
                self.handlers[job["name"]](job["payload"])
            except Exception as exc:
                self.queue.fail(session, job["id"], job["attempts"], job["max_attempts"], str(exc))
                log.exception("job failed", extra={"job_id": job["id"]})
            else:
                self.queue.complete(session, job["id"])
            return True


def default_worker() -> Worker:
    return Worker({"ingest_document": ingest_document})
