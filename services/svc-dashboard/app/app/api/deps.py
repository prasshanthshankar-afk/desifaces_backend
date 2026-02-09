from __future__ import annotations

import os
import secrets

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
import asyncpg

from app.settings import settings
from app.db import get_pool

bearer = HTTPBearer(auto_error=False)

# ✅ Service-to-service bearer (Option A)
# Set the same value across svc-fusion-extension (worker), svc-dashboard, svc-audio, svc-fusion:
#   SVC_TO_SVC_BEARER="Bearer <LONG_RANDOM_SECRET>"
_SVC_TO_SVC_BEARER = os.getenv("SVC_TO_SVC_BEARER", "").strip()


def get_db_pool() -> asyncpg.Pool:
    # Adapter for FastAPI Depends
    return get_pool()


def _service_claims() -> dict:
    # Minimal claims for internal calls (do not pretend it's a user JWT)
    return {
        "sub": "svc-fusion-extension",
        "token_type": "service",
        "scopes": ["internal"],
        "is_service": True,
        "iss": "desifaces-internal",
        "aud": "desifaces-services",
    }


def get_current_claims(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    if not creds or not creds.credentials:
        raise HTTPException(status_code=401, detail="missing_token")

    token_raw = (creds.credentials or "").strip()
    if not token_raw:
        raise HTTPException(status_code=401, detail="missing_token")

    # ✅ Option A: allow internal service bearer
    if _SVC_TO_SVC_BEARER:
        svc = _SVC_TO_SVC_BEARER.strip()
        if svc.lower().startswith("bearer "):
            svc = svc[7:].strip()
        # creds.credentials from HTTPBearer is already the raw token (no "Bearer " prefix)
        if svc and secrets.compare_digest(token_raw, svc):
            return _service_claims()

    # Otherwise validate as user JWT
    try:
        claims = jwt.decode(
            token_raw,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALG],
            options={"verify_aud": False},
        )
        return claims
    except JWTError:
        raise HTTPException(status_code=401, detail="invalid_token")


def get_current_user_id(claims: dict = Depends(get_current_claims)) -> str:
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="missing_sub")
    return str(sub)