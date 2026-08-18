import logging
import time

from common.logging import configure_logging
from common.settings import settings
from ingestion.ocr import get_backend

from .worker import default_worker

log = logging.getLogger(__name__)


def main() -> None:
    configure_logging()
    worker = default_worker()
    startup = {"event": "worker_startup",
               "stage": "worker_startup",
               "storage_backend": settings.storage_backend,
               "document_path": str(settings.document_root),
               "analysis_pipeline": settings.analysis_pipeline}
    if settings.analysis_pipeline == "legacy":
        ocr_backend = get_backend()
        startup.update(ocr_backend=ocr_backend.name,
                       ocr_backend_available=ocr_backend.available())
    log.info("worker startup", extra=startup)
    recovered = worker.recover_stale()
    if recovered:
        log.warning("recovered stale running jobs", extra={"event": "stale_jobs_recovered",
                                                            "stage": "queue_recovery",
                                                            "job_id": f"count:{recovered}"})
    last_idle_log = 0.0
    while True:
        try:
            worked = worker.run_once()
        except Exception as error:
            log.exception("worker polling failed", extra={
                "event": "worker_poll_failed",
                "stage": "queue_poll",
                "success": False,
                "error_type": type(error).__name__,
                "error_message": str(error),
            })
            time.sleep(5)
            continue
        if worked:
            continue
        now = time.monotonic()
        if now - last_idle_log >= 60:
            claimable = worker.claimable_summary()
            log.info("queue poll", extra={
                "event": "queue_poll",
                "stage": "queue_poll",
                "claimable_jobs": sum(claimable.values()),
                "claimable_types": claimable,
            })
            last_idle_log = now
        if not worked:
            time.sleep(1)


if __name__ == "__main__":
    main()
