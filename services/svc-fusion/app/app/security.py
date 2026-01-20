from __future__ import annotations

import os
from typing import Any, Dict

from jose import jwt, JWTError

_JWT_SECRET = os.getenv("JWT_SECRET") or os.getenv("JWT_HMAC_SECRET") or ""
_JWT_ALG = os.getenv("JWT_ALG", "HS256")
_JWT_ISSUER = os.getenv("JWT_ISSUER") or None
_JWT_AUDIENCE = os.getenv("JWT_AUDIENCE") or None


def decode_access_jwt(token: str) -> Dict[str, Any]:
    if not _JWT_SECRET:
        raise ValueError("invalid_token: JWT_SECRET/JWT_HMAC_SECRET not set")

    try:
        kwargs = {"algorithms": [_JWT_ALG]}
        if _JWT_AUDIENCE:
            kwargs["audience"] = _JWT_AUDIENCE
        if _JWT_ISSUER:
            kwargs["issuer"] = _JWT_ISSUER

        return jwt.decode(token, _JWT_SECRET, **kwargs)
    except JWTError as e:
        raise ValueError(f"invalid_token: {e}") from e