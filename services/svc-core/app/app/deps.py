from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.security import decode_access_jwt

bearer = HTTPBearer(auto_error=False)

# -------------------------
# Dependency functions
# -------------------------
def get_current_claims(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
) -> dict:
    if not creds or not creds.credentials:
        raise HTTPException(status_code=401, detail="missing_token")
    try:
        return decode_access_jwt(creds.credentials)
    except Exception:
        raise HTTPException(status_code=401, detail="invalid_token")


def get_current_user_id(
    claims: dict = Depends(get_current_claims),
) -> str:
    user_id = (
        str(claims.get("sub") or "").strip()
        or str(claims.get("user_id") or "").strip()
        or str(claims.get("uid") or "").strip()
        or str(claims.get("id") or "").strip()
    )
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid_token")
    return user_id


def get_optional_current_user_id(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> Optional[str]:
    if not creds or not creds.credentials:
        return None
    try:
        claims = decode_access_jwt(creds.credentials)
    except Exception:
        return None

    user_id = (
        str(claims.get("sub") or "").strip()
        or str(claims.get("user_id") or "").strip()
        or str(claims.get("uid") or "").strip()
        or str(claims.get("id") or "").strip()
    )
    return user_id or None


# -------------------------
# Admin role required
# -------------------------
def require_admin(claims: dict = Depends(get_current_claims)) -> dict:
    roles = claims.get("roles") or []
    if "admin" not in roles:
        raise HTTPException(status_code=403, detail="admin_required")
    return claims


# -------------------------
# Internal service auth
# -------------------------
def require_internal_service_auth(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_service_token: Optional[str] = Header(default=None, alias="X-Service-Token"),
) -> bool:
    """
    Minimal internal-service auth helper for routes like
    /api/internal/notifications/events.

    Accepts either:
    - Authorization: Bearer <token>
    - X-Service-Token: <token>

    Expected token comes from SVC_TO_SVC_BEARER.
    """
    import os

    expected = (os.getenv("SVC_TO_SVC_BEARER") or "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail="internal_service_auth_not_configured")

    supplied = ""

    if authorization:
        value = authorization.strip()
        if value.lower().startswith("bearer "):
            supplied = value[7:].strip()
        else:
            supplied = value

    if not supplied and x_service_token:
        supplied = x_service_token.strip()

    if not supplied:
        raise HTTPException(status_code=401, detail="missing_internal_service_token")

    if supplied != expected:
        raise HTTPException(status_code=403, detail="invalid_internal_service_token")

    return True