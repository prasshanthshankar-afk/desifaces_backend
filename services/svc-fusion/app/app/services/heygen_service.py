from __future__ import annotations

import asyncio
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from pathlib import Path

from app.services.providers.heygen.client import HeyGenAV4Client, HeyGenApiError
from app.services.azure_storage import AzureStorageService
from app.config import settings

logger = logging.getLogger("heygen_service")


class HeyGenService:
    """
    High-level service for HeyGen video generation
    
    Handles:
    - Image upload from local filesystem
    - Audio URL generation from Azure Blob
    - Video submission and polling
    - Error handling and retries
    """
    
    def __init__(self):
        self.client = HeyGenAV4Client()
        self.azure_storage = AzureStorageService() if hasattr(settings, 'AZURE_STORAGE_CONNECTION_STRING') else None
    
    async def create_video_from_azure_assets(
        self,
        face_image_path: str,
        audio_blob_path: str,
        idempotency_key: str,
        dimension: Dict[str, int] = None,
        test_mode: bool = False,
        max_poll_time: int = 600
    ) -> Dict[str, Any]:
        """
        Complete workflow: Create video from face image and Azure TTS audio
        
        Args:
            face_image_path: Local path to face image (downloaded from Azure Blob)
            audio_blob_path: Path to audio in Azure Blob Storage
            idempotency_key: Unique identifier for this request
            dimension: Video dimensions (default: 1920x1080)
            test_mode: If True, creates test video (faster)
            max_poll_time: Maximum seconds to wait for completion
            
        Returns:
            dict with:
                - video_id: HeyGen video ID
                - video_url: Download URL when complete
                - status: 'succeeded' | 'failed' | 'timeout'
                - duration: Time taken in seconds
                
        Raises:
            HeyGenApiError: If any step fails critically
            FileNotFoundError: If face image doesn't exist
        """
        start_time = datetime.now()
        
        try:
            logger.info(f"Starting HeyGen video generation: {idempotency_key}")
            
            # Step 1: Upload face image to HeyGen
            logger.info("Step 1/4: Uploading face image...")
            talking_photo_id = await self.client.upload_image(face_image_path)
            logger.info(f"✓ Image uploaded: {talking_photo_id}")
            
            # Step 2: Generate Azure Blob SAS URL for audio
            logger.info("Step 2/4: Generating audio SAS URL...")
            audio_url = await self._get_audio_sas_url(audio_blob_path)
            logger.info(f"✓ Audio URL ready: {audio_url[:60]}...")
            
            # Step 3: Submit video generation
            logger.info("Step 3/4: Submitting video generation...")
            result = await self.client.submit_with_audio_url(
                talking_photo_id=talking_photo_id,
                audio_url=audio_url,
                idempotency_key=idempotency_key,
                dimension=dimension,
                test=test_mode
            )
            video_id = result.provider_job_id
            logger.info(f"✓ Video submitted: {video_id}")
            
            # Step 4: Poll for completion
            logger.info(f"Step 4/4: Polling for completion (max {max_poll_time}s)...")
            video_url = await self._poll_until_complete(video_id, max_poll_time)
            
            duration = (datetime.now() - start_time).total_seconds()
            
            if video_url:
                logger.info(f"✓✓✓ Video completed in {duration:.1f}s: {video_url}")
                return {
                    "video_id": video_id,
                    "video_url": video_url,
                    "status": "succeeded",
                    "duration": duration,
                    "talking_photo_id": talking_photo_id
                }
            else:
                logger.warning(f"Video generation timeout after {duration:.1f}s")
                return {
                    "video_id": video_id,
                    "video_url": None,
                    "status": "timeout",
                    "duration": duration,
                    "talking_photo_id": talking_photo_id
                }
        
        except Exception as e:
            duration = (datetime.now() - start_time).total_seconds()
            logger.error(f"Video generation failed after {duration:.1f}s: {e}")
            raise
    
    async def create_video_with_text(
        self,
        face_image_path: str,
        script: str,
        idempotency_key: str,
        voice_id: Optional[str] = None,
        dimension: Dict[str, int] = None,
        test_mode: bool = False,
        max_poll_time: int = 600
    ) -> Dict[str, Any]:
        """
        Create video using HeyGen's TTS (alternative to Azure TTS)
        
        Args:
            face_image_path: Local path to face image
            script: Text for avatar to speak
            idempotency_key: Unique identifier
            voice_id: HeyGen voice ID (auto-validates if invalid)
            dimension: Video dimensions
            test_mode: Test mode flag
            max_poll_time: Max wait time
            
        Returns:
            Result dict (same structure as create_video_from_azure_assets)
        """
        start_time = datetime.now()
        
        try:
            logger.info(f"Starting HeyGen video with text: {idempotency_key}")
            
            # Upload image
            logger.info("Uploading face image...")
            talking_photo_id = await self.client.upload_image(face_image_path)
            
            # Submit with text
            logger.info("Submitting video with text script...")
            result = await self.client.submit_with_text(
                talking_photo_id=talking_photo_id,
                script=script,
                idempotency_key=idempotency_key,
                voice_id=voice_id,
                dimension=dimension,
                test=test_mode
            )
            video_id = result.provider_job_id
            
            # Poll for completion
            logger.info(f"Polling video {video_id}...")
            video_url = await self._poll_until_complete(video_id, max_poll_time)
            
            duration = (datetime.now() - start_time).total_seconds()
            
            if video_url:
                return {
                    "video_id": video_id,
                    "video_url": video_url,
                    "status": "succeeded",
                    "duration": duration
                }
            else:
                return {
                    "video_id": video_id,
                    "video_url": None,
                    "status": "timeout",
                    "duration": duration
                }
        
        except Exception as e:
            duration = (datetime.now() - start_time).total_seconds()
            logger.error(f"Video generation failed after {duration:.1f}s: {e}")
            raise
    
    async def _get_audio_sas_url(self, audio_blob_path: str, expiry_hours: int = 2) -> str:
        """
        Generate Azure Blob SAS URL for audio file
        
        Args:
            audio_blob_path: Path to audio in Azure Blob
            expiry_hours: SAS token validity period
            
        Returns:
            Public SAS URL
        """
        if self.azure_storage:
            return await self.azure_storage.generate_sas_url(
                audio_blob_path,
                expiry_hours=expiry_hours
            )
        
        # Fallback: assume audio_blob_path is already a URL
        if audio_blob_path.startswith('http'):
            return audio_blob_path
        
        raise HeyGenApiError(
            "Azure Storage not configured and audio_blob_path is not a URL. "
            "Set AZURE_STORAGE_CONNECTION_STRING or provide a public URL."
        )
    
    async def _poll_until_complete(
        self,
        video_id: str,
        max_wait_seconds: int = 600,
        poll_interval: int = 10
    ) -> Optional[str]:
        """
        Poll video status until completed or timeout
        
        Args:
            video_id: HeyGen video ID
            max_wait_seconds: Maximum wait time
            poll_interval: Seconds between polls
            
        Returns:
            video_url if succeeded, None if timeout or failed
            
        Raises:
            HeyGenApiError: If video generation fails
        """
        iterations = max_wait_seconds // poll_interval
        
        for i in range(iterations):
            await asyncio.sleep(poll_interval)
            
            try:
                poll_result = await self.client.poll(video_id)
                
                logger.info(f"[{i+1}/{iterations}] Video {video_id}: {poll_result.status}")
                
                if poll_result.status == "succeeded":
                    if not poll_result.video_url:
                        logger.warning("Video succeeded but no video_url in response")
                    return poll_result.video_url
                
                elif poll_result.status == "failed":
                    error_msg = poll_result.error_message or "Unknown error"
                    logger.error(f"Video generation failed: {error_msg}")
                    raise HeyGenApiError(f"Video generation failed: {error_msg}")
            
            except HeyGenApiError as e:
                # If it's a failed status, re-raise
                if "failed" in str(e).lower():
                    raise
                # Otherwise log and continue (might be transient)
                logger.warning(f"Poll attempt {i+1} error: {e}")
        
        logger.warning(f"Video {video_id} still processing after {max_wait_seconds}s")
        return None
    
    async def get_video_status(self, video_id: str) -> Dict[str, Any]:
        """
        Get current status of a video
        
        Args:
            video_id: HeyGen video ID
            
        Returns:
            dict with status, video_url (if available), and raw response
        """
        poll_result = await self.client.poll(video_id)
        
        return {
            "video_id": video_id,
            "status": poll_result.status,
            "video_url": poll_result.video_url,
            "error_message": poll_result.error_message,
            "raw_response": poll_result.raw_response
        }
    
    async def get_available_voices(self) -> list:
        """
        Get list of available HeyGen voices
        
        Returns:
            List of voice dictionaries with id, name, language, gender
        """
        return await self.client.get_available_voices()
    
    async def validate_and_fix_voice_id(self, voice_id: Optional[str]) -> str:
        """
        Validate voice ID and return valid one (or default)
        
        Args:
            voice_id: Voice ID to validate
            
        Returns:
            Valid voice_id
        """
        return await self.client.validate_voice_id(voice_id)


