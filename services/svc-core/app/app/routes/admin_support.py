from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.db import get_pool
from app.deps import require_admin

router = APIRouter()


@router.get("/support/requests")
async def admin_list_support_requests(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=200),
    status: str | None = Query(default=None, max_length=64),
    _: dict = Depends(require_admin),
):
    """Search support-ticket metadata without exposing message bodies or attachments.

    Customer support conversations may contain PII or other sensitive data. The
    default Admin console therefore returns only operational metadata needed to
    find and triage a request. The normal user-scoped support API is unchanged.
    """
    query = (q or "").strip()
    requested_status = (status or "").strip()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                sr.id::text AS id,
                sr.user_id::text AS user_id,
                sr.email,
                sr.name,
                sr.topic,
                sr.product_area,
                sr.priority,
                sr.subject,
                sr.status::text AS status,
                sr.tier_code,
                sr.latest_message_at,
                sr.created_at,
                sr.updated_at
            FROM support_requests sr
            WHERE
                ($1 = '' OR sr.email ILIKE ('%' || $1 || '%')
                    OR sr.subject ILIKE ('%' || $1 || '%')
                    OR sr.id::text = $1
                    OR sr.user_id::text = $1)
                AND ($2 = '' OR sr.status::text = $2)
            ORDER BY sr.latest_message_at DESC NULLS LAST, sr.created_at DESC
            LIMIT $3 OFFSET $4
            """,
            query,
            requested_status,
            limit,
            offset,
        )
    return {
        "items": [dict(row) for row in rows],
        "count": len(rows),
        "limit": limit,
        "offset": offset,
        "content_redacted": True,
        "content_policy": "ticket_metadata_only",
    }
