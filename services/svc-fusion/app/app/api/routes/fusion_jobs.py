from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import RequireFusionEnabled, get_current_user_id
from app.db import get_pool
from app.domain.models import FusionJobCreate, FusionJobView, StepView, ArtifactView
from app.domain.validators import validate_fusion_request
from app.services.fusion_orchestrator import FusionOrchestrator
from app.services.artifact_service import ArtifactService
from app.repos.fusion_jobs_repo import FusionJobsRepo
from app.repos.steps_repo import StepsRepo
from app.repos.artifacts_repo import ArtifactsRepo

logger = logging.getLogger("fusion_jobs")

router = APIRouter()


def _is_azure_blob_url(url: str) -> bool:
    s = (url or "").strip()
    if not s:
        return False
    try:
        p = urlparse(s)
        host = (p.netloc or "").lower()
        return host.endswith(".blob.core.windows.net")
    except Exception:
        return False


# Optional: only mint SAS for these kinds (tune as you like)
_SAS_KINDS = {
    "audio",
    "image",
    "face",
    "face_image",
    "video",  # if you later persist videos into azure blob
    "resolved_face_sas_url",
    "resolved_audio_sas_url",
}


@router.post("/jobs", dependencies=[RequireFusionEnabled], response_model=FusionJobView)
async def create_job(
    req: FusionJobCreate,
    user_id: str = Depends(get_current_user_id),  # UUID string
) -> FusionJobView:
    validate_fusion_request(req)

    pool = await get_pool()
    orch = FusionOrchestrator(pool)

    job_id = await orch.create_job(user_id=user_id, req=req)
    return FusionJobView(job_id=job_id, status="queued")


@router.get("/jobs/{job_id}", dependencies=[RequireFusionEnabled], response_model=FusionJobView)
async def get_job(job_id: str) -> FusionJobView:
    """
    Get job status, steps, and artifacts.

    UX behavior:
      - For Azure Blob artifacts, mint a fresh read SAS before returning,
        so UI playback/download doesn't rely on stale SAS URLs.
    """
    pool = await get_pool()
    jobs = FusionJobsRepo(pool)
    steps = StepsRepo(pool)
    artifacts = ArtifactsRepo(pool)
    artifact_svc = ArtifactService()

    job = await jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    step_rows = await steps.list_steps(job_id)
    artifact_rows = await artifacts.list_artifacts(job_id)

    # 1) Best-effort provider_job_id discovery (prefer provider_runs)
    provider_job_id = None
    try:
        async with pool.acquire() as conn:
            provider_job_id = await conn.fetchval(
                """
                SELECT provider_job_id::text
                FROM provider_runs
                WHERE job_id = $1::uuid
                  AND provider_job_id IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                job_id,
            )
    except Exception as e:
        logger.debug("provider_job_id_lookup_failed job_id=%s err=%s", job_id, str(e))

    # fallback: scan steps meta_json
    if not provider_job_id:
        for step in step_rows:
            meta = step.get("meta_json")
            if isinstance(meta, dict):
                provider_job_id = meta.get("provider_job_id")
                if provider_job_id:
                    break

    # 2) Mint fresh SAS for Azure Blob artifacts
    resolved_artifacts: list[ArtifactView] = []
    for a in artifact_rows:
        kind = str(a.get("kind") or "")
        url = str(a.get("url") or "")
        content_type = a.get("content_type")

        try:
            if kind in _SAS_KINDS and _is_azure_blob_url(url):
                # IMPORTANT: mint_read_sas_for_artifact must be robust to bad storage_path.
                url = await artifact_svc.mint_read_sas_for_artifact(dict(a), ttl_hours=2)
        except Exception as e:
            # Don't fail the whole response
            logger.debug(
                "sas_mint_failed job_id=%s kind=%s url=%s err=%s",
                job_id,
                kind,
                url[:120],
                str(e),
            )

        resolved_artifacts.append(ArtifactView(kind=kind, url=url, content_type=content_type))

    return FusionJobView(
        job_id=str(job["id"]),
        status=str(job["status"]),
        provider_job_id=provider_job_id,
        error_code=job.get("error_code"),
        error_message=job.get("error_message"),
        steps=[
            StepView(
                step_code=str(r["step_code"]),
                status=str(r["status"]),
                attempt=int(r.get("attempt") or 0),
                error_code=r.get("error_code"),
                error_message=r.get("error_message"),
            )
            for r in step_rows
        ],
        artifacts=resolved_artifacts,
    )