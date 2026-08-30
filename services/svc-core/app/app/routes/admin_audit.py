from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.db import get_pool
from app.deps import require_admin

router = APIRouter()


@router.get("/audit")
async def admin_audit_events(
    limit: int = Query(default=100, ge=1, le=500),
    action: str | None = Query(default=None, max_length=160),
    _: dict = Depends(require_admin),
):
    """Read append-only Core Admin audit events.

    Only privileged Admin actions are exposed here; unrelated authentication,
    provider and job audit traffic remains outside the Admin governance view.
    """
    requested_action = (action or "").strip()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                a.id,
                a.actor_user_id::text AS actor_user_id,
                u.email AS actor_email,
                a.action,
                a.entity_type,
                a.entity_id,
                a.request_id,
                a.before_json,
                a.after_json,
                a.created_at
            FROM core.audit_log a
            LEFT JOIN core.users u ON u.id = a.actor_user_id
            WHERE a.action LIKE 'admin.%'
              AND ($1 = '' OR a.action = $1)
            ORDER BY a.created_at DESC
            LIMIT $2
            """,
            requested_action,
            limit,
        )
    return {"items": [dict(row) for row in rows], "count": len(rows), "source": "core.audit_log"}
