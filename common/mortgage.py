"""Shared mortgage identity rules used by underwriting and strategies."""

FIRST_POSITIONS = frozenset({"first", "1", "1st"})

def position_key(position: str | None) -> str:
    normalized = (position or "").strip().casefold()
    if normalized in FIRST_POSITIONS or normalized.startswith("first"):
        return "1"
    if normalized in {"second", "2", "2nd"} or normalized.startswith("second"):
        return "2"
    if "heloc" in normalized:
        return "heloc"
    return normalized

def is_first(position: str | None) -> bool:
    return position_key(position) == "1"
