from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
import asyncpg
import os
from typing import Optional

from app.api.deps import (
    get_db_pool_dep as get_db_pool,
    get_current_user_id,
    get_current_token,  # used only for request auth; we won't persist user JWT
)
from app.domain.models import (
    LongformCreateRequest,
    LongformJobCreated,
    LongformJobView,
    LongformSegmentView,
)
from app.repos.longform_jobs_repo import LongformJobsRepo
from app.repos.longform_segments_repo import LongformSegmentsRepo
from app.services.chunking_service import split_script_into_segments
from app.services.sas_service import AzureBlobService
from app.config import settings

router = APIRouter(prefix="/api/longform", tags=["longform"])

jobs_repo = LongformJobsRepo()
segs_repo = LongformSegmentsRepo()


def _clamp_fusion_duration(sec: int) -> int:
    # svc-fusion hard limit is 120 today
    return max(1, min(120, int(sec)))


def _normalize_bearer(token_or_header: Optional[str]) -> Optional[str]:
    t = (token_or_header or "").strip()
    if not t:
        return None
    if t.lower().startswith("bearer "):
        return t
    return f"Bearer {t}"


def _service_bearer_for_workers() -> Optional[str]:
    """
    Product-grade rule:
    - NEVER persist short-lived user JWTs for async workers (they expire).
    - Prefer service bearer for worker execution (Option A).

    We store a bearer in longform_jobs.auth_token ONLY if a service token is configured.
    Otherwise worker must rely on env inside container; API still works but jobs can fail later.
    """
    # Prefer settings if present
    tok = getattr(settings, "SVC_TO_SVC_BEARER", None)
    tok = tok or os.getenv("SVC_TO_SVC_BEARER") or os.getenv("SVC_FUSION_EXTENSION_BEARER")
    return _normalize_bearer(tok)


@router.post("/jobs", response_model=LongformJobCreated)
async def create_longform_job(
    req: LongformCreateRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: str = Depends(get_current_user_id),
    # This dependency enforces the request is authenticated; we intentionally do NOT store it.
    _request_token: str = Depends(get_current_token),
):
    if req.segment_seconds > 120 or req.max_segment_seconds > 120:
        raise HTTPException(
            status_code=400,
            detail="segment_seconds/max_segment_seconds must be <= 120 (svc-fusion limit)",
        )

    chunks = split_script_into_segments(
        req.script_text,
        target_segment_seconds=req.segment_seconds,
        max_segment_seconds=req.max_segment_seconds,
        wpm=150,
    )
    if not chunks:
        raise HTTPException(status_code=400, detail="script_text produced no segments")

    if len(chunks) > settings.MAX_TOTAL_SEGMENTS_PER_JOB:
        raise HTTPException(
            status_code=400,
            detail=f"Too many segments ({len(chunks)} > {settings.MAX_TOTAL_SEGMENTS_PER_JOB})",
        )

    # Optional voice controls
    voice_gender_mode = getattr(req, "voice_gender_mode", None)  # "auto" | "manual"
    voice_gender = getattr(req, "voice_gender", None)            # "male" | "female" | None
    if voice_gender_mode is not None:
        voice_gender_mode = str(voice_gender_mode).strip().lower() or None
    if voice_gender is not None:
        voice_gender = str(voice_gender).strip().lower() or None

    # ✅ Persist service bearer for workers (NOT the user JWT)
    worker_auth_token = _service_bearer_for_workers()
    if not worker_auth_token:
        # Product-grade: fail early rather than accept a job that will likely fail async.
        # If you prefer "best effort", change this to warn/log and continue.
        raise HTTPException(
            status_code=503,
            detail="svc_to_svc_bearer_missing: configure SVC_TO_SVC_BEARER for longform workers",
        )

    async with pool.acquire() as conn:
        job_id = await jobs_repo.create_job(
            conn,
            user_id=user_id,
            face_artifact_id=req.face_artifact_id,
            script_text=req.script_text,
            voice_cfg=req.voice_cfg.model_dump(),
            aspect_ratio=req.aspect_ratio,
            segment_seconds=req.segment_seconds,
            max_segment_seconds=req.max_segment_seconds,
            tags=req.tags,
            total_segments=len(chunks),
            # ✅ Persist token + gender policy
            auth_token=worker_auth_token,
            voice_gender_mode=voice_gender_mode,
            voice_gender=voice_gender,
        )

        for c in chunks:
            await segs_repo.insert_segment(
                conn,
                job_id=job_id,
                segment_index=c.index,
                text_chunk=c.text,
                duration_sec=_clamp_fusion_duration(c.duration_sec),
            )

    return LongformJobCreated(job_id=job_id, status="queued")


@router.get("/jobs/{job_id}", response_model=LongformJobView)
async def get_longform_job(
    job_id: str,
    pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: str = Depends(get_current_user_id),
):
    async with pool.acquire() as conn:
        row = await jobs_repo.get_job(conn, job_id, user_id)
        if not row:
            raise HTTPException(status_code=404, detail="Job not found")

        final_url = None
        if row["final_storage_path"]:
            az = AzureBlobService(settings.AZURE_STORAGE_CONNECTION_STRING)
            final_url = az.sign_read_url(
                settings.AZURE_FINAL_VIDEO_CONTAINER,
                row["final_storage_path"],
                settings.FINAL_SAS_TTL_SECONDS,
            )

        # Only include if your LongformJobView model has these fields.
        # If not, remove these 2 lines + the args below.
        voice_gender_mode = row.get("voice_gender_mode")
        voice_gender = row.get("voice_gender")

        return LongformJobView(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            status=row["status"],
            aspect_ratio=row["aspect_ratio"],
            segment_seconds=row["segment_seconds"],
            max_segment_seconds=row["max_segment_seconds"],
            total_segments=row["total_segments"],
            completed_segments=row["completed_segments"],
            final_video_url=final_url,
            final_storage_path=row["final_storage_path"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"].isoformat(),
            updated_at=row["updated_at"].isoformat(),
            # include only if present in model
            voice_gender_mode=voice_gender_mode,
            voice_gender=voice_gender,
        )


@router.get("/jobs/{job_id}/segments", response_model=list[LongformSegmentView])
async def list_job_segments(
    job_id: str,
    pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: str = Depends(get_current_user_id),
):
    async with pool.acquire() as conn:
        job = await jobs_repo.get_job(conn, job_id, user_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        rows = await segs_repo.list_segments_for_job(conn, job_id)

        out: list[LongformSegmentView] = []
        for r in rows:
            out.append(
                LongformSegmentView(
                    id=str(r["id"]),
                    segment_index=r["segment_index"],
                    status=r["status"],
                    duration_sec=r["duration_sec"],
                    audio_url=r["audio_url"],
                    fusion_job_id=str(r["fusion_job_id"]) if r["fusion_job_id"] else None,
                    segment_video_url=r["segment_video_url"],
                    error_code=r["error_code"],
                    error_message=r["error_message"],
                )
            )
        return out