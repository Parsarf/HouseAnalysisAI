from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    DUPLICATE = "duplicate"
    EXTRACTION_FAILED = "extraction_failed"
    GROUNDING_FAILED = "grounding_failed"
    BUDGET_PAUSED = "budget_paused"
    CONFLICT = "conflict"
    LOCKED = "locked"
    INTERNAL = "internal"


class AcqError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}
