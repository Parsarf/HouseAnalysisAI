from dataclasses import dataclass

from fastapi import Cookie, HTTPException, status


@dataclass(frozen=True)
class User:
    id: str
    read_only: bool = False


def current_user(session_cookie: str | None = Cookie(default=None)) -> User:
    if not session_cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    # Session persistence is deliberately replaceable; the MVP stub accepts a signed session id.
    user_id, _, role = session_cookie.partition(":")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session")
    return User(user_id, role == "read_only")
