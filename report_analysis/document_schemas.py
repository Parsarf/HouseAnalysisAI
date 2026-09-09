"""Page-accounted discovery; canonical schemas remain per entity."""
from typing import Literal

from pydantic import Field

from .schemas import StrictModel

VERSION = "document-v2"


class DiscoveredEntity(StrictModel):
    key: str
    kind: Literal["property", "owner"]
    role: Literal["subject", "comparable", "listing", "reference", "owner"]
    address: str | None
    city: str | None
    state: str | None
    zip5: str | None
    apn: str | None
    jurisdiction: str | None
    name: str | None
    pages: list[int]
    evidence: str


class PageDisposition(StrictModel):
    page: int = Field(ge=1)
    status: Literal["entities", "supporting", "blank", "irrelevant", "unreadable"]
    entity_keys: list[str]
    reason: str | None


class Discovery(StrictModel):
    pages: list[PageDisposition]
    entities: list[DiscoveredEntity]


DISCOVERY_PROMPT = """Inventory EVERY page of this PDF chunk, using both visual content and text.
Discover EVERY identifiable real-estate property, including every row of property lists and
comparable-sales tables. One page can contain many properties; one property can span many
pages. Keep units/apartments distinct. Include separate owner/skip-trace profiles as owner
entities. A person's mailing or contact address alone is NOT a property entity.
Use a unique key per entity within this chunk. Give all relevant local page numbers (1-based),
and a distinguishing evidence description including table row/region where applicable.
Group repeated mentions of the same property; never group merely because owners match.
Return a disposition for every page and link supporting pages to entities when grounded.
If identity or attribution cannot be resolved, retain the entity with null identity fields and
describe the uncertainty. Mark unreadable pages explicitly. Never invent missing identifiers.
All page numbers refer to THIS attachment, starting at 1. Return only the required JSON.
"""
