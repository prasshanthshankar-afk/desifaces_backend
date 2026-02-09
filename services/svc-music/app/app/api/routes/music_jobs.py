from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_current_user
from app.domain.models import (
    GenerateMusicIn,
    GenerateMusicOut,
    MusicJobStatusOut,
    PublishMusicIn,
    PublishMusicOut,
)
from app.domain.enums import MusicJobStatus
from app.repos.music_projects_repo import MusicProjectsRepo
from app.repos.music_jobs_repo import MusicJobsRepo
from app.services.music_orchestrator import (
    enqueue_video_job,
    get_video_job_status,
    publish_project_to_video_or_fusion,
)

router = APIRouter(prefix="/music", tags=["music-jobs"])


@router.post("/projects/{project_id}/generate", response_model=GenerateMusicOut)
async def generate(project_id: UUID, payload: GenerateMusicIn, user=Depends(get_current_user)):
    # Validate ownership via project (music_video_jobs doesn't store user_id)
    projects = MusicProjectsRepo()
    proj = await projects.get(project_id=project_id, user_id=user.id)
    if not proj:
        raise HTTPException(status_code=404, detail="project_not_found")

    # Build job input_json (enums -> strings for jsonb)
    input_json = payload.model_dump(mode="json")

    # Attach voice reference pointer (if configured)
    voice_ref_asset_id = proj.get("voice_ref_asset_id")
    input_json["voice_ref_asset_id"] = str(voice_ref_asset_id) if voice_ref_asset_id else None

    jobs = MusicJobsRepo()
    job_id = await jobs.create_video_job(
        project_id=project_id,
        input_json=input_json,
    )

    # Enqueue worker job
    await enqueue_video_job(job_id)

    return GenerateMusicOut(job_id=job_id, status=MusicJobStatus.queued)


@router.get("/jobs/{job_id}/status", response_model=MusicJobStatusOut)
async def status(job_id: UUID, user=Depends(get_current_user)):
    out = await get_video_job_status(job_id=job_id, user_id=user.id)
    if not out:
        raise HTTPException(status_code=404, detail="job_not_found")
    return out


@router.post("/jobs/{job_id}/publish", response_model=PublishMusicOut)
async def publish(job_id: UUID, payload: PublishMusicIn, user=Depends(get_current_user)):
    out = await publish_project_to_video_or_fusion(job_id=job_id, user_id=user.id, publish_in=payload)
    if not out:
        raise HTTPException(status_code=404, detail="job_not_found")
    return out