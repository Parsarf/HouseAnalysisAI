"""Structural document-kind classification independent of filenames."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

DocumentKind = Literal["property_profile", "owner_profile"]


def classify_document_text(text: str) -> tuple[DocumentKind, float]:
    normalized = re.sub(r"\s+", " ", text).casefold()
    owner_markers = ("person type", "ownership role")
    property_markers = ("property details", "tax assessment")
    has_apn = bool(re.search(r"\b(?:apn|assessor(?:'s)? parcel)\b", normalized))
    owner_score = sum(marker in normalized for marker in owner_markers)
    property_score = sum(marker in normalized for marker in property_markers) + int(has_apn)
    if owner_score >= 2 and property_score == 0:
        return "owner_profile", 0.98
    if property_score >= 2:
        return "property_profile", 0.95
    if owner_score > property_score:
        return "owner_profile", 0.70
    return "property_profile", 0.60


def classify_pdf(path: Path) -> tuple[DocumentKind, float]:
    try:
        import fitz

        with fitz.open(path) as document:
            text = "\n".join(page.get_text() for page in document[: min(4, len(document))])
    except (ImportError, OSError, RuntimeError, ValueError):
        text = ""
    return classify_document_text(text)
