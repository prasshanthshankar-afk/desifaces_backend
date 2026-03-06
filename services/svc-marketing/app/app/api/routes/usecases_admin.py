# services/svc-marketing/app/app/api/routes/usecases_admin.py
from __future__ import annotations

import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import AuthedUser, require_user
from app.config import settings
from app.db import get_pool
from app.domain.models import (
    UseCaseApproveIn,
    UseCaseOut,
    UseCaseSuggestIn,
    UseCaseSuggestOut,
)
from app.repos.marketing_use_cases_repo import MarketingUseCasesRepo
from app.services.curation.usecase_curation_service import UseCaseCurationService

router = APIRouter(prefix="/api/marketing/admin/usecases", tags=["marketing-admin-usecases"])


def _require_admin(user: AuthedUser) -> None:
    if not settings.ADMIN_MARKETING_USER_ID or str(user.user_id) != settings.ADMIN_MARKETING_USER_ID:
        raise HTTPException(status_code=403, detail="Admin only")


def _to_out(row) -> UseCaseOut:
    # base_overlay_lines may be jsonb already
    bol = row["base_overlay_lines"]
    if isinstance(bol, str):
        try:
            bol = json.loads(bol)
        except Exception:
            bol = []
    return UseCaseOut(
        use_case_id=row["use_case_id"],
        approved=row["approved"],
        source=row["source"],
        version=row["version"],
        parent_use_case_id=row["parent_use_case_id"],
        persona=row["persona"],
        industry=row["industry"],
        recipe=row["recipe"],
        campaign_type=row["campaign_type"],
        season_event=row["season_event"],
        tags=row["tags"] or [],
        product_anchor=row["product_anchor"],
        default_offer=row["default_offer"],
        default_seconds=row["default_seconds"],
        default_hook=row["default_hook"],
        base_overlay_lines=bol or [],
        base_script=row["base_script"],
        default_music_prompt=row["default_music_prompt"],
        required_assets_json=row["required_assets_json"] or {},
        notes=row["notes"],
        weight=float(row["weight"]),
        usage_count=int(row["usage_count"]),
        last_used_at=row["last_used_at"].isoformat() if row["last_used_at"] else None,
        last_metrics_json=row["last_metrics_json"] or {},
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


@router.get("", response_model=list[UseCaseOut])
async def list_use_cases(
    approved: Optional[bool] = Query(default=None),
    source: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user: AuthedUser = Depends(require_user),
) -> list[UseCaseOut]:
    _require_admin(user)
    pool = await get_pool()
    repo = MarketingUseCasesRepo(pool)
    rows = await repo.list_use_cases(approved=approved, source=source, q=q, limit=limit)
    return [_to_out(r) for r in rows]


@router.post("/suggest", response_model=UseCaseSuggestOut)
async def suggest_use_cases(inp: UseCaseSuggestIn, user: AuthedUser = Depends(require_user)) -> UseCaseSuggestOut:
    _require_admin(user)
    pool = await get_pool()
    repo = MarketingUseCasesRepo(pool)
    svc = UseCaseCurationService(repo)

    ids = await svc.suggest_use_cases(
        created_by=user.user_id,
        persona=inp.persona.value if inp.persona else None,
        industry=inp.industry,
        recipe=inp.recipe.value if inp.recipe else None,
        season_event=inp.season_event,
        tags=inp.tags,
        count=inp.count,
    )
    return UseCaseSuggestOut(suggested_use_case_ids=ids)


@router.post("/{use_case_id}/approve", response_model=dict)
async def approve_use_case(use_case_id: UUID, inp: UseCaseApproveIn, user: AuthedUser = Depends(require_user)) -> dict:
    _require_admin(user)
    pool = await get_pool()
    repo = MarketingUseCasesRepo(pool)
    await repo.approve(use_case_id=use_case_id, approved=inp.approved, updated_by=user.user_id)
    return {"ok": True, "use_case_id": str(use_case_id), "approved": inp.approved}