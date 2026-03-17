from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import RequireFusionEnabled, get_current_user_id
from app.db import get_pool
from app.domain.models import ArtifactView, FusionJobCreate, FusionJobView, StepView
from app.domain.validators import validate_fusion_request
from app.services.fusion_orchestrator import FusionOrchestrator, PricingClientError
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


def _coerce_dict(v):
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            vv = json.loads(v)
            return vv if isinstance(vv, dict) else {}
        except Exception:
            return {}
    try:
        if hasattr(v, "keys"):
            return {k: v[k] for k in v.keys()}
    except Exception:
        pass
    try:
        vv = dict(v)
        return vv if isinstance(vv, dict) else {}
    except Exception:
        return {}


def _extract_pricing_view(job) -> dict | None:
    payload = _coerce_dict(job.get("payload_json"))
    meta = _coerce_dict(job.get("meta_json"))

    pricing = _coerce_dict(payload.get("pricing"))
    if pricing:
        return pricing

    pricing = _coerce_dict(meta.get("pricing"))
    if pricing:
        return pricing

    return None


def _raise_http_for_pricing_error(exc: Exception) -> None:
    msg = str(exc or "")

    if "PRICING_CLIENT_DISABLED" in msg or "pricing client unavailable" in msg.lower():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PRICING_CLIENT_DISABLED",
        )

    if "PRICING_UNKNOWN_OR_INACTIVE_VARIANT" in msg:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="PRICING_UNKNOWN_OR_INACTIVE_VARIANT",
        )

    if "PRICING_VARIANT_ZERO_QTY_LINES" in msg:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="PRICING_VARIANT_ZERO_QTY_LINES",
        )

    raise HTTPException(
        status_code=status.HTTP_424_FAILED_DEPENDENCY,
        detail="PRICING_RESERVATION_FAILED",
    )


_SAS_KINDS = {
    "audio",
    "image",
    "face",
    "face_image",
    "video",
    "resolved_face_sas_url",
    "resolved_audio_sas_url",
}


@router.post("/jobs", dependencies=[RequireFusionEnabled], response_model=FusionJobView)
async def create_job(
    req: FusionJobCreate,
    user_id: str = Depends(get_current_user_id),
) -> FusionJobView:
    validate_fusion_request(req)

    pool = await get_pool()
    orch = FusionOrchestrator(pool)
    jobs = FusionJobsRepo(pool)

    try:
        job_id = await orch.create_job(user_id=user_id, req=req)
    except PricingClientError as e:
        _raise_http_for_pricing_error(e)

    job = await jobs.get_job(job_id)
    if not job:
        return FusionJobView(job_id=job_id, status="queued")

    return FusionJobView(
        job_id=str(job["id"]),
        status=str(job["status"]),
        error_code=job.get("error_code"),
        error_message=job.get("error_message"),
        pricing=_extract_pricing_view(job),
    )


@router.get("/jobs/{job_id}", dependencies=[RequireFusionEnabled], response_model=FusionJobView)
async def get_job(job_id: str) -> FusionJobView:
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

    if not provider_job_id:
        for step in step_rows:
            meta = step.get("meta_json")
            if isinstance(meta, dict):
                provider_job_id = meta.get("provider_job_id")
                if provider_job_id:
                    break

    resolved_artifacts: list[ArtifactView] = []
    for a in artifact_rows:
        kind = str(a.get("kind") or "")
        url = str(a.get("url") or "")
        content_type = a.get("content_type")

        try:
            if kind in _SAS_KINDS and _is_azure_blob_url(url):
                url = await artifact_svc.mint_read_sas_for_artifact(dict(a), ttl_hours=2)
        except Exception as e:
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
        pricing=_extract_pricing_view(job),
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