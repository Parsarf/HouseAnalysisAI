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
    ENCRYPTED = "encrypted"
    CORRUPT = "corrupt"
    NOT_PDF = "not_pdf"
    PARTIAL_OCR = "partial_ocr"
    UNCLASSIFIED = "unclassified"
    SECTION_UNMATCHED = "section_unmatched"
    IDENTITY_CONFLICT = "identity_conflict"
    IDENTITY_UNRESOLVED = "identity_unresolved"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    CONFLICTING_MORTGAGE = "conflicting_mortgage"
    INVALID_GROUNDING = "invalid_grounding"
    RETRY_EXHAUSTED = "retry_exhausted"
    OCR_LOW_CONFIDENCE = "ocr_low_confidence"


FailureCode = ErrorCode


class AcqError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}
