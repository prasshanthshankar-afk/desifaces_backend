# services/svc-face/app/app/services/face_orchestrator.py
from __future__ import annotations
import asyncio
import logging
import hashlib
import json
from typing import Any, Dict, List, Optional
import asyncpg

from app.domain.models import FaceGenerateRequest
from app.repos.face_jobs_repo import FaceJobsRepo
from app.repos.face_profiles_repo import FaceProfilesRepo
from app.repos.media_assets_repo import MediaAssetsRepo
from app.repos.config_repo import FaceConfigRepo
from app.services.safety_service import SafetyService
from app.services.translation_service import TranslationService
from app.services.creator_platform_prompt_engine import CreatorPlatformPromptEngine
from app.services.fal_client import FalClient
from app.services.azure_storage_service import AzureStorageService
from app.domain.creator_platform_models import CreatorPlatformFaceRequest

logger = logging.getLogger("face_orchestrator")

def _request_hash(payload: Dict[str, Any]) -> str:
    """Generate deterministic hash for idempotency"""
    stable = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(stable.encode()).hexdigest()[:16]

class FaceOrchestrator:
    """Main orchestrator for face generation pipeline"""
    
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.jobs_repo = FaceJobsRepo(pool)
        self.profiles_repo = FaceProfilesRepo(pool)
        self.assets_repo = MediaAssetsRepo(pool)
        self.config_repo = FaceConfigRepo(pool)
        
        self.safety = SafetyService()
        self.translation = TranslationService()
        self.prompt_engine = CreatorPlatformPromptEngine(pool)
        self.fal = FalClient()
        self.storage = AzureStorageService()
    
    async def create_job(self, user_id: str, req: FaceGenerateRequest) -> str:
        """Create face generation job"""
        payload = req.model_dump()
        req_hash = _request_hash(payload)
        
        job_id = await self.jobs_repo.insert_job(
            user_id=user_id,
            request_hash=req_hash,
            payload=payload
        )
        
        logger.info("job_created", extra={"job_id": job_id, "user_id": user_id})

        return job_id
    
    async def run_job(self, job_id: str) -> None:
        """Execute face generation job"""
        job = await self.jobs_repo.get_job(job_id)
        logger.debug("fetched_job", extra={"job_id": job_id, "job": job})

        if not job:
            logger.warning("job_not_found", extra={"job_id": job_id})
            return
        
        status = str(job.get("status") or "")
        if status in ("succeeded", "failed", "canceled"):
            logger.info("job_terminal_skip", extra={"job_id": job_id, "status": status})
            return
        
        logger.info("job_starting", extra={"job_id": job_id, "status": status})
        
        # Parse payload
        payload_json = job["payload_json"]
        if isinstance(payload_json, str):
            payload_json = json.loads(payload_json)
        
        logger.debug("parsed_payload", extra={"job_id": job_id, "payload": payload_json})

        # Detect request type and handle appropriately
        if 'age_range_code' in payload_json:
            # Creator platform request - map fields explicitly
            req = FaceGenerateRequest(
                mode=payload_json.get('mode', 'text-to-image'),
                gender=payload_json['gender'],
                age_group=payload_json['age_range_code'],
                region=payload_json['region_code'],
                style=payload_json['style_code'],
                num_variants=payload_json.get('num_variants', 4),
                user_prompt=payload_json.get('user_prompt'),
                language=payload_json.get('language', 'en'),
                context=payload_json.get('context'),
                preservation_strength=payload_json.get('preservation_strength', 0.7),
                source_image_url=payload_json.get('source_image_url')
            )
        else:
            # Legacy request - validate normally
            req = FaceGenerateRequest.model_validate(payload_json)
        
        user_id = str(job["user_id"])
        
        try:
            # ========================================
            # STEP 1: Validate & Translate
            # ========================================
            user_prompt = req.user_prompt or ""
            
            if user_prompt and req.language.value != "en":
                translated, success = await self.translation.translate_to_english(
                    user_prompt, req.language.value
                )
                if not success:
                    raise ValueError("Translation failed")
                
                is_valid = await self.translation.validate_translation(
                    user_prompt, translated, req.language.value
                )
                if not is_valid:
                    raise ValueError("Translation validation failed")
                
                user_prompt = translated
            
            logger.info("prompt_translated", extra={"job_id": job_id, "language": req.language.value, "original_length": len(req.user_prompt or ""), "translated_length": len(user_prompt)})

            is_safe, reason = await self.safety.validate_text(user_prompt)
            if not is_safe:
                raise ValueError(f"Unsafe content: {reason}")
            
            
            logger.info("prompt_validated", extra={"job_id": job_id})
            # ========================================
            # STEP 2: Fetch Diversity Config
            # ========================================
            region_config = await self.config_repo.get_region_by_code(req.region)
            
            logger.debug("fetched_region_config", extra={"job_id": job_id, "region_config": region_config})

            if not region_config:
                raise ValueError(f"Invalid region: {req.region}")
            
            context_config = None
            if hasattr(req, 'context') and req.context:
                context_config = await self.config_repo.get_context_by_code(req.context)
            
            skin_tones = await self.config_repo.get_skin_tones()
            facial_features = await self.config_repo.get_facial_features()
            
            logger.debug("fetched_diversity_configs", extra={"job_id": job_id, "skin_tones_count": len(skin_tones), "facial_features_count": len(facial_features)})

            # ========================================
            # STEP 3: Generate Diverse Prompts
            # ========================================
            logger.info("generating_prompts", extra={"job_id": job_id})
            
            creator_request = CreatorPlatformFaceRequest(
                mode=req.mode,
                gender=req.gender,
                age_range_code=req.age_group,
                skin_tone_code="medium_brown",  # Default or extract from payload
                region_code=req.region,
                style_code=req.style,
                image_format_code="instagram_portrait",  # Default or extract from payload
                use_case_code="brand_ambassador",  # Default or extract from payload
                num_variants=req.num_variants,
                user_prompt=req.user_prompt
            )

            logger.debug("creator_request_built", extra={"job_id": job_id, "creator_request": creator_request.model_dump()})

            prompts = await self.prompt_engine.generate_creator_variants(creator_request)
            
            logger.info("prompts_generated", extra={"job_id": job_id, "num_prompts": len(prompts)})

            safety_negative = self.safety.get_safety_negative_prompt()
            enhanced_prompts = [
                await self.prompt_engine.enhance_prompt_with_safety(p, safety_negative)
                for p in prompts
            ]
            
            logger.info("prompts_enhanced", extra={"job_id": job_id, "num_prompts": len(enhanced_prompts)})

            # ========================================
            # STEP 4: Generate Images
            # ========================================
            logger.info("generating_images", extra={"job_id": job_id, "num_variants": len(prompts)})
            
            face_profile_ids = []
            
            for idx, prompt_data in enumerate(enhanced_prompts):
                variant_num = idx + 1
                seed = idx * 100
                
                try:
                    # Generate image
                    if req.mode.value == "text-to-image":
                        image_result = await self.fal.generate_image(
                            prompt=prompt_data["prompt"],
                            negative_prompt=prompt_data["negative_prompt"],
                            seed=seed,
                            width=1024,
                            height=1024
                        )
                    else:  # image-to-image
                        if not hasattr(req, 'source_image_url') or not req.source_image_url:
                            raise ValueError("source_image_url required for image-to-image")
                        
                        image_result = await self.fal.generate_image_to_image(
                            prompt=prompt_data["prompt"],
                            negative_prompt=prompt_data["negative_prompt"],
                            image_url=str(req.source_image_url),
                            strength=req.preservation_strength,
                            seed=seed,
                            width=1024,
                            height=1024
                        )
                    
                    # Upload to Azure Blob
                    storage_path, sas_url = await self.storage.upload_from_url(
                        url=image_result["url"],
                        user_id=user_id,
                        job_id=job_id,
                        variant=variant_num
                    )
                    
                    logger.info("image_uploaded", extra={
                        "job_id": job_id,
                        "variant": variant_num,
                        "storage_path": storage_path
                    })

                    # Create media asset with EXACT method signature
                    asset_id = await self.assets_repo.create_asset(
                        user_id=user_id,
                        kind="face_image",
                        url=sas_url,
                        storage_path=storage_path,
                        content_type="image/jpeg",
                        size_bytes=150000,  # Approximate
                        meta_json={
                            "job_id": job_id,
                            "variant": variant_num,
                            "prompt": prompt_data["prompt"][:500],
                            "seed": seed,
                            "width": 1024,
                            "height": 1024
                        }
                    )
                    
                    logger.info("image_generated", extra={
                        "job_id": job_id,
                        "variant": variant_num,
                        "asset_id": asset_id
                    })

                    # Create face profile with correct method
                    face_profile_id = await self.profiles_repo.create_profile(
                        user_id=user_id,
                        display_name=f"Face {variant_num}",
                        primary_image_asset_id=asset_id,
                        attributes_json={
                            "region": req.region,
                            "gender": req.gender.value,
                            "age_group": req.age_group,
                            "style": req.style
                        },
                        meta_json={
                            "job_id": job_id,
                            "variant": variant_num,
                            "mode": req.mode.value,
                            "generation_prompt": prompt_data["prompt"][:500],
                            "seed": seed
                        }
                    )

                    logger.info("variant_completed", extra={
                        "job_id": job_id,
                        "variant": variant_num,
                        "face_profile_id": face_profile_id
                    })
                    
                    # Link job output
                    await self.profiles_repo.link_job_output(
                        job_id=job_id,
                        face_profile_id=face_profile_id,
                        output_asset_id=asset_id
                    )

                    face_profile_ids.append(face_profile_id)
                    
                    logger.info("variant_completed", extra={
                        "job_id": job_id,
                        "variant": variant_num,
                        "face_profile_id": face_profile_id
                    })

                except Exception as e:
                    import traceback
                    error_detail = traceback.format_exc()
                    logger.error("variant_failed", extra={
                        "job_id": job_id,
                        "variant": variant_num,
                        "error": str(e),
                        "traceback": error_detail
                    })
                    print(f"VARIANT {variant_num} FAILED: {str(e)}")
                    print(f"FULL TRACEBACK: {error_detail}")

            # ========================================
            # STEP 5: Mark Job Success
            # ========================================
            if len(face_profile_ids) == 0:
                raise Exception("All variants failed")
            
            await self.jobs_repo.set_status(job_id, "succeeded")
            
            logger.info("job_succeeded", extra={
                "job_id": job_id,
                "faces_generated": len(face_profile_ids)
            })
        
        except Exception as e:
            error_msg = str(e)
            logger.exception("job_failed", extra={"job_id": job_id, "error": error_msg})
            
            await self.jobs_repo.set_status(
                job_id,
                "failed",
                error_code="FACE_GENERATION_FAILED",
                error_message=error_msg
            )