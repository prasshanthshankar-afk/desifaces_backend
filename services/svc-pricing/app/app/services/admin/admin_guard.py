# services/svc-pricing/app/app/services/admin/admin_guard.py
from __future__ import annotations

from typing import Iterable
from uuid import UUID

import asyncpg

from app.config import settings


def _role_keys_allowlist() -> list[str]:
    raw = (settings.PRICING_ADMIN_ROLE_KEYS or "").strip()
    if not raw:
        return ["admin", "ops", "pricing_admin"]
    return [x.strip() for x in raw.split(",") if x.strip()]


async def require_admin(conn: asyncpg.Connection, user_id: UUID) -> None:
    """
    Authorizes against existing RBAC:
      core.user_roles(user_id, role_id) -> core.roles(id, role_key)

    Admin allowed if role_key ∈ PRICING_ADMIN_ROLE_KEYS.
    """
    allow = _role_keys_allowlist()

    r = await conn.fetchrow(
        """
        select 1
        from core.user_roles ur
        join core.roles r on r.id = ur.role_id
        where ur.user_id = $1
          and r.role_key = any($2::text[])
        limit 1
        """,
        user_id,
        allow,
    )
    if not r:
        raise PermissionError("admin_required")