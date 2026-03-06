from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

import httpx
from fastapi import Header, HTTPException

from app.config import settings


@dataclass
class AuthedUser:
    user_id: UUID
    bearer_token: Optional[str] = None


async def require_user(
    authorization: Optional[str] = Header(default=None),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> AuthedUser:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Missing X-User-Id")
    try:
        user_uuid = UUID(x_user_id)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid X-User-Id")

    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1].strip()

    if settings.AUTH_MODE == "svc_core":
        if not bearer:
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    settings.CORE_URL.rstrip("/") + settings.CORE_INTROSPECT_PATH,
                    headers={"Authorization": f"Bearer {bearer}", "X-User-Id": str(user_uuid)},
                )
                if r.status_code >= 400:
                    raise HTTPException(status_code=401, detail="Auth introspection failed")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Auth introspection error")

    return AuthedUser(user_id=user_uuid, bearer_token=bearer)