# ==============================================================================
# AZURE STORAGE SERVICE (STUB - Implement based on your setup)
# ==============================================================================

class AzureStorageService:
    """
    Azure Blob Storage service for generating SAS URLs
    Location: app/services/azure_storage.py
    """
    
    async def generate_sas_url(
        self,
        blob_path: str,
        expiry_hours: int = 2
    ) -> str:
        """
        Generate SAS URL for blob
        
        Args:
            blob_path: Path to blob in container
            expiry_hours: Validity period
            
        Returns:
            Public SAS URL
        """
        from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
        from datetime import datetime, timedelta
        
        # Parse blob path
        # Expected format: "container/path/to/file.mp3" or just "path/to/file.mp3"
        parts = blob_path.split('/', 1)
        if len(parts) == 2:
            container_name, blob_name = parts
        else:
            container_name = settings.AZURE_AUDIO_CONTAINER
            blob_name = blob_path
        
        # Create blob client
        blob_service_client = BlobServiceClient.from_connection_string(
            settings.AZURE_STORAGE_CONNECTION_STRING
        )
        
        blob_client = blob_service_client.get_blob_client(
            container=container_name,
            blob=blob_name
        )
        
        # Generate SAS token
        sas_token = generate_blob_sas(
            account_name=blob_client.account_name,
            container_name=container_name,
            blob_name=blob_name,
            account_key=blob_service_client.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(hours=expiry_hours)
        )
        
        # Construct full URL
        sas_url = f"{blob_client.url}?{sas_token}"
        
        logger.info(f"Generated SAS URL for {blob_name}")
        
        return sas_url