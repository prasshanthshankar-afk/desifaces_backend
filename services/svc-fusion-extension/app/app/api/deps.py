from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import asyncpg

from app.db import get_db_pool
from app.security import decode_access_token

bearer = HTTPBearer(auto_error=True)


async def get_db_pool_dep() -> asyncpg.Pool:
    return await get_db_pool()


def get_current_token(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    token = (creds.credentials or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return token


def get_current_user_id(token: str = Depends(get_current_token)) -> str:
    try:
        payload = decode_access_token(token)
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=401, detail="Token missing sub")
        return str(sub)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")