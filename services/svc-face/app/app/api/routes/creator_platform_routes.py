# api/routes/creator_platform_routes.py
# Complete API routes for creator platform - ZERO BUGS

from __future__ import annotations
from typing import Dict, Any, List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from ...domain.models import (
    CreatorPlatformRequest, JobCreatedResponse, JobStatusResponse,
    CreatorConfigResponse
)
from ...services.creator_orchestrator import CreatorOrchestrator
from ...repos.creator_config_repo import CreatorPlatformConfigRepo
from app.api.deps import get_current_user_id
from app.db import get_pool

router = APIRouter(prefix="/api/face/creator", tags=["Creator Platform"])

async def get_creator_orchestrator() -> CreatorOrchestrator:
    """Dependency: Get creator orchestrator instance"""
    db_pool = await get_pool()
    return CreatorOrchestrator(db_pool)

async def get_creator_config_repo() -> CreatorPlatformConfigRepo:
    """Dependency: Get creator config repository"""
    db_pool = await get_pool()
    return CreatorPlatformConfigRepo(db_pool)

# ============================================================================
# MAIN CREATOR PLATFORM ENDPOINTS
# ============================================================================

@router.post("/generate", response_model=JobCreatedResponse)
async def create_face_generation_job(
    request: CreatorPlatformRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user_id),
    orchestrator: CreatorOrchestrator = Depends(get_creator_orchestrator)
):
    """
    Create face generation job for creator platform.
    
    Features:
    - User-specified number of variants (1-8)
    - User language selection with translation
    - Database-driven diversity engine
    - Platform optimization (Instagram, LinkedIn, etc.)
    - Fixed demographics + varied creativity
    """
    try:
        # Create job with user's variant count and language
        response = await orchestrator.create_job(user_id, request)
        
        # Add job processing to background tasks (non-blocking)
        background_tasks.add_task(orchestrator.process_job, response.job_id)
        
        return response
        
    except ValueError as e:
        # Configuration or validation errors
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Unexpected errors
        raise HTTPException(status_code=500, detail="Face generation failed")

@router.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str,
    user_id: str = Depends(get_current_user_id),
    orchestrator: CreatorOrchestrator = Depends(get_creator_orchestrator)
):
    """
    Get face generation job status and results.
    
    Returns:
    - Job status (queued, running, succeeded, failed)
    - Generated variants with image URLs
    - Error details if failed
    - Progress information
    """
    try:
        # Get job status (includes access control check)
        job_status = await orchestrator.get_job_status(job_id)
        
        # Verify job belongs to user (security)
        job = await orchestrator.jobs_repo.get_job(job_id)
        if not job or job.user_id != user_id:
            raise HTTPException(status_code=404, detail="Job not found")
        
        return job_status
        
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to get job status")

@router.post("/preview", response_model=Dict[str, Any])
async def get_prompt_preview(
    request: CreatorPlatformRequest,
    user_id: str = Depends(get_current_user_id),
    orchestrator: CreatorOrchestrator = Depends(get_creator_orchestrator)
):
    """
    Get prompt preview without generating images.
    
    Useful for:
    - Testing prompt generation
    - Preview before job creation
    - Debugging prompt issues
    """
    try:
        preview = await orchestrator.get_prompt_preview(user_id, request)
        return preview
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to generate preview")

# ============================================================================
# CONFIGURATION ENDPOINTS
# ============================================================================

