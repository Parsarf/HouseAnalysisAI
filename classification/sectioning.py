import re
from dataclasses import dataclass

from .tokens import estimate_tokens


@dataclass(frozen=True)
class SectionedUnit:
    unit_type: str
    page_start: int
    page_end: int
    text: str
    token_estimate: int
    matched_header: bool = True


# Header regexes per spec §5.1; unit types map to the extraction schemas.
HEADERS = {"mortgage": "mortgage|deed of trust", "foreclosure": "foreclosure|notice of trustee", "lien": "lien|judgment", "bankruptcy": "bankruptcy|chapter (7|11|13)", "tax": "tax information|assessed value", "comparables": "comparables|comparative market|comparable sales", "owner_report": "owner information|ownership"}


def _match_header(page: str) -> str | None:
    for unit_type, pattern in HEADERS.items():
        if re.search(pattern, page, re.IGNORECASE):
            return unit_type
    return None


def section_pages(pages: list[str], fallback_size: int = 3, *, overlap: int = 1) -> list[SectionedUnit]:
    """Split page text into typed extraction units (spec §5.1).

    Units open on header matches and close when the next header appears or the
    document ends. When no header matches anywhere, fall back to windows of
    ``fallback_size`` pages advancing by ``fallback_size - overlap`` pages, so
    the default is 3-page windows with a 1-page overlap.
    """
    units = []
    current = None
    start = 1
    for index, page in enumerate(pages, 1):
        header = _match_header(page)
        if header is not None:
            if current is not None:
                text = "\n".join(pages[start - 1:index - 1])
                units.append(SectionedUnit(current, start, index - 1, text, estimate_tokens(text)))
            current, start = header, index
    if current is not None:
        text = "\n".join(pages[start - 1:])
        units.append(SectionedUnit(current, start, len(pages), text, estimate_tokens(text)))
    if not units:
        step = max(1, fallback_size - overlap)
        for first in range(0, len(pages), step):
            last = min(len(pages), first + fallback_size)
            text = "\n".join(pages[first:last])
            units.append(SectionedUnit("combined", first + 1, last, text, estimate_tokens(text), matched_header=False))
            if last == len(pages):
                break  # don't emit a trailing window fully covered by the overlap
    return units


def section_match_rate(units: list[SectionedUnit]) -> float:
    """Share of pages covered by header-matched units (Problems page metric)."""
    total = sum(unit.page_end - unit.page_start + 1 for unit in units)
    if total == 0:
        return 0.0
    matched = sum(unit.page_end - unit.page_start + 1 for unit in units if unit.matched_header)
    return round(matched / total, 4)
