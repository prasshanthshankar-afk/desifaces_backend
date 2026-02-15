from __future__ import annotations

import os
from uuid import UUID

try:
    from jose import jwt  # type: ignore
except Exception:  # pragma: no cover
    jwt = None  # type: ignore


def try_get_user_id_from_auth(authorization: str | None) -> UUID | None:
    """
    If you already have a shared JWT secret across services, set:
      JWT_SECRET, JWT_ALGORITHM (default HS256)
    If not set (or jose not installed), this returns None and deps.py falls back to X-User-Id.
    """
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None

    token = authorization.split(" ", 1)[1].strip()

    # If token itself is a UUID (some internal scripts do this), accept it.
    try:
        return UUID(token)
    except Exception:
        pass

    secret = os.getenv("JWT_SECRET")
    if not secret or jwt is None:
        return None

    alg = os.getenv("JWT_ALGORITHM", "HS256")
    try:
        payload = jwt.decode(token, secret, algorithms=[alg])
        sub = payload.get("sub") or payload.get("user_id") or payload.get("uid")
        if not sub:
            return None
        return UUID(str(sub))
    except Exception:
        return None