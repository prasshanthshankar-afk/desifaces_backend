from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

from app.config import settings
from app.domain.models import FusionJobCreate
from app.services.providers.heygen.av4_payload import build_av4_payload
from app.services.providers.heygen.client import HeyGenAV4Client, HeyGenApiError

logger = logging.getLogger("heygen_service")


class HeyGenService:
    """
    Stable HeyGen service for DesiFaces Fusion.

    Product contract:
    - Audio Studio remains the exact speech source.
    - Fusion submits quickly and lets the worker own polling/recovery.
    - Video-direction prompt is preserved in DesiFaces metadata without
      mutating the provider into script/timed-script mode.
    - The exact-audio avatar-video route must stay provider-safe and MUST NOT
      accidentally fall back into a script / timed-script request shape.
    """

    def __init__(self):
        self.client = HeyGenAV4Client()
        self.azure_storage = (
            AzureStorageService()
            if getattr(settings, "AZURE_STORAGE_CONNECTION_STRING", None)
            else None
        )

    @staticmethod
    def _coerce_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _clean_text(value: Any) -> str:
        return str(value or "").strip()

    def _req_dict(self, req: Optional[FusionJobCreate]) -> Dict[str, Any]:
        if req is None:
            return {}
        try:
            dumped = req.model_dump(mode="python", exclude_none=False)
            return dumped if isinstance(dumped, dict) else {}
        except Exception:
            return {}

    def _derive_dimension_from_req(self, req: Optional[FusionJobCreate]) -> Optional[Dict[str, int]]:
        if req is None:
            return None
        try:
            video = req.video.model_dump()
        except Exception:
            video = {}
        aspect_ratio = str(video.get("aspect_ratio") or "9:16").strip()
        if aspect_ratio == "16:9":
            return {"width": 1920, "height": 1080}
        if aspect_ratio == "1:1":
            return {"width": 1080, "height": 1080}
        return {"width": 1080, "height": 1920}

    def _derive_background_override_from_req(self, req: Optional[FusionJobCreate]) -> Optional[Dict[str, Any]]:
        req_dict = self._req_dict(req)
        provider_options = self._coerce_dict(req_dict.get("provider_options"))
        for key in ("background_override", "heygen_background_override"):
            value = provider_options.get(key)
            if isinstance(value, dict) and value:
                return dict(value)
        return None

    def _build_submit_meta(
        self,
        *,
        req: Optional[FusionJobCreate],
        creative_prompt: Optional[str],
        dimension: Optional[Dict[str, int]],
        background_override: Optional[Dict[str, Any]],
        talking_photo_id: str,
        audio_url: str,
    ) -> Dict[str, Any]:
        req_dict = self._req_dict(req)
        provider_options = self._coerce_dict(req_dict.get("provider_options"))
        video = self._coerce_dict(req_dict.get("video"))
        tags = self._coerce_dict(req_dict.get("tags"))

        return {
            "provider": "heygen_av4",
            "mode": "exact_audio_talking_photo",
            "talking_photo_id": talking_photo_id,
            "audio_url_preview": self._clean_text(audio_url)[:160] or None,
            "prompt_preview": self._clean_text(creative_prompt)[:160] or None,
            "aspect_ratio": self._clean_text(video.get("aspect_ratio")) or None,
            "duration_sec": video.get("duration_sec"),
            "dimension": dict(dimension or {}),
            "camera_angle": self._clean_text(provider_options.get("camera_angle")) or self._clean_text(tags.get("camera_angle")) or None,
            "camera_framing": self._clean_text(provider_options.get("camera_framing")) or self._clean_text(tags.get("camera_framing")) or None,
            "camera_motion_style": self._clean_text(provider_options.get("camera_motion_style")) or self._clean_text(tags.get("camera_motion_style")) or None,
            "background_override": dict(background_override or {}),
        }

    def _force_exact_audio_payload(
        self,
        *,
        payload: Dict[str, Any],
        talking_photo_id: str,
        audio_url: str,
        dimension: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """
        Hard-stop any accidental script/text mode and force a pure audio-backed
        talking-photo payload.

        This protects us even if build_av4_payload(...) starts injecting fields
        from FusionJobCreate.voice_tts or provider options in the future.
        """
        out = dict(payload or {})
        raw_inputs = out.get("video_inputs")
        if isinstance(raw_inputs, list) and raw_inputs:
            video_input = self._coerce_dict(raw_inputs[0])
        else:
            video_input = {}

        character = dict(self._coerce_dict(video_input.get("character")))
        character["type"] = "talking_photo"
        character["talking_photo_id"] = talking_photo_id
        for key in (
            "avatar_id",
            "photo_avatar_id",
            "character_id",
            "asset_id",
            "photo_id",
            "image_key",
            "url",
        ):
            character.pop(key, None)
        video_input["character"] = character

        video_input["voice"] = {
            "type": "audio",
            "audio_url": audio_url,
        }

        # Remove fields that can push the provider into script / timed-script mode.
        for key in (
            "script",
            "script_id",
            "script_text",
            "text",
            "input_text",
            "voice_text",
            "ssml",
            "word_timestamps",
            "word_timings",
            "timed_metadata",
            "audio",
            "audio_blob_path",
            "audio_asset_id",
        ):
            video_input.pop(key, None)
            out.pop(key, None)

        # Keep a single exact-audio video input.
        out["video_inputs"] = [video_input]
        out["use_avatar_iv_model"] = True

        # Preserve existing size settings from build_av4_payload, but if none are
        # present and the caller provided dimensions, add the common width/height keys.
        if dimension and isinstance(dimension, dict):
            width = dimension.get("width")
            height = dimension.get("height")
            if width and height:
                video = dict(self._coerce_dict(out.get("video")))
                if not video.get("width") and not video.get("height"):
                    video["width"] = int(width)
                    video["height"] = int(height)
                out["video"] = video

        return out

    async def submit_exact_audio_video(
        self,
        *,
        req: FusionJobCreate,
        face_image_path: str,
        audio_blob_path: str,
        idempotency_key: str,
        creative_prompt: Optional[str] = None,
        background_override: Optional[Dict[str, Any]] = None,
        dimension: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """
        Submit a non-blocking exact-audio talking-photo job and return the provider job id.
        """
        if req is None:
            raise HeyGenApiError("submit_exact_audio_video requires req")
        if getattr(req, "voice_mode", None) and getattr(req.voice_mode, "value", None) != "audio":
            raise HeyGenApiError("HeyGen exact-audio flow requires voice_mode=audio")
        if not Path(face_image_path).exists():
            raise FileNotFoundError(face_image_path)

        effective_dimension = dict(dimension or self._derive_dimension_from_req(req) or {})
        effective_background_override = background_override or self._derive_background_override_from_req(req)

        talking_photo_id = await self.client.upload_talking_photo(face_image_path)
        audio_url = await self._get_audio_sas_url(audio_blob_path)

        base_payload = build_av4_payload(
            req,
            talking_photo_id=talking_photo_id,
            video_title="DesiFaces Fusion",
            audio_url_override=audio_url,
            background_override=effective_background_override,
        )
        payload = self._force_exact_audio_payload(
            payload=base_payload,
            talking_photo_id=talking_photo_id,
            audio_url=audio_url,
            dimension=effective_dimension,
        )

        submit_res = await self.client.submit(payload, idempotency_key)
        submit_meta = self._build_submit_meta(
            req=req,
            creative_prompt=creative_prompt,
            dimension=effective_dimension,
            background_override=effective_background_override,
            talking_photo_id=talking_photo_id,
            audio_url=audio_url,
        )
        return {
            "provider_job_id": submit_res.provider_job_id,
            "talking_photo_id": talking_photo_id,
            "audio_url": audio_url,
            "request_payload": payload,
            "prompt_preview": (creative_prompt or "").strip()[:160] or None,
            "submit_meta": submit_meta,
            "raw_response": submit_res.raw_response,
        }

    async def submit_video_from_azure_assets(
        self,
        face_image_path: str,
        audio_blob_path: str,
        idempotency_key: str,
        dimension: Dict[str, int] | None = None,
        test_mode: bool = False,
        creative_prompt: Optional[str] = None,
        req: Optional[FusionJobCreate] = None,
    ) -> Dict[str, Any]:
        """
        Compatibility wrapper for existing Fusion callers.

        Long-term callers should prefer submit_exact_audio_video(...).
        """
        if req is None:
            raise HeyGenApiError(
                "submit_video_from_azure_assets requires req for the stable exact-audio flow."
            )
        return await self.submit_exact_audio_video(
            req=req,
            face_image_path=face_image_path,
            audio_blob_path=audio_blob_path,
            idempotency_key=idempotency_key,
            creative_prompt=creative_prompt,
            dimension=dimension,
        )

    async def create_video_from_azure_assets(
        self,
        face_image_path: str,
        audio_blob_path: str,
        idempotency_key: str,
        dimension: Dict[str, int] | None = None,
        test_mode: bool = False,
        max_poll_time: int = 600,
        creative_prompt: Optional[str] = None,
        req: Optional[FusionJobCreate] = None,
    ) -> Dict[str, Any]:
        """
        Backward-compatible blocking helper.
        Prefer submit_exact_audio_video() + poll_video() in Fusion workers.
        """
        start_time = datetime.now()
        submit = await self.submit_video_from_azure_assets(
            face_image_path=face_image_path,
            audio_blob_path=audio_blob_path,
            idempotency_key=idempotency_key,
            dimension=dimension,
            test_mode=test_mode,
            creative_prompt=creative_prompt,
            req=req,
        )
        video_id = str(submit.get("provider_job_id") or "").strip()
        if not video_id:
            raise HeyGenApiError("submit_video_from_azure_assets missing provider_job_id")

        video_url = await self._poll_until_complete(video_id, max_poll_time)
        duration = (datetime.now() - start_time).total_seconds()
        if video_url:
            return {
                "video_id": video_id,
                "video_url": video_url,
                "status": "succeeded",
                "duration": duration,
                "talking_photo_id": submit.get("talking_photo_id"),
                "submit_meta": submit.get("submit_meta"),
                "raw_response": submit.get("raw_response"),
            }
        return {
            "video_id": video_id,
            "video_url": None,
            "status": "timeout",
            "duration": duration,
            "talking_photo_id": submit.get("talking_photo_id"),
            "submit_meta": submit.get("submit_meta"),
            "raw_response": submit.get("raw_response"),
        }

    async def poll_video(self, video_id: str) -> Dict[str, Any]:
        poll_res = await self.client.poll(video_id)
        return {
            "video_id": video_id,
            "status": poll_res.status,
            "video_url": poll_res.video_url,
            "error_message": poll_res.error_message,
            "raw_response": poll_res.raw_response,
        }

    async def get_video_status(self, video_id: str) -> Dict[str, Any]:
        return await self.poll_video(video_id)

    async def _get_audio_sas_url(self, audio_blob_path: str, expiry_hours: int = 2) -> str:
        s = str(audio_blob_path or "").strip()
        if not s:
            raise HeyGenApiError("audio_blob_path is required")

        if s.startswith(("http://", "https://")):
            return s

        if s.startswith("az://"):
            s = s[5:]

        if self.azure_storage:
            return await self.azure_storage.generate_sas_url(
                s,
                expiry_hours=expiry_hours,
            )

        raise HeyGenApiError(
            "Azure Storage not configured and audio_blob_path is not a URL. "
            "Set AZURE_STORAGE_CONNECTION_STRING or provide a public URL."
        )

    async def _poll_until_complete(
        self,
        video_id: str,
        max_wait_seconds: int = 600,
        poll_interval: int = 10,
    ) -> Optional[str]:
        iterations = max_wait_seconds // poll_interval

        for i in range(iterations):
            await asyncio.sleep(poll_interval)
            try:
                poll_result = await self.client.poll(video_id)
                logger.info("[%s/%s] Video %s: %s", i + 1, iterations, video_id, poll_result.status)
                if poll_result.status == "succeeded":
                    return poll_result.video_url
                if poll_result.status == "failed":
                    raise HeyGenApiError(
                        f"Video generation failed: {poll_result.error_message or 'Unknown error'}"
                    )
            except HeyGenApiError as e:
                if "failed" in str(e).lower():
                    raise
                logger.warning("Poll attempt %s error: %s", i + 1, e)

        logger.warning("Video %s still processing after %ss", video_id, max_wait_seconds)
        return None


class AzureStorageService:
    async def generate_sas_url(
        self,
        blob_path: str,
        expiry_hours: int = 2,
    ) -> str:
        from azure.storage.blob import BlobServiceClient, BlobSasPermissions, generate_blob_sas

        clean = str(blob_path or "").strip()
        if clean.startswith("az://"):
            clean = clean[5:]

        parts = clean.split("/", 1)
        if len(parts) == 2:
            container_name, blob_name = parts
        else:
            container_name = settings.AZURE_AUDIO_CONTAINER
            blob_name = clean

        blob_service_client = BlobServiceClient.from_connection_string(
            settings.AZURE_STORAGE_CONNECTION_STRING
        )
        blob_client = blob_service_client.get_blob_client(
            container=container_name,
            blob=blob_name,
        )
        sas_token = generate_blob_sas(
            account_name=blob_client.account_name,
            container_name=container_name,
            blob_name=blob_name,
            account_key=blob_service_client.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(hours=expiry_hours),
        )
        return f"{blob_client.url}?{sas_token}"
