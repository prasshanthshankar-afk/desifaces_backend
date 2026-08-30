from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.audit import audit_log
from app.db import get_pool
from app.deps import require_admin, require_super_admin

router = APIRouter()

PRIVILEGED_ROLES = {"admin", "super_admin"}


class AdminUserPatch(BaseModel):
    tier: Literal["free", "pro", "enterprise"] | None = None
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


async def _roles_for_user(conn, user_id: str) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT r.role_key
        FROM core.user_roles ur
        JOIN core.roles r ON r.id = ur.role_id
        WHERE ur.user_id = $1::uuid
        ORDER BY r.role_key
        """,
        user_id,
    )
    return [str(row["role_key"]) for row in rows]


async def _active_role_count(conn, role_key: str) -> int:
    count = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT u.id)
        FROM core.users u
        JOIN core.user_roles ur ON ur.user_id = u.id
        JOIN core.roles r ON r.id = ur.role_id
        WHERE u.is_active = true AND r.role_key = $1
        """,
        role_key,
    )
    return int(count or 0)


@router.get("/context")
async def admin_context(claims: dict = Depends(require_admin)):
    """Live authorization context used by server and client Admin guards."""
    return {
        "ok": True,
        "user_id": _actor_user_id(claims),
        "email": str(claims.get("email") or "").strip().lower(),
        "roles": list(claims.get("roles") or []),
        "is_super_admin": "super_admin" in (claims.get("roles") or []),
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


@router.get("/access/administrators")
async def list_administrators(_: dict = Depends(require_super_admin)):
    """Super-admin-only roster for the Access Control interface."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                u.id::text AS id,
                u.email,
                u.full_name,
                u.is_active,
                u.created_at,
                ARRAY(
                    SELECT r2.role_key
                    FROM core.user_roles ur2
                    JOIN core.roles r2 ON r2.id = ur2.role_id
                    WHERE ur2.user_id = u.id
                    ORDER BY r2.role_key
                ) AS roles
            FROM core.users u
            WHERE EXISTS (
                SELECT 1
                FROM core.user_roles ur
                JOIN core.roles r ON r.id = ur.role_id
                WHERE ur.user_id = u.id
                  AND r.role_key IN ('admin', 'super_admin')
            )
            ORDER BY u.email
            """
        )
        return [dict(row) for row in rows]


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: str,
    patch: AdminUserPatch,
    request: Request,
    claims: dict = Depends(require_admin),
):
    target_user_id = _validated_user_id(user_id)
    actor_user_id = _actor_user_id(claims)
    actor_roles = {str(role) for role in (claims.get("roles") or [])}
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

            before_roles = await _roles_for_user(conn, target_user_id)
            if (
                patch.is_active is False
                and PRIVILEGED_ROLES.intersection(before_roles)
                and "super_admin" not in actor_roles
            ):
                raise HTTPException(status_code=403, detail="super_admin_required")

            if patch.is_active is False and "super_admin" in before_roles:
                if await _active_role_count(conn, "super_admin") <= 1:
                    raise HTTPException(
                        status_code=409,
                        detail="cannot_deactivate_last_active_super_admin",
                    )

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
                before={**dict(before_row), "roles": before_roles},
                after={**dict(after_row), "roles": before_roles},
                strict=True,
            )

    return {"ok": True, "user": dict(after_row)}


