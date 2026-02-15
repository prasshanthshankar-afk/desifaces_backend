from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.repos.music_candidates_repo import MusicCandidatesRepo
from app.services.music_candidates_controller import MusicCandidatesController

router = APIRouter(prefix="/api/music", tags=["music-candidates"])


class CreateLyricsCandidatesIn(BaseModel):
    count: int = Field(3, ge=1, le=10)
    hitl: bool = True
    provider: Optional[str] = None
    seeds: Optional[List[int]] = None
    overrides: Dict[str, Any] = Field(default_factory=dict)


class CreateAudioCandidatesIn(BaseModel):
    count: int = Field(3, ge=1, le=10)
    hitl: bool = True
    providers: Optional[List[str]] = None
    seeds: Optional[List[int]] = None
    duration_ms: Optional[int] = None
    overrides: Dict[str, Any] = Field(default_factory=dict)


class CreateVideoCandidatesIn(BaseModel):
    count: int = Field(2, ge=1, le=6)
    hitl: bool = True
    providers: Optional[List[str]] = None
    overrides: Dict[str, Any] = Field(default_factory=dict)


class ChooseCandidateIn(BaseModel):
    make_active: bool = True


@router.post("/jobs/{job_id}/candidates/lyrics")
async def create_lyrics_candidates(job_id: UUID, body: CreateLyricsCandidatesIn):
    ctrl = MusicCandidatesController()
    gid = await ctrl.start_group(
        job_id=job_id,
        candidate_type="lyrics",
        count=body.count,
        provider=body.provider,
        providers=None,
        seeds=body.seeds,
        hitl=body.hitl,
        request_overrides=body.overrides,
    )
    return {"job_id": str(job_id), "candidate_type": "lyrics", "group_id": str(gid), "created": body.count, "status": "queued"}


@router.post("/jobs/{job_id}/candidates/audio")
async def create_audio_candidates(job_id: UUID, body: CreateAudioCandidatesIn):
    ctrl = MusicCandidatesController()
    overrides = dict(body.overrides or {})
    if body.duration_ms:
        overrides["duration_ms"] = int(body.duration_ms)

    gid = await ctrl.start_group(
        job_id=job_id,
        candidate_type="audio",
        count=body.count,
        providers=body.providers,
        seeds=body.seeds,
        hitl=body.hitl,
        request_overrides=overrides,
    )
    return {"job_id": str(job_id), "candidate_type": "audio", "group_id": str(gid), "created": body.count, "status": "queued"}


@router.post("/jobs/{job_id}/candidates/video")
async def create_video_candidates(job_id: UUID, body: CreateVideoCandidatesIn):
    ctrl = MusicCandidatesController()
    gid = await ctrl.start_group(
        job_id=job_id,
        candidate_type="video",
        count=body.count,
        providers=body.providers,
        hitl=body.hitl,
        request_overrides=body.overrides,
    )
    return {"job_id": str(job_id), "candidate_type": "video", "group_id": str(gid), "created": body.count, "status": "queued"}


@router.get("/jobs/{job_id}/candidates")
async def list_candidates(
    job_id: UUID,
    type: Optional[str] = None,
    group_id: Optional[UUID] = None,
    attempt: Optional[int] = None,
):
    repo = MusicCandidatesRepo()
    rows = await repo.list(job_id=job_id, candidate_type=type, group_id=group_id, attempt=attempt)
    return {"job_id": str(job_id), "candidate_type": type, "group_id": str(group_id) if group_id else None, "items": rows}


@router.post("/jobs/{job_id}/candidates/{candidate_id}/select")
async def select_candidate(job_id: UUID, candidate_id: UUID, body: ChooseCandidateIn):
    if not body.make_active:
        return {"status": "noop"}

    ctrl = MusicCandidatesController()
    try:
        chosen = await ctrl.choose_candidate(job_id=job_id, candidate_id=candidate_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Recompute required_action (it should clear)
    await ctrl.refresh_required_action(job_id=job_id)
    return {"status": "ok", "job_id": str(job_id), "chosen_candidate_id": str(candidate_id), "candidate": chosen}


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: UUID):
    ctrl = MusicCandidatesController()
    # Clears required_action if set and recomputes
    await ctrl.refresh_required_action(job_id=job_id)
    return {"status": "ok", "job_id": str(job_id)}