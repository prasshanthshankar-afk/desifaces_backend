from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
import asyncpg

from app.settings import settings
from app.db import get_pool

bearer = HTTPBearer(auto_error=False)


def get_db_pool() -> asyncpg.Pool:
    # Adapter for FastAPI Depends
    return get_pool()


def get_current_claims(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    if not creds or not creds.credentials:
        raise HTTPException(status_code=401, detail="missing_token")
    try:
        claims = jwt.decode(
            creds.credentials,
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