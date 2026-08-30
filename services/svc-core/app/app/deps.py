from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import get_pool
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


def _user_id_from_claims(claims: dict) -> str:
    user_id = (
        str(claims.get("sub") or "").strip()
        or str(claims.get("user_id") or "").strip()
        or str(claims.get("uid") or "").strip()
        or str(claims.get("id") or "").strip()
    )
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid_token")
    try:
        UUID(user_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="invalid_token")
    return user_id


def get_current_user_id(
    claims: dict = Depends(get_current_claims),
) -> str:
    return _user_id_from_claims(claims)


def get_optional_current_user_id(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> Optional[str]:
    if not creds or not creds.credentials:
        return None
    try:
        claims = decode_access_jwt(creds.credentials)
        return _user_id_from_claims(claims)
    except HTTPException:
        return None
    except Exception:
        return None


# -------------------------
# Admin role required
# -------------------------
async def require_admin(claims: dict = Depends(get_current_claims)) -> dict:
    """Authorize an administrator from the live Core role tables.

    JWT roles remain useful as a UI/session hint, but they are deliberately not
    authoritative for privileged APIs. Every Admin request re-checks the user,
    active-account state, and roles in Core so role revocation takes effect
    immediately instead of waiting for access-token expiry.
    """
    user_id = _user_id_from_claims(claims)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                u.is_active,
                COALESCE(
                    ARRAY(
                        SELECT r.role_key
                        FROM core.user_roles ur
                        JOIN core.roles r ON r.id = ur.role_id
                        WHERE ur.user_id = u.id
                        ORDER BY r.role_key
                    ),
                    ARRAY[]::text[]
                ) AS roles
            FROM core.users u
            WHERE u.id = $1::uuid
            """,
            user_id,
        )

    if not row or not bool(row["is_active"]):
        raise HTTPException(status_code=403, detail="admin_required")

    roles = [str(role).strip().lower() for role in (row["roles"] or []) if str(role).strip()]
    if "admin" not in roles:
        raise HTTPException(status_code=403, detail="admin_required")

    live_claims = dict(claims)
    live_claims["roles"] = roles
    live_claims["sub"] = user_id
    return live_claims


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
