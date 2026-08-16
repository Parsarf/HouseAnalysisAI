from dataclasses import dataclass
import hashlib
import hmac

from fastapi import Cookie, HTTPException, status
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
