import json
import logging
import sys
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "property_id", "report_id", "unit_id", "job_id", "job_name",
            "job_status", "batch_id", "document_path", "storage_backend",
            "eligible_units", "queued_jobs", "claimable_jobs", "claimable_types",
            "request_method", "request_path", "transaction_status", "dedupe_key",
            "report_status_before", "report_status_after", "report_statuses",
            "unit_status", "unit_statuses", "batch_status_before", "batch_status_after",
            "is_scanned", "ocr_backend", "ocr_backend_available", "ocr_applied",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
