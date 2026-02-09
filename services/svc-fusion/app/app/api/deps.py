from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.security import decode_access_jwt
from app.config import settings
from app.db import get_pool

bearer = HTTPBearer(auto_error=False)


def check_fusion_enabled() -> bool:
    if not settings.FUSION_STUDIO_ENABLED:
        raise HTTPException(status_code=403, detail="fusion_disabled")
    return True


RequireFusionEnabled = Depends(check_fusion_enabled)


def get_current_claims(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    if not creds or not creds.credentials:
        raise HTTPException(status_code=401, detail="missing_token")
    try:
        return decode_access_jwt(creds.credentials)
    except Exception:
        raise HTTPException(status_code=401, detail="invalid_token")


async def get_current_user_id(
    request: Request,
    claims: dict = Depends(get_current_claims),
) -> str:
    """
    USER JWT:
      - use claims.sub (UUID)

    SERVICE token:
      - REQUIRE header X-Actor-User-Id (UUID)
      - DO NOT return a sentinel string (must be UUID everywhere)
    """
    is_service = bool(claims.get("is_service")) or (claims.get("token_type") == "service")

    if is_service:
        actor = (request.headers.get("X-Actor-User-Id") or "").strip()
        if not actor:
            raise HTTPException(status_code=401, detail="missing_actor_user_id")

        try:
            actor_uuid = str(UUID(actor))
        except Exception:
            raise HTTPException(status_code=401, detail="invalid_actor_user_id")

        pool = await get_pool()
        async with pool.acquire() as conn:
            exists = await conn.fetchval("SELECT 1 FROM core.users WHERE id = $1::uuid", actor_uuid)
            if not exists:
                raise HTTPException(status_code=401, detail="actor_user_not_found")

        return actor_uuid

    # Normal user token
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="missing_sub")

    try:
        user_uuid = str(UUID(str(sub)))
    except Exception:
        raise HTTPException(status_code=401, detail="invalid_sub")

    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM core.users WHERE id = $1::uuid", user_uuid)
        if not exists:
            raise HTTPException(status_code=401, detail="user_not_found")

    return user_uuid