# services/svc-music/app/app/api/deps.py
from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.db import get_pool

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    id: UUID
    email: str | None = None


def _decode_jwt(token: str) -> dict:
    secret = os.getenv("JWT_HMAC_SECRET") or os.getenv("JWT_SECRET") or ""
    alg = os.getenv("JWT_ALG", "HS256")
    issuer = os.getenv("JWT_ISSUER") or None
    audience = os.getenv("JWT_AUDIENCE") or None

    if not secret:
        raise HTTPException(status_code=500, detail="jwt_secret_missing")

    # Try python-jose first, then PyJWT.
    try:
        from jose import jwt as jose_jwt  # type: ignore

        kwargs = {"algorithms": [alg]}
        if issuer:
            kwargs["issuer"] = issuer
        if audience:
            kwargs["audience"] = audience
        return jose_jwt.decode(token, secret, **kwargs)
    except ImportError:
        pass
    except Exception:
        raise HTTPException(status_code=401, detail="invalid_token")

    try:
        import jwt as pyjwt  # type: ignore

        options = {
            "verify_signature": True,
            "verify_aud": bool(audience),
            "verify_iss": bool(issuer),
        }
        return pyjwt.decode(
            token,
            secret,
            algorithms=[alg],
            audience=audience,
            issuer=issuer,
            options=options,
        )
    except Exception:
        raise HTTPException(status_code=401, detail="invalid_token")


def _user_from_claims(claims: dict) -> CurrentUser:
    uid = claims.get("user_id") or claims.get("sub")
    if not uid:
        raise HTTPException(status_code=401, detail="token_missing_sub")
    try:
        user_id = UUID(str(uid))
    except Exception:
        raise HTTPException(status_code=401, detail="token_bad_sub")

    email = claims.get("email") or claims.get("upn") or None
    return CurrentUser(id=user_id, email=email)


async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> CurrentUser:
    """
    Primary: Authorization: Bearer <JWT>
    Fallback (dev/back-compat): X-User-Id: <uuid>
    """
    if creds and creds.scheme.lower() == "bearer" and creds.credentials:
        claims = _decode_jwt(creds.credentials)
        return _user_from_claims(claims)

    # Fallback header support (optional)
    if x_user_id:
        try:
            return CurrentUser(id=UUID(x_user_id))
        except Exception:
            raise HTTPException(status_code=401, detail="bad_x_user_id")

    raise HTTPException(status_code=401, detail="not_authenticated")



def require_user(user=Depends(get_current_user)):
    return user

async def require_admin(user=Depends(get_current_user)):
    """
    Production-grade RBAC gate for svc-music.

    Source of truth:
      - core.user_roles (user_id, role_id)
      - core.roles (id, role_key)

    Admin allowed if role_key in ('admin','support','ops')  (you can keep only 'admin' for now)
    """
    uid = getattr(user, "id", None) or getattr(user, "user_id", None)
    if not uid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT 1
        FROM core.user_roles ur
        JOIN core.roles r ON r.id = ur.role_id
        WHERE ur.user_id = $1
          AND r.role_key IN ('admin','support','ops')
        LIMIT 1
        """,
        uid,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_required")

    return user

# -------------------------------------------------------------------
# Back-compat dependency: some older routers import get_current_user_id
# -------------------------------------------------------------------
async def get_current_user_id(user: CurrentUser = Depends(get_current_user)) -> UUID:
    return user.id