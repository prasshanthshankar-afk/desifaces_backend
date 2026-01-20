from __future__ import annotations

import os
from typing import Any, Dict

from jose import jwt, JWTError

# svc-audio is a resource server: it only validates access JWTs issued by svc-core.
# It must NOT do password hashing or refresh token logic.

_JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
_JWT_ISSUER = os.getenv("JWT_ISSUER", "desifaces").strip()
_JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "desifaces_clients").strip()
_JWT_ALG = os.getenv("JWT_ALG", "HS256").strip()

if not _JWT_SECRET:
    raise RuntimeError("JWT_SECRET is required")

def decode_access_jwt(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(
            token,
            _JWT_SECRET,
            algorithms=[_JWT_ALG],
            audience=_JWT_AUDIENCE,
            issuer=_JWT_ISSUER,
        )
    except JWTError as e:
        raise ValueError(f"invalid_token: {e}") from e

def require_admin(claims: Dict[str, Any]) -> Dict[str, Any]:
    if not claims.get("is_admin"):
        raise ValueError("forbidden: admin_required")
    return claims