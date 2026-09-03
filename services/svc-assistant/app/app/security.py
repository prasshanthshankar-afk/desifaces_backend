from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from desifaces_shared.identity.account_context import AccountContextNotFound, resolve_account_context

from .config import settings
from .db import open_business_pool

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AssistantAuthContext:
    user_id: UUID
    account_id: UUID
    token: str


def decode_access_jwt(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALG],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="invalid_token") from exc


async def get_assistant_auth(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
) -> AssistantAuthContext:
    if not creds or not creds.credentials:
        raise HTTPException(status_code=401, detail="missing_token")

    token = creds.credentials
    claims = decode_access_jwt(token)
    try:
        user_id = UUID(str(claims.get("sub")))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid_sub") from exc

    pool = await open_business_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("select 1 from core.users where id=$1", user_id)
        if not exists:
            raise HTTPException(status_code=401, detail="user_not_found")
        try:
            account = await resolve_account_context(conn, user_id)
        except AccountContextNotFound as exc:
            raise HTTPException(status_code=409, detail="account_context_not_found") from exc

    return AssistantAuthContext(
        user_id=user_id,
        account_id=account.account_id,
        token=token,
    )
