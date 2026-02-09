from __future__ import annotations

import os
import secrets
from typing import Any, Dict, Optional

from jose import jwt, JWTError

# svc-audio is a resource server: it only validates access JWTs issued by svc-core.
# It must NOT do password hashing or refresh token logic.

_JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
_JWT_ISSUER = os.getenv("JWT_ISSUER", "desifaces").strip()
_JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "desifaces_clients").strip()
_JWT_ALG = os.getenv("JWT_ALG", "HS256").strip()

# ✅ Service-to-service bearer (Option A)
# Set this same value in svc-fusion-extension (worker) and in svc-audio (server).
# Example: SVC_TO_SVC_BEARER="Bearer <LONG_RANDOM_SECRET>"
_SVC_TO_SVC_BEARER = os.getenv("SVC_TO_SVC_BEARER", "").strip()

if not _JWT_SECRET:
    raise RuntimeError("JWT_SECRET is required")


def _strip_bearer(auth: str) -> str:
    s = (auth or "").strip()
    if not s:
        return ""
    if s.lower().startswith("bearer "):
        return s[7:].strip()
    return s


def _service_claims() -> Dict[str, Any]:
    # Minimal claims object for internal calls (avoid pretending it's a user)
    return {
        "sub": "svc-fusion-extension",
        "token_type": "service",
        "scopes": ["internal"],
        "is_service": True,
        "iss": "desifaces-internal",
        "aud": "desifaces-services",
    }


def decode_access_jwt(token: str) -> Dict[str, Any]:
    """
    Accepts either:
      - user access JWT (from svc-core), OR
      - service-to-service bearer secret (Option A)

    The caller may pass:
      - raw token, OR
      - full Authorization header value ('Bearer ...').
    """
    raw = _strip_bearer(token)

    # ✅ Option A: allow internal service bearer
    if _SVC_TO_SVC_BEARER:
        svc_raw = _strip_bearer(_SVC_TO_SVC_BEARER)
        if raw and svc_raw and secrets.compare_digest(raw, svc_raw):
            return _service_claims()

    # Otherwise validate as user JWT issued by svc-core
    try:
        return jwt.decode(
            raw,
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