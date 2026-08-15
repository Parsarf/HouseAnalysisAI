from dataclasses import dataclass
from decimal import Decimal

from common.serializers import json_safe


@dataclass(frozen=True)
class ChangeEvent:
    change_type: str
    field_path: str
    old_value: object
    new_value: object
    score_delta: Decimal | None = None


def diff_records(before: dict, after: dict, score_delta: Decimal | None = None) -> list[ChangeEvent]:
    events = []
    for field in sorted(set(before) | set(after)):
        old, new = before.get(field), after.get(field)
        if json_safe(old) == json_safe(new):
            continue
        if field.startswith("liens"):
            change_type = "new_lien" if old is None else "lien_amount_corrected"
        elif field.startswith("foreclosure"):
            change_type = "foreclosure_status_changed"
        else:
            change_type = "field_changed"
        events.append(ChangeEvent(change_type, field, old, new, score_delta))
    return events
