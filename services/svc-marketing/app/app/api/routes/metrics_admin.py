# services/svc-marketing/app/app/api/routes/metrics_admin.py
from __future__ import annotations

from datetime import date
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import AuthedUser, require_user
from app.config import settings
from app.db import get_pool
from app.domain.models import MetricsIngestIn
from app.repos.marketing_platform_posts_repo import MarketingPlatformPostsRepo
from app.repos.marketing_metrics_repo import MarketingMetricsRepo

router = APIRouter(prefix="/api/marketing/admin/metrics", tags=["marketing-admin-metrics"])


def _require_admin(user: AuthedUser) -> None:
    if not settings.ADMIN_MARKETING_USER_ID or str(user.user_id) != settings.ADMIN_MARKETING_USER_ID:
        raise HTTPException(status_code=403, detail="Admin only")


@router.post("/ingest", response_model=dict)
async def ingest_metrics(inp: MetricsIngestIn, user: AuthedUser = Depends(require_user)) -> dict:
    _require_admin(user)
    pool = await get_pool()
    posts = MarketingPlatformPostsRepo(pool)
    metrics_repo = MarketingMetricsRepo(pool)

    post_row = await posts.find_by_media_id(platform=inp.platform, media_id=inp.media_id)
    if not post_row:
        raise HTTPException(status_code=404, detail="platform post not found (unknown media_id)")

    metric_date = date.fromisoformat(inp.metric_date)
    await metrics_repo.upsert_metrics(
        platform_post_id=post_row["platform_post_id"],
        metric_date=metric_date,
        metrics={
            "impressions": inp.impressions,
            "reach": inp.reach,
            "plays": inp.plays,
            "likes": inp.likes,
            "comments": inp.comments,
            "shares": inp.shares,
            "saves": inp.saves,
            "profile_visits": inp.profile_visits,
            "follows": inp.follows,
            "watch_time_ms": inp.watch_time_ms,
        },
        raw_json=inp.raw_json,
    )

    return {"ok": True, "platform_post_id": str(post_row["platform_post_id"]), "metric_date": inp.metric_date}