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
            "event", "stage", "success", "property_id", "report_id", "unit_id",
            "job_id", "job_name",
            "job_status", "batch_id", "document_path", "storage_backend",
            "eligible_units", "queued_jobs", "claimable_jobs", "claimable_types",
            "request_method", "request_path", "transaction_status", "dedupe_key",
            "report_status_before", "report_status_after", "report_statuses",
            "unit_status", "unit_statuses", "batch_status_before", "batch_status_after",
            "is_scanned", "ocr_backend", "ocr_backend_available", "ocr_applied",
            "ocr_started", "ocr_completed", "ocr_partial", "median_text_chars",
            "empty_page_ratio", "report_created", "previous_batch_id", "final_batch_id",
            "file_count", "file_size", "sha256", "storage_operation", "page_count",
            "detected_report_type", "vendor", "confidence", "section_count",
            "unit_count", "existing_unit_count", "units_created", "attempt",
            "max_attempts", "retry_count", "model", "model_used", "provider_host",
            "provider_status_code", "unit_type", "token_estimate", "error_type",
            "error_message", "total_count", "completed_count", "failed_count",
            "report_count", "estimated_cost_usd", "jobs_inserted", "fact_count",
            "inactive_fact_count", "outstanding_units", "transitioned", "final_status",
            "unit_ids", "eligible_unit_count", "excluded_unit_statuses",
            "unit_statuses_before",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if hasattr(record, "upload_filename"):
            payload["filename"] = record.upload_filename
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
