from sqlalchemy import text
from sqlalchemy.orm import Session


def advisory_lock_key(value: str) -> int:
    # Stable signed 32-bit key suitable for pg_advisory_xact_lock(int).
    return int.from_bytes(value.encode()[:4].ljust(4, b"\0"), "big", signed=True)


def acquire_advisory_lock(session: Session, value: str) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory_lock_key(value)})
