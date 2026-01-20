# services/svc-face/app/app/api/deps.py
from __future__ import annotations
from uuid import UUID
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.security import decode_access_jwt
from app.db import get_pool

bearer = HTTPBearer(auto_error=False)

def get_current_claims(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    """Extract and validate JWT claims"""
    if not creds or not creds.credentials:
        raise HTTPException(status_code=401, detail="missing_token")
    try:
        return decode_access_jwt(creds.credentials)
    except Exception:
        raise HTTPException(status_code=401, detail="invalid_token")

async def get_current_user_id(claims: dict = Depends(get_current_claims)) -> str:
    """
    Extract user UUID from JWT and verify user exists.
    Returns UUID as string.
    """
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="missing_sub")
    
    try:
        user_uuid = str(UUID(str(sub)))
    except Exception:
        raise HTTPException(status_code=401, detail="invalid_sub")
    
    # Verify user exists
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM core.users WHERE id = $1::uuid",
            user_uuid
        )
        if not exists:
            raise HTTPException(status_code=401, detail="user_not_found")
    
    return user_uuid