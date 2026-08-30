from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.audit import audit_log
from app.db import get_pool
from app.deps import require_admin

router = APIRouter()


class AdminUserPatch(BaseModel):
    tier: str | None = None
    is_active: bool | None = None


def _actor_user_id(claims: dict) -> str:
    return str(claims.get("sub") or "").strip()


def _validated_user_id(user_id: str) -> str:
    try:
        return str(UUID(str(user_id)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="invalid_user_id")


def _request_meta(request: Request) -> tuple[str | None, str | None, str | None]:
    request_id = getattr(request.state, "request_id", None)
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    return request_id, ip, user_agent


@router.get("/context")
async def admin_context(claims: dict = Depends(require_admin)):
    """Small live-authorization endpoint used by server-side Admin guards."""
    return {
        "ok": True,
        "user_id": _actor_user_id(claims),
        "email": str(claims.get("email") or "").strip().lower(),
        "roles": list(claims.get("roles") or []),
    }


@router.get("/users")
async def list_users(
    limit: int = Query(default=100, ge=1, le=500),
    q: str | None = Query(default=None, max_length=200),
    _: dict = Depends(require_admin),
):
    pool = await get_pool()
    query = (q or "").strip()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                u.id::text AS id,
                u.email,
                u.full_name,
                u.tier,
                u.is_active,
                u.created_at,
                COALESCE(
                    ARRAY(
                        SELECT r.role_key
                        FROM core.user_roles ur
                        JOIN core.roles r ON r.id = ur.role_id
                        WHERE ur.user_id = u.id
                        ORDER BY r.role_key
                    ),
                    ARRAY[]::text[]
                ) AS roles
            FROM core.users u
            WHERE ($1 = '' OR u.email ILIKE ('%' || $1 || '%') OR u.id::text = $1)
            ORDER BY u.created_at DESC
            LIMIT $2
            """,
            query,
            limit,
        )
        return [dict(r) for r in rows]


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: str,
    patch: AdminUserPatch,
    request: Request,
    claims: dict = Depends(require_admin),
):
    target_user_id = _validated_user_id(user_id)
    actor_user_id = _actor_user_id(claims)
    payload = patch.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(status_code=422, detail="empty_admin_user_patch")

    if target_user_id == actor_user_id and patch.is_active is False:
        raise HTTPException(status_code=409, detail="admin_cannot_deactivate_self")

    request_id, ip, user_agent = _request_meta(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            before_row = await conn.fetchrow(
                """
                SELECT id::text AS id, email, full_name, tier, is_active
                FROM core.users
                WHERE id = $1::uuid
                FOR UPDATE
                """,
                target_user_id,
            )
            if not before_row:
                raise HTTPException(status_code=404, detail="user_not_found")

            after_row = await conn.fetchrow(
                """
                UPDATE core.users
                SET tier = COALESCE($2, tier),
                    is_active = COALESCE($3, is_active),
                    updated_at = now()
                WHERE id = $1::uuid
                RETURNING id::text AS id, email, full_name, tier, is_active
                """,
                target_user_id,
                patch.tier,
                patch.is_active,
            )

            await audit_log(
                conn,
                action="admin.user.update",
                entity_type="user",
                entity_id=target_user_id,
                actor_user_id=actor_user_id,
                request_id=request_id,
                ip=ip,
                user_agent=user_agent,
                before=dict(before_row),
                after=dict(after_row),
                strict=True,
            )

    return {"ok": True, "user": dict(after_row)}
