from datetime import date, datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not permitted")
    return value.astimezone(timezone.utc)


def months_between(start: date, end: date) -> int:
    return max(0, (end.year - start.year) * 12 + end.month - start.month - (end.day < start.day))
