# services/svc-marketing/app/app/api/routes/marketing_runs.py
from __future__ import annotations

import json
from uuid import UUID
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import AuthedUser, require_user
from app.config import settings
from app.db import get_pool
from app.domain.enums import MarketingRunMode
from app.domain.models import MarketingRunIn, MarketingRunOut, MarketingRunStatusOut, UseCaseSpec
from app.repos.marketing_runs_repo import MarketingRunsRepo
from app.services.orchestration.run_executor import RunExecutor

router = APIRouter(prefix="/api/marketing", tags=["marketing"])


def _as_dict_loose(x: Any) -> Dict[str, Any]:
    """
    Robustly normalize asyncpg json/jsonb outputs across environments:
    - dict -> dict
    - str(JSON) -> dict
    - None/other -> {}
    """
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            v = json.loads(s)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    try:
        d = dict(x)  # type: ignore[arg-type]
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


@router.post("/runs", response_model=MarketingRunOut)
async def create_run(inp: MarketingRunIn, user: AuthedUser = Depends(require_user)) -> MarketingRunOut:
    if inp.mode == MarketingRunMode.publish and settings.STRICT_ADMIN_ONLY:
        if not settings.ADMIN_MARKETING_USER_ID or str(user.user_id) != settings.ADMIN_MARKETING_USER_ID:
            raise HTTPException(status_code=403, detail="Publish mode requires marketing admin account")

    pool = await get_pool()
    repo = MarketingRunsRepo(pool)

    # IMPORTANT:
    # Do NOT persist short-lived bearer tokens in DB.
    # RunExecutor should obtain a fresh token at execution time via DF_SERVICE_EMAIL/DF_SERVICE_PASSWORD,
    # or fall back to request-time token only in-memory.
    run_id = await repo.create_run(
        run_as_user_id=user.user_id,
        bearer_token=None,  # <--- intentionally NOT stored
        mode=inp.mode,
        recipe=inp.recipe,
        cost_bucket=settings.DEFAULT_COST_BUCKET,
        cost_category=inp.recipe.value,
        input_json=inp.model_dump(),
    )

    row = await repo.get_run_row(run_id)
    return MarketingRunOut(
        run_id=run_id,
        status=row["status"],
        mode=inp.mode,
        recipe=inp.recipe,
        stage=row["stage"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
        use_case=None,
        output={"artifact_status": {}},  # UI-friendly default; executor will fill
    )


@router.get("/runs/{run_id}/status", response_model=MarketingRunStatusOut)
async def get_status(run_id: UUID, user: AuthedUser = Depends(require_user)) -> MarketingRunStatusOut:
    pool = await get_pool()
    repo = MarketingRunsRepo(pool)
    row = await repo.get_run_row(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")

    # Owners can read; admin can read all
    if str(row["run_as_user_id"]) != str(user.user_id):
        if not settings.ADMIN_MARKETING_USER_ID or str(user.user_id) != settings.ADMIN_MARKETING_USER_ID:
            raise HTTPException(status_code=403, detail="Forbidden")

    planning = _as_dict_loose(row.get("planning_json"))
    output = _as_dict_loose(row.get("output_json"))

    # Ensure artifact_status always exists for the UI
    if not isinstance(output.get("artifact_status"), dict):
        output["artifact_status"] = {}

    use_case = None
    if isinstance(planning.get("use_case"), dict):
        use_case = UseCaseSpec(**planning["use_case"])

    return MarketingRunStatusOut(
        run_id=run_id,
        status=row["status"],
        stage=row["stage"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        use_case=use_case,
        output=output,
    )


@router.post("/runs/{run_id}/publish", response_model=MarketingRunStatusOut)
async def publish_now(run_id: UUID, user: AuthedUser = Depends(require_user)) -> MarketingRunStatusOut:
    if not settings.ADMIN_MARKETING_USER_ID or str(user.user_id) != settings.ADMIN_MARKETING_USER_ID:
        raise HTTPException(status_code=403, detail="Publish requires marketing admin account")

    pool = await get_pool()
    executor = RunExecutor(pool=pool)
    await executor.publish_only(run_id=run_id)

    repo = MarketingRunsRepo(pool)
    row = await repo.get_run_row(run_id)

    planning = _as_dict_loose(row.get("planning_json"))
    output = _as_dict_loose(row.get("output_json"))
    if not isinstance(output.get("artifact_status"), dict):
        output["artifact_status"] = {}

    use_case = None
    if isinstance(planning.get("use_case"), dict):
        use_case = UseCaseSpec(**planning["use_case"])

    return MarketingRunStatusOut(
        run_id=run_id,
        status=row["status"],
        stage=row["stage"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        use_case=use_case,
        output=output,
    )