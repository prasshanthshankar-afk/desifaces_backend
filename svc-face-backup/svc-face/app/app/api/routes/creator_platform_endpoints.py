# services/svc-face/app/api/creator_platform_endpoints.py
from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any, List
import logging

from app.domain.creator_platform_models import (
    CreatorPlatformFaceRequest, 
    CreatorPlatformGenerationResult,
    CreatorPlatformConfig
)
from app.services.face_orchestrator import FaceOrchestrator
from app.repos.creator_platform_config_repo import CreatorPlatformConfigRepo
from app.db import get_pool
from app.api.deps import get_current_user_id

router = APIRouter(prefix="/api/face/creator", tags=["Creator Platform"])
logger = logging.getLogger("creator_platform_api")

@router.get("/config", response_model=CreatorPlatformConfig)
async def get_creator_platform_config(
    platform_filter: str = None,
    language: str = "en",
    pool = Depends(get_pool)
):
    """
    Get complete creator platform configuration for UI.
    Includes all image formats, use cases, age ranges, variations, etc.
    """
    try:
        config_repo = CreatorPlatformConfigRepo(pool)
        config = await config_repo.get_complete_creator_config(
            language=language,
            platform_filter=platform_filter
        )
        
        logger.info("Creator config requested", extra={
            "language": language,
            "platform_filter": platform_filter,
            "formats_count": len(config.image_formats),
            "use_cases_count": len(config.use_cases)
        })
        
        return config
        
    except Exception as e:
        logger.error("Failed to get creator config", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to get configuration: {str(e)}")

@router.get("/formats")
async def get_image_formats(
    platform_category: str = None,
    pool = Depends(get_pool)
):
    """Get available image formats, optionally filtered by platform category"""
    try:
        config_repo = CreatorPlatformConfigRepo(pool)
        formats = await config_repo.get_image_formats(platform_category)
        return {"formats": formats}
        
    except Exception as e:
        logger.error("Failed to get image formats", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to get formats: {str(e)}")

@router.get("/use-cases")
async def get_use_cases(
    category: str = None,
    image_format_code: str = None,
    pool = Depends(get_pool)
):
    """Get available use cases, optionally filtered by category or compatible with format"""
    try:
        config_repo = CreatorPlatformConfigRepo(pool)
        
        if image_format_code:
            use_cases = await config_repo.get_use_cases_for_format(image_format_code)
        else:
            use_cases = await config_repo.get_use_cases(category)
            
        return {"use_cases": use_cases}
        
    except Exception as e:
        logger.error("Failed to get use cases", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to get use cases: {str(e)}")

@router.get("/variations")
async def get_creative_variations(
    variation_type: str = None,
    use_case_code: str = None,
    pool = Depends(get_pool)
):
    """Get creative variations, optionally filtered by type or use case compatibility"""
    try:
        config_repo = CreatorPlatformConfigRepo(pool)
        variations = await config_repo.get_creative_variations(
            variation_type=variation_type,
            use_case_code=use_case_code
        )
        
        if use_case_code:
            # Group by variation type for easier UI consumption
            grouped = {}
            for variation in variations:
                var_type = variation.variation_type
                if var_type not in grouped:
                    grouped[var_type] = []
                grouped[var_type].append(variation)
            return {"variations_by_type": grouped}
        else:
            return {"variations": variations}
        
    except Exception as e:
        logger.error("Failed to get variations", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to get variations: {str(e)}")

@router.post("/validate")
async def validate_creator_request(
    request: CreatorPlatformFaceRequest,
    pool = Depends(get_pool)
):
    """Validate creator platform request configuration compatibility"""
    try:
        config_repo = CreatorPlatformConfigRepo(pool)
        validation = await config_repo.validate_request_config(
            image_format_code=request.image_format_code,
            use_case_code=request.use_case_code,
            age_range_code=request.age_range_code,
            platform_code=request.platform_code
        )
        
        return {
            "valid": validation.get('image_format_valid', False) and 
                    validation.get('use_case_valid', False) and 
                    validation.get('age_range_valid', False),
            "validation_details": validation
        }
        
    except Exception as e:
        logger.error("Failed to validate request", extra={"error": str(e)})
        raise HTTPException(status_code=400, detail=f"Validation failed: {str(e)}")

@router.get("/recommendations")
async def get_recommendations(
    use_case_code: str,
    platform_code: str = None,
    pool = Depends(get_pool)
):
    """Get recommended configurations for a use case and platform"""
    try:
        config_repo = CreatorPlatformConfigRepo(pool)
        recommendations = await config_repo.get_recommended_config(
            use_case_code=use_case_code,
            platform_code=platform_code
        )
        return recommendations
        
    except Exception as e:
        logger.error("Failed to get recommendations", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to get recommendations: {str(e)}")

@router.post("/generate", response_model=Dict[str, Any])
async def generate_creator_face(
    request: CreatorPlatformFaceRequest,
    pool = Depends(get_pool),
    user_id: str = Depends(get_current_user_id)
):
    """
    Generate creator platform optimized faces.
    
    Returns job_id for tracking. Use /job/{job_id}/status to monitor progress.
    """
    try:
        orchestrator = FaceOrchestrator(pool)
        
        # Create job
        job_id = await orchestrator.create_job(user_id, request)
        
        logger.info("Creator face generation job created", extra={
            "job_id": job_id,
            "user_id": user_id,
            "use_case": request.use_case_code,
            "image_format": request.image_format_code,
            "num_variants": request.num_variants
        })
        
        # Start job processing (async)
        # Note: In production, this should be queued to a background worker
        import asyncio
        asyncio.create_task(orchestrator.run_job(job_id))
        
        return {
            "job_id": job_id,
            "status": "queued",
            "message": "Creator face generation started",
            "estimated_completion_time": f"{request.num_variants * 30} seconds",
            "config": {
                "use_case": request.use_case_code,
                "image_format": request.image_format_code,
                "platform_optimized": request.platform_code is not None,
                "variants_requested": request.num_variants
            }
        }
        
    except Exception as e:
        logger.error("Creator face generation failed", extra={
            "error": str(e),
            "user_id": user_id,
            "request": request.model_dump()
        })
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")

@router.get("/job/{job_id}/status")
async def get_job_status(
    job_id: str,
    pool = Depends(get_pool),
    user_id: str = Depends(get_current_user_id)
):
    """Get creator platform job status and results"""
    try:
        # TODO: Implement job status checking with FaceOrchestrator
        # For now, return placeholder
        return {
            "job_id": job_id,
            "status": "processing",
            "message": "Generating creator platform variants...",
            "progress": {
                "variants_completed": 2,
                "variants_total": 4,
                "current_step": "Generating images"
            }
        }
        
    except Exception as e:
        logger.error("Failed to get job status", extra={"error": str(e), "job_id": job_id})
        raise HTTPException(status_code=500, detail=f"Failed to get status: {str(e)}")

# Helper endpoint for UI development
@router.get("/platform-formats")
async def get_platform_formats(
    platform_code: str,
    pool = Depends(get_pool)
):
    """Get image formats compatible with specific platform"""
    try:
        config_repo = CreatorPlatformConfigRepo(pool)
        formats = await config_repo.get_formats_for_platform(platform_code)
        return {"platform": platform_code, "formats": formats}
        
    except Exception as e:
        logger.error("Failed to get platform formats", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to get platform formats: {str(e)}")