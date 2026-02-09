from __future__ import annotations

from typing import Any, Dict
import jwt

from app.config import settings


def decode_access_token(token: str) -> Dict[str, Any]:
    return jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALG],
        issuer=settings.JWT_ISSUER,
        audience=settings.JWT_AUDIENCE,
        options={"require": ["exp", "iat", "sub"]},
        leeway=30,
    )