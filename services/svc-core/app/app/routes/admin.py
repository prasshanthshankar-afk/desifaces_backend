from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr

from app.db import get_pool
from app.deps import require_admin

router = APIRouter()

class AdminUserPatch(BaseModel):
    tier: str | None = None  # free|pro|enterprise
    is_active: bool | None = None

@router.get("/users")
async def list_users(_: dict = Depends(require_admin)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text AS id, email, full_name, tier, is_active, created_at
            FROM core.users
            ORDER BY created_at DESC
            LIMIT 200
            """
        )
        return [dict(r) for r in rows]

@router.patch("/users/{user_id}")
async def patch_user(user_id: str, patch: AdminUserPatch, _: dict = Depends(require_admin)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE core.users
            SET tier = COALESCE($2, tier),
                is_active = COALESCE($3, is_active),
                updated_at = now()
            WHERE id = $1::uuid
            """,
            user_id,
            patch.tier,
            patch.is_active,
        )
        return {"ok": True}