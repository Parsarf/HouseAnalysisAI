import logging
import time

from common.logging import configure_logging
from common.settings import settings

from .worker import default_worker

log = logging.getLogger(__name__)


def main() -> None:
    configure_logging()
    worker = default_worker()
    log.info("worker startup", extra={"storage_backend": settings.storage_backend,
                                      "document_path": str(settings.document_root)})
    recovered = worker.recover_stale()
    if recovered:
        log.warning("recovered stale running jobs", extra={"job_id": f"count:{recovered}"})
    last_idle_log = 0.0
    while True:
        try:
            worked = worker.run_once()
        except Exception:
            log.exception("worker polling failed")
            time.sleep(5)
            continue
        if worked:
            continue
        now = time.monotonic()
        if now - last_idle_log >= 60:
            claimable = worker.claimable_summary()
            log.info("queue poll", extra={
                "claimable_jobs": sum(claimable.values()),
                "claimable_types": claimable,
            })
            last_idle_log = now
        if not worked:
            time.sleep(1)


if __name__ == "__main__":
    main()
