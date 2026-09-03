# services/svc-pricing/app/app/api/deps.py
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from app.config import settings
from app.db import ensure_db_pool


@dataclass(frozen=True)
class AuthContext:
    user_id: UUID
    bearer_token: str
    country_code: str  # e.g. "IN", "US", ""


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def _try_extract_sub_from_jwt(bearer_token: str) -> Optional[str]:
    # WARNING: this does NOT verify signature. Use X-User-Id for production correctness.
    try:
        parts = bearer_token.split(".")
        if len(parts) < 2:
            return None
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        sub = payload.get("sub")
        return sub if isinstance(sub, str) else None
    except Exception:
        return None


async def get_db_pool():
    return await ensure_db_pool()


async def get_auth_context(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_country_code: Optional[str] = Header(default="", alias="X-Country-Code"),
) -> AuthContext:
    if settings.REQUIRE_AUTH:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1].strip()

    uid: Optional[UUID] = None
    if x_user_id:
        try:
            uid = UUID(x_user_id)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid X-User-Id")
    elif settings.REQUIRE_X_USER_ID:
        if settings.ALLOW_JWT_SUB_FALLBACK and bearer:
            sub = _try_extract_sub_from_jwt(bearer)
            if sub:
                try:
                    uid = UUID(sub)
                except Exception:
                    uid = None
        if uid is None:
            raise HTTPException(status_code=401, detail="missing X-User-Id")

    if uid is None:
        raise HTTPException(status_code=401, detail="unauthorized")

    header_cc = (x_country_code or "").strip().upper()
    if header_cc and (len(header_cc) != 2 or not header_cc.isalpha()):
        raise HTTPException(status_code=400, detail="invalid X-Country-Code")

    # CANONICAL_LOGIN_COUNTRY_V1: once authentication succeeds, country/currency
    # comes from core.users. A header may bootstrap older accounts only when the
    # canonical DB value is still empty; it cannot override an existing value.
    pool = await ensure_db_pool()
    async with pool.acquire() as conn:
        db_cc = await conn.fetchval(
            "SELECT country_code FROM core.users WHERE id=$1::uuid", uid
        )
        cc = str(db_cc or "").strip().upper()
        if not cc and header_cc:
            cc = header_cc
            await conn.execute(
                "UPDATE core.users SET country_code=$2, updated_at=now() WHERE id=$1::uuid AND country_code IS NULL",
                uid, cc,
            )
        if cc and (len(cc) != 2 or not cc.isalpha()):
            raise HTTPException(status_code=500, detail="invalid_canonical_country_code")

    return AuthContext(user_id=uid, bearer_token=bearer, country_code=cc)


AuthDep = Depends(get_auth_context)
PoolDep = Depends(get_db_pool)  