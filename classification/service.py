import re
from dataclasses import dataclass


REPORT_TYPES = ("property_profile", "owner_report", "mortgage", "foreclosure", "lien", "bankruptcy", "tax", "comparables", "valuation", "listing_history", "ownership_history", "rental", "combined", "unknown")


@dataclass(frozen=True)
class Classification:
    report_type: str
    vendor: str | None
    confidence: float


RULES = [(r"foreclosure|notice of trustee|NOD|NTS", "foreclosure"), (r"mortgage|deed of trust", "mortgage"), (r"lien|judgment", "lien"), (r"bankruptcy|chapter 7|chapter 13", "bankruptcy"), (r"comparables|comparative market", "comparables"), (r"tax information|assessed value", "tax"), (r"listing history|MLS", "listing_history"), (r"owner information|ownership", "owner_report")]


def classify(text: str, filename: str = "") -> Classification:
    haystack = f"{filename}\n{text[:4000]}"
    for pattern, report_type in RULES:
        if re.search(pattern, haystack, re.IGNORECASE):
            return Classification(report_type, None, .9)
    return Classification("unknown", None, 0.0)
