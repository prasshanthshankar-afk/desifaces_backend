# services/svc-face/app/app/api/routes/face_jobs.py
from __future__ import annotations
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_user_id
from app.db import get_pool
from app.domain.models import (
    FaceGenerateRequest, 
    FaceJobView, 
    FaceProfileView,
    RegionConfigView,
    StyleConfigView,
    ContextConfigView
)
from app.services.face_orchestrator import FaceOrchestrator
from app.repos.face_jobs_repo import FaceJobsRepo
from app.repos.face_profiles_repo import FaceProfilesRepo
from app.repos.config_repo import FaceConfigRepo

router = APIRouter()

@router.post("/generate", response_model=FaceJobView)
async def generate_faces(
    req: FaceGenerateRequest,
    user_id: str = Depends(get_current_user_id),
) -> FaceJobView:
    """
    Generate 4 diverse face images.
    
    Returns job_id - use /jobs/{job_id} to check status.
    """
    pool = await get_pool()
    orch = FaceOrchestrator(pool)
    
    job_id = await orch.create_job(user_id=user_id, req=req)
    
    return FaceJobView(
        job_id=job_id,
        status="queued",
        faces=[]
    )

@router.get("/jobs/{job_id}", response_model=FaceJobView)
async def get_job_status(
    job_id: str,
    user_id: str = Depends(get_current_user_id)
) -> FaceJobView:
    """
    Get face generation job status and results.
    """
    pool = await get_pool()
    jobs_repo = FaceJobsRepo(pool)
    profiles_repo = FaceProfilesRepo(pool)
    
    # Get job
    job = await jobs_repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    
    # Verify ownership
    if str(job["user_id"]) != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    
    # Get generated faces
    face_records = await profiles_repo.get_job_faces(job_id)
    
    faces = [
        FaceProfileView(
            face_profile_id=str(f["id"]),
            image_url=str(f["image_url"]) if f["image_url"] else "",
            thumbnail_url=None,
            variant=f["attributes_json"].get("variant", 0),
            generation_params=f["meta_json"] or {}
        )
        for f in face_records
    ]
    
    return FaceJobView(
        job_id=str(job["id"]),
        status=str(job["status"]),
        faces=faces,
        error_code=job.get("error_code"),
        error_message=job.get("error_message")
    )

@router.get("/jobs", response_model=List[FaceJobView])
async def list_user_jobs(
    user_id: str = Depends(get_current_user_id),
    limit: int = 20
) -> List[FaceJobView]:
    """
    List user's face generation jobs.
    """
    pool = await get_pool()
    jobs_repo = FaceJobsRepo(pool)
    
    jobs = await jobs_repo.list_user_jobs(user_id, limit)
    
    return [
        FaceJobView(
            job_id=str(j["id"]),
            status=str(j["status"]),
            faces=[]
        )
        for j in jobs
    ]

@router.get("/profiles", response_model=List[FaceProfileView])
async def list_user_profiles(
    user_id: str = Depends(get_current_user_id),
    limit: int = 50
) -> List[FaceProfileView]:
    """
    List user's saved face profiles.
    """
    pool = await get_pool()
    profiles_repo = FaceProfilesRepo(pool)
    
    profiles = await profiles_repo.list_user_profiles(user_id, limit)
    
    return [
        FaceProfileView(
            face_profile_id=str(p["id"]),
            image_url=str(p["image_url"]) if p["image_url"] else "",
            thumbnail_url=None,
            variant=p["attributes_json"].get("variant", 0),
            generation_params=p["meta_json"] or {}
        )
        for p in profiles
    ]

@router.get("/config/regions", response_model=List[RegionConfigView])
async def get_available_regions(
    language: str = "en"
) -> List[RegionConfigView]:
    """
    Get available regions for face generation.
    """
    pool = await get_pool()
    config_repo = FaceConfigRepo(pool)
    
    regions = await config_repo.get_regions(language=language, active_only=True)
    
    return [
        RegionConfigView(
            code=r["code"],
            display_name=r["display_name"],
            sub_region=r.get("sub_region"),
            is_active=True
        )
        for r in regions
    ]

@router.get("/config/contexts", response_model=List[ContextConfigView])
async def get_available_contexts() -> List[ContextConfigView]:
    """
    Get available socioeconomic contexts.
    """
    pool = await get_pool()
    config_repo = FaceConfigRepo(pool)
    
    contexts = await config_repo.get_contexts()
    
    return [
        ContextConfigView(
            code=c["code"],
            display_name=c["code"].replace("_", " ").title(),
            economic_class=c["economic_class"],
            glamour_level=c["glamour_level"],
            is_active=True
        )
        for c in contexts
    ]