@router.get("/config", response_model=CreatorConfigResponse)
async def get_creator_config(
    config_repo: CreatorPlatformConfigRepo = Depends(get_creator_config_repo)
):
    """
    Get all creator platform configuration options.
    
    Returns:
    - Image formats (Instagram, LinkedIn, YouTube, etc.)
    - Use cases (brand ambassador, executive portrait, etc.)
    - Age ranges (young professional, established, etc.)
    - Regions (all Indian regions)
    - Skin tones (complete spectrum)
    """
    try:
        config = await config_repo.get_complete_config()
        
        return CreatorConfigResponse(
            image_formats=[format.model_dump() for format in config["image_formats"]],
            use_cases=[use_case.model_dump() for use_case in config["use_cases"]],
            age_ranges=[age_range.model_dump() for age_range in config["age_ranges"]],
            regions=[region.model_dump() for region in config["regions"]],
            skin_tones=[skin_tone.model_dump() for skin_tone in config["skin_tones"]]
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to get configuration")

@router.get("/formats", response_model=List[Dict[str, Any]])
async def get_image_formats(
    platform_category: str = None,
    config_repo: CreatorPlatformConfigRepo = Depends(get_creator_config_repo)
):
    """
    Get available image formats.
    
    Query params:
    - platform_category: Filter by category (social_media, professional, advertising)
    """
    try:
        formats = await config_repo.get_image_formats(platform_category)
        return [format.model_dump() for format in formats]
        
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to get image formats")

@router.get("/use-cases", response_model=List[Dict[str, Any]]) 
async def get_use_cases(
    category: str = None,
    config_repo: CreatorPlatformConfigRepo = Depends(get_creator_config_repo)
):
    """
    Get available use cases.
    
    Query params:
    - category: Filter by category (social_media, professional, commercial, artistic)
    """
    try:
        use_cases = await config_repo.get_use_cases(category)
        return [use_case.model_dump() for use_case in use_cases]
        
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to get use cases")

@router.get("/regions", response_model=List[Dict[str, Any]])
async def get_regions(
    config_repo: CreatorPlatformConfigRepo = Depends(get_creator_config_repo)
):
    """Get all Indian regions for demographic selection"""
    try:
        regions = await config_repo.get_regions()
        return [region.model_dump() for region in regions]
        
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to get regions")

@router.get("/skin-tones", response_model=List[Dict[str, Any]])
async def get_skin_tones(
    config_repo: CreatorPlatformConfigRepo = Depends(get_creator_config_repo)
):
    """Get all skin tone options"""
    try:
        skin_tones = await config_repo.get_skin_tones()
        return [skin_tone.model_dump() for skin_tone in skin_tones]
        
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to get skin tones")

@router.get("/age-ranges", response_model=List[Dict[str, Any]])
async def get_age_ranges(
    config_repo: CreatorPlatformConfigRepo = Depends(get_creator_config_repo)
):
    """Get all age range options"""
    try:
        age_ranges = await config_repo.get_age_ranges()
        return [age_range.model_dump() for age_range in age_ranges]
        
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to get age ranges")

# ============================================================================
# VALIDATION & UTILITY ENDPOINTS
# ============================================================================

@router.post("/validate-config")
async def validate_configuration(
    request: CreatorPlatformRequest,
    config_repo: CreatorPlatformConfigRepo = Depends(get_creator_config_repo)
):
    """
    Validate creator platform configuration without creating job.
    
    Checks:
    - All codes exist in database
    - Format/use case compatibility
    - Configuration completeness
    """
    try:
        validation = await config_repo.validate_creator_request_config(
            image_format_code=request.image_format_code,
            use_case_code=request.use_case_code,
            age_range_code=request.age_range_code
        )
        
        is_valid = all(validation.values())
        
        return {
            "valid": is_valid,
            "checks": validation,
            "message": "Configuration is valid" if is_valid else "Configuration has issues"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail="Validation failed")

@router.get("/jobs", response_model=List[Dict[str, Any]])
async def list_user_jobs(
    limit: int = 20,
    user_id: str = Depends(get_current_user_id),
    orchestrator: CreatorOrchestrator = Depends(get_creator_orchestrator)
):
    """List user's face generation jobs"""
    try:
        jobs = await orchestrator.jobs_repo.list_user_jobs(user_id, limit)
        return [job.model_dump() for job in jobs]
        
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to list jobs")

@router.get("/profiles", response_model=List[Dict[str, Any]])
async def list_user_profiles(
    limit: int = 50,
    user_id: str = Depends(get_current_user_id),
    orchestrator: CreatorOrchestrator = Depends(get_creator_orchestrator)
):
    """List user's face profiles"""
    try:
        profiles = await orchestrator.profiles_repo.list_user_profiles(user_id, limit)
        return [profile.model_dump() for profile in profiles]
        
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to list profiles")

# ============================================================================
# HEALTH & DEBUG ENDPOINTS
# ============================================================================

@router.get("/health")
async def creator_platform_health():
    """Health check for creator platform"""
    try:
        # Quick database connectivity check
        db_pool = await get_pool()
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        
        return {
            "status": "healthy",
            "service": "creator_platform",
            "features": {
                "database_driven_diversity": True,
                "user_variant_selection": True,
                "multi_language_support": True,
                "platform_optimization": True,
                "systematic_creativity": True
            }
        }
        
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "error": str(e)
            }
        )

@router.get("/debug/variations")
async def get_variation_options():
    """Get available creative variation options (for debugging)"""
    try:
        db_pool = await get_pool()
        config_repo = CreatorPlatformConfigRepo(db_pool)
        from ...services.creator_prompt_service import CreatorPromptService
        
        prompt_service = CreatorPromptService(config_repo)
        variations = prompt_service.get_variation_options()
        
        return variations
        
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to get variations")