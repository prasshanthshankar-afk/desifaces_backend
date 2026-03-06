from __future__ import annotations

import re
from typing import Optional
from uuid import UUID

import asyncpg

from app.config import settings

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _qi_part(name: str) -> str:
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f'"{name}"'


def _qi_qualified(name: str) -> str:
    """
    Allows schema-qualified identifiers like: core.users
    Disallows arbitrary SQL by requiring each part match IDENT regex.
    """
    parts = [p.strip() for p in (name or "").split(".") if p.strip()]
    if not parts:
        raise ValueError("Empty identifier")
    return ".".join(_qi_part(p) for p in parts)


async def maybe_set_admin_marketing_user_id(pool: asyncpg.Pool) -> Optional[UUID]:
    """
    Option A:
      - If settings.ADMIN_MARKETING_USER_ID is set: return it.
      - Else if settings.ADMIN_MARKETING_EMAIL is set: resolve user_id from DB and set settings.ADMIN_MARKETING_USER_ID.
    """
    if settings.ADMIN_MARKETING_USER_ID:
        try:
            return UUID(str(settings.ADMIN_MARKETING_USER_ID))
        except Exception:
            pass

    email = (settings.ADMIN_MARKETING_EMAIL or "").strip()
    if not email:
        return None

    table = _qi_qualified(settings.ADMIN_MARKETING_USER_TABLE or "core.users")
    id_col = _qi_part(settings.ADMIN_MARKETING_USER_ID_COLUMN or "id")
    email_col = _qi_part(settings.ADMIN_MARKETING_USER_EMAIL_COLUMN or "email")

    sql = f"""
    select {id_col}
    from {table}
    where lower({email_col}) = lower($1)
    limit 1
    """

    async with pool.acquire() as conn:
        val = await conn.fetchval(sql, email)

    if not val:
        return None

    uid = UUID(str(val))
    settings.ADMIN_MARKETING_USER_ID = str(uid)  # cache for process lifetime
    return uid