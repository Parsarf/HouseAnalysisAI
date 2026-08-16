import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

logger = logging.getLogger(__name__)


REPORT_TYPES = ("property_profile", "owner_report", "mortgage", "foreclosure", "lien", "bankruptcy", "tax", "comparables", "valuation", "listing_history", "ownership_history", "rental", "combined", "unknown")


@dataclass(frozen=True)
class Signature:
    pattern: str
    report_type: str
    vendor: str | None = None
    priority: int = 0


# Fallback rules used when the document_signatures table is unavailable or
# empty. Mirrors the seeded rows; the table is the primary source at runtime.
DEFAULT_SIGNATURES = (
    Signature(r"foreclosure|notice of trustee|NOD|NTS", "foreclosure"),
    Signature(r"mortgage|deed of trust", "mortgage"),
    Signature(r"lien|judgment", "lien"),
    Signature(r"bankruptcy|chapter 7|chapter 13", "bankruptcy"),
    Signature(r"comparables|comparative market", "comparables"),
    Signature(r"tax information|assessed value", "tax"),
    Signature(r"listing history|MLS", "listing_history"),
    Signature(r"owner information|ownership", "owner_report"),
)

# Kept for backwards compatibility with callers that imported the raw rules.
RULES = [(signature.pattern, signature.report_type) for signature in DEFAULT_SIGNATURES]


@dataclass(frozen=True)
class Classification:
    report_type: str
    vendor: str | None
    confidence: float
    match_rate: float = 0.0


SignatureSource = Callable[[], Iterable]


def _coerce_signature(row) -> Signature | None:
    if isinstance(row, Signature):
        return row
    try:
        if isinstance(row, Mapping):
            if not row.get("is_active", True):
                return None
            return Signature(str(row["pattern"]), str(row["report_type"]), row.get("vendor"), int(row.get("priority") or 0))
        pattern, report_type, *rest = row
        vendor = rest[0] if rest else None
        priority = int(rest[1]) if len(rest) > 1 else 0
        return Signature(str(pattern), str(report_type), vendor, priority)
    except (KeyError, TypeError, ValueError):
        return None


def _load_db_signatures() -> list[dict] | None:
    try:
        from sqlalchemy import text

        from common.db import db_session

        with db_session() as session:
            rows = session.execute(text("SELECT pattern, report_type, vendor, priority FROM document_signatures WHERE is_active ORDER BY priority DESC, id")).mappings().all()
        return [dict(row) for row in rows]
    except Exception as exc:  # noqa: BLE001 - DB unavailable/migrated differently -> fall back
        logger.warning("document_signatures unavailable, using built-in rules: %s", exc)
        return None


def load_signatures(source: SignatureSource | Iterable | None = None) -> list[Signature]:
    """Load active signatures, newest-configuration first.

    ``source`` is injectable for tests: a callable (or plain iterable) returning
    rows as dicts, ``Signature`` objects, or ``(pattern, report_type[, vendor[, priority]])``
    tuples. With no source, the ``document_signatures`` table is queried. Any
    failure or an empty result falls back to ``DEFAULT_SIGNATURES``.
    """
    rows: Iterable | None
    if source is None:
        rows = _load_db_signatures()
    else:
        try:
            rows = source() if callable(source) else source
        except Exception as exc:  # noqa: BLE001 - injected source failed -> fall back
            logger.warning("signature source failed, using built-in rules: %s", exc)
            rows = None
    signatures = []
    for row in rows or []:
        signature = _coerce_signature(row)
        if signature is None:
            continue
        try:
            re.compile(signature.pattern)
        except re.error:
            logger.warning("invalid document_signatures pattern skipped: %r", signature.pattern)
            continue
        signatures.append(signature)
    return signatures or list(DEFAULT_SIGNATURES)


def _confidence(match_rate: float, first_page_hit: bool) -> float:
    confidence = 0.5 + 0.4 * match_rate + (0.05 if first_page_hit else 0.0)
    return round(min(0.95, confidence), 4)


def classify(text: str, filename: str = "", *, pages: Sequence[str] | None = None, signature_source: SignatureSource | Iterable | None = None) -> Classification:
    """Classify a document by matching signatures per page.

    Every active signature is matched against each page (the filename is part
    of page 1, per spec §4.4). The winning report type is the one whose
    signature has the highest per-page match rate, tie-broken by priority, so
    a single stray mention on one page no longer outranks a document-wide
    pattern. Confidence scales with the match rate.
    """
    if pages is None:
        pages = text.split("\f") if text else []
    haystacks = [str(page) for page in pages]
    if not haystacks and (text or filename):
        haystacks = [text[:4000] if text else ""]
    if not haystacks:
        return Classification("unknown", None, 0.0)
    if filename:
        haystacks[0] = f"{filename}\n{haystacks[0]}"
    signatures = load_signatures(signature_source)
    best: tuple[float, int, int, Signature] | None = None
    for order, signature in enumerate(signatures):
        regex = re.compile(signature.pattern, re.IGNORECASE)
        hits = sum(1 for haystack in haystacks if regex.search(haystack))
        if hits == 0:
            continue
        match_rate = hits / len(haystacks)
        # Higher match rate wins; then priority; then table order (stable).
        score = (match_rate, signature.priority, -order)
        if best is None or score > best[:3]:
            best = (match_rate, signature.priority, -order, signature)
    if best is None:
        return Classification("unknown", None, 0.0)
    match_rate, _, _, signature = best
    first_page_hit = bool(re.search(signature.pattern, haystacks[0], re.IGNORECASE))
    return Classification(signature.report_type, signature.vendor, _confidence(match_rate, first_page_hit), round(match_rate, 4))
