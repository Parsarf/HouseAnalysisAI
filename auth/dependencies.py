import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Cookie, Depends, HTTPException, status

from common.settings import settings


@dataclass(frozen=True)
class User:
    id: str
    read_only: bool = False


def make_session(user_id: str, read_only: bool, secret: str) -> str:
    role = "read_only" if read_only else "owner"
    value = f"{user_id}:{role}"
    signature = hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}:{signature}"


def current_user(session_cookie: str | None = Cookie(default=None)) -> User:
    if not session_cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    # Session persistence is deliberately replaceable; the MVP stub accepts a signed session id.
    parts = session_cookie.split(":")
    if len(parts) != 3:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session")
    user_id, role, signature = parts
    expected = hmac.new(settings.session_secret.encode(), f"{user_id}:{role}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session")
    return User(user_id, role == "read_only")


def write_user(user: User = Depends(current_user)) -> User:
    """Guard for mutating endpoints: read-only users get 403 (spec §21)."""
    if user.read_only or settings.read_only:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="read-only user cannot mutate")
    return user
