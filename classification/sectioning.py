import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractionUnit:
    unit_type: str
    page_start: int
    page_end: int
    text: str
    token_estimate: int


HEADERS = {"mortgage": "mortgage|deed of trust", "foreclosure": "foreclosure|notice of trustee", "lien": "lien|judgment", "tax": "tax information|assessed value", "comparables": "comparables|comparative market"}


def section_pages(pages: list[str], fallback_size: int = 3) -> list[ExtractionUnit]:
    units = []
    current = None
    start = 1
    for index, page in enumerate(pages, 1):
        for unit_type, pattern in HEADERS.items():
            if re.search(pattern, page, re.IGNORECASE):
                current, start = unit_type, index
                break
        if current and (index == len(pages) or any(re.search(pattern, pages[index], re.IGNORECASE) for pattern in HEADERS.values())):
            text = "\n".join(pages[start - 1:index])
            units.append(ExtractionUnit(current, start, index, text, max(1, len(text) // 4)))
            current = None
    if not units:
        for start in range(0, len(pages), fallback_size - 1):
            end = min(len(pages), start + fallback_size)
            text = "\n".join(pages[start:end])
            units.append(ExtractionUnit("combined", start + 1, end, text, max(1, len(text) // 4)))
    return units
