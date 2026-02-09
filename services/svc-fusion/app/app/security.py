from __future__ import annotations

import os
import secrets
from typing import Any, Dict

from jose import jwt, JWTError

_JWT_SECRET = os.getenv("JWT_SECRET") or os.getenv("JWT_HMAC_SECRET") or ""
_JWT_ALG = os.getenv("JWT_ALG", "HS256")
_JWT_ISSUER = os.getenv("JWT_ISSUER") or None
_JWT_AUDIENCE = os.getenv("JWT_AUDIENCE") or None

# ✅ Service-to-service bearer (Option A)
# Set the same value across svc-fusion-extension (worker) and svc-fusion (server):
#   SVC_TO_SVC_BEARER="Bearer <LONG_RANDOM_SECRET>"
_SVC_TO_SVC_BEARER = os.getenv("SVC_TO_SVC_BEARER", "").strip()


def _strip_bearer(auth: str) -> str:
    s = (auth or "").strip()
    if not s:
        return ""
    if s.lower().startswith("bearer "):
        return s[7:].strip()
    return s


def _service_claims() -> Dict[str, Any]:
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

    Input may be raw token or 'Bearer <token>'.
    """
    raw = _strip_bearer(token)

    # ✅ Option A: allow internal service bearer
    if _SVC_TO_SVC_BEARER:
        svc_raw = _strip_bearer(_SVC_TO_SVC_BEARER)
        if raw and svc_raw and secrets.compare_digest(raw, svc_raw):
            return _service_claims()

    # Otherwise validate user JWT
    if not _JWT_SECRET:
        raise ValueError("invalid_token: JWT_SECRET/JWT_HMAC_SECRET not set")

    try:
        kwargs: Dict[str, Any] = {"algorithms": [_JWT_ALG]}
        if _JWT_AUDIENCE:
            kwargs["audience"] = _JWT_AUDIENCE
        if _JWT_ISSUER:
            kwargs["issuer"] = _JWT_ISSUER

        return jwt.decode(raw, _JWT_SECRET, **kwargs)
    except JWTError as e:
        raise ValueError(f"invalid_token: {e}") from e