async def _grant_role(
    *,
    role_key: Literal["admin", "super_admin"],
    user_id: str,
    request: Request,
    claims: dict,
):
    target_user_id = _validated_user_id(user_id)
    actor_user_id = _actor_user_id(claims)
    request_id, ip, user_agent = _request_meta(request)
    pool = await get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            target = await conn.fetchrow(
                "SELECT id::text AS id, email, is_active FROM core.users WHERE id = $1::uuid FOR UPDATE",
                target_user_id,
            )
            if not target:
                raise HTTPException(status_code=404, detail="user_not_found")
            if not bool(target["is_active"]):
                raise HTTPException(status_code=409, detail="inactive_user_cannot_receive_privileged_role")

            role_id = await conn.fetchval(
                "SELECT id FROM core.roles WHERE role_key = $1",
                role_key,
            )
            if role_id is None:
                raise HTTPException(status_code=500, detail=f"{role_key}_role_not_configured")

            before_roles = await _roles_for_user(conn, target_user_id)
            await conn.execute(
                """
                INSERT INTO core.user_roles(user_id, role_id)
                VALUES ($1::uuid, $2)
                ON CONFLICT (user_id, role_id) DO NOTHING
                """,
                target_user_id,
                role_id,
            )
            after_roles = await _roles_for_user(conn, target_user_id)

            await audit_log(
                conn,
                action="admin.role.grant",
                entity_type="user_role",
                entity_id=target_user_id,
                actor_user_id=actor_user_id,
                request_id=request_id,
                ip=ip,
                user_agent=user_agent,
                before={"roles": before_roles},
                after={"roles": after_roles, "role": role_key},
                strict=True,
            )

    return {"ok": True, "user_id": target_user_id, "roles": after_roles}


async def _revoke_role(
    *,
    role_key: Literal["admin", "super_admin"],
    user_id: str,
    request: Request,
    claims: dict,
):
    target_user_id = _validated_user_id(user_id)
    actor_user_id = _actor_user_id(claims)
    if target_user_id == actor_user_id and role_key == "super_admin":
        raise HTTPException(status_code=409, detail="super_admin_cannot_revoke_self")

    request_id, ip, user_agent = _request_meta(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            target = await conn.fetchrow(
                "SELECT id::text AS id, email, is_active FROM core.users WHERE id = $1::uuid FOR UPDATE",
                target_user_id,
            )
            if not target:
                raise HTTPException(status_code=404, detail="user_not_found")

            role_id = await conn.fetchval(
                "SELECT id FROM core.roles WHERE role_key = $1",
                role_key,
            )
            if role_id is None:
                raise HTTPException(status_code=500, detail=f"{role_key}_role_not_configured")

            before_roles = await _roles_for_user(conn, target_user_id)
            if role_key not in before_roles:
                return {"ok": True, "user_id": target_user_id, "roles": before_roles}

            if role_key == "super_admin" and await _active_role_count(conn, role_key) <= 1:
                raise HTTPException(status_code=409, detail="cannot_revoke_last_active_super_admin")

            await conn.execute(
                "DELETE FROM core.user_roles WHERE user_id = $1::uuid AND role_id = $2",
                target_user_id,
                role_id,
            )
            after_roles = await _roles_for_user(conn, target_user_id)

            await audit_log(
                conn,
                action="admin.role.revoke",
                entity_type="user_role",
                entity_id=target_user_id,
                actor_user_id=actor_user_id,
                request_id=request_id,
                ip=ip,
                user_agent=user_agent,
                before={"roles": before_roles, "role": role_key},
                after={"roles": after_roles},
                strict=True,
            )

    return {"ok": True, "user_id": target_user_id, "roles": after_roles}


@router.put("/users/{user_id}/roles/admin")
async def grant_admin_role(
    user_id: str,
    request: Request,
    claims: dict = Depends(require_super_admin),
):
    return await _grant_role(
        role_key="admin", user_id=user_id, request=request, claims=claims
    )


@router.delete("/users/{user_id}/roles/admin")
async def revoke_admin_role(
    user_id: str,
    request: Request,
    claims: dict = Depends(require_super_admin),
):
    return await _revoke_role(
        role_key="admin", user_id=user_id, request=request, claims=claims
    )


@router.put("/users/{user_id}/roles/super_admin")
async def grant_super_admin_role(
    user_id: str,
    request: Request,
    claims: dict = Depends(require_super_admin),
):
    return await _grant_role(
        role_key="super_admin", user_id=user_id, request=request, claims=claims
    )


@router.delete("/users/{user_id}/roles/super_admin")
async def revoke_super_admin_role(
    user_id: str,
    request: Request,
    claims: dict = Depends(require_super_admin),
):
    return await _revoke_role(
        role_key="super_admin", user_id=user_id, request=request, claims=claims
    )
