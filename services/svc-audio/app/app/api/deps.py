from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.security import decode_access_jwt
from app.db import get_pool

bearer = HTTPBearer(auto_error=False)


def get_current_claims(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    if not creds or not creds.credentials:
        raise HTTPException(status_code=401, detail="missing_token")
    try:
        return decode_access_jwt(creds.credentials)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"invalid_token: {e}")


def require_admin(claims: dict = Depends(get_current_claims)) -> dict:
    roles = claims.get("roles") or []
    if "admin" not in roles:
        raise HTTPException(status_code=403, detail="admin_required")
    return claims


async def get_current_user_id(
    claims: dict = Depends(get_current_claims),
    x_actor_user_id: str | None = Header(default=None, alias="X-Actor-User-Id"),
) -> str:
    # ✅ service token path
    if claims.get("is_service") or claims.get("token_type") == "service":
        if not x_actor_user_id:
            raise HTTPException(status_code=401, detail="missing_actor_user_id")
        try:
            actor_uuid = str(UUID(str(x_actor_user_id)))
        except Exception:
            raise HTTPException(status_code=401, detail="invalid_actor_user_id")

        pool = await get_pool()
        async with pool.acquire() as conn:
            exists = await conn.fetchval("select 1 from core.users where id=$1::uuid", actor_uuid)
            if not exists:
                raise HTTPException(status_code=401, detail="actor_user_not_found")
        return actor_uuid

    # ✅ normal user JWT path
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="missing_user_id")
    try:
        return str(UUID(str(user_id)))
    except Exception:
        raise HTTPException(status_code=401, detail="invalid_user_id")