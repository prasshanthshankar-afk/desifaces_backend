from __future__ import annotations

import os
from typing import Any, Dict, Optional

try:
    import fal_client  # type: ignore
except Exception as fal_import_error:  # pragma: no cover
    fal_client = None  # type: ignore
    _FAL_IMPORT_ERROR = str(fal_import_error)
else:
    _FAL_IMPORT_ERROR = None

from app.services.providers.base import (
    ProviderClient,
    ProviderPrepareInput,
    ProviderPrepareResult,
    ProviderPollResult,
    ProviderSubmitResult,
)


class VeedFabricAdapterError(RuntimeError):
    pass


class VeedFabricAdapter(ProviderClient):
    """
    VEED Fabric adapter via fal.ai.

    Launch policy:
      - image_url + audio_url required
      - 480p default for economy mode
      - economy path is locked to 480p for launch pricing
      - conservative max-duration guard defaults to 30s
    """

    provider_name = "veed_fabric"
    provider_version = "v1"

    def __init__(self) -> None:
        self.application = str(os.getenv("DF_VEED_FABRIC_MODEL_ID") or "veed/fabric-1.0").strip() or "veed/fabric-1.0"
        self.max_duration_sec = max(5, int(float(os.getenv("DF_VEED_FABRIC_MAX_DURATION_SEC") or "30")))
        self.default_resolution = self._normalize_resolution(os.getenv("DF_VEED_FABRIC_DEFAULT_RESOLUTION") or "480p")

    @staticmethod
    def _safe_str(value: Any) -> Optional[str]:
        if value is None:
            return None
        s = str(value).strip()
        return s or None

    @staticmethod
    def _coerce_dict(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        return {}

    @staticmethod
    def _normalize_resolution(value: Any) -> str:
        s = str(value or "").strip().lower()
        if s in {"480p", "480", "sd"}:
            return "480p"
        if s in {"720p", "720", "hd", "1080p", "1080", "fullhd", "fhd"}:
            return "720p"
        return "480p"

    def _extract_duration_sec(self, payload: Dict[str, Any]) -> int:
        video = self._coerce_dict(payload.get("video"))
        provider_options = self._coerce_dict(payload.get("provider_options"))
        for value in (
            provider_options.get("duration_sec"),
            provider_options.get("planned_duration_sec"),
            video.get("duration_sec"),
            payload.get("duration_sec"),
        ):
            try:
                if value is None:
                    continue
                sec = int(float(value))
                if sec > 0:
                    return sec
            except Exception:
                continue
        return 0

    def _extract_resolution(self, payload: Dict[str, Any]) -> str:
        video = self._coerce_dict(payload.get("video"))
        provider_options = self._coerce_dict(payload.get("provider_options"))
        quality_tier = self._safe_str(payload.get("quality_tier")) or self._safe_str(provider_options.get("quality_tier"))
        output_profile = self._safe_str(payload.get("output_profile"))
        if str(quality_tier or "").strip().lower() == "economy" or str(output_profile or "").strip().lower() == "economy":
            return "480p"
        for value in (
            provider_options.get("resolution"),
            video.get("resolution"),
            payload.get("output_profile"),
        ):
            if value is not None:
                return self._normalize_resolution(value)
        return self.default_resolution

    async def prepare(self, data: ProviderPrepareInput) -> ProviderPrepareResult:
        if fal_client is None:
            raise VeedFabricAdapterError(f"fal_client_unavailable: {_FAL_IMPORT_ERROR}")

        payload = self._coerce_dict(getattr(data, "request_payload", None))
        face_url = self._safe_str(getattr(data, "resolved_face_url", None))
        audio_url = self._safe_str(getattr(data, "resolved_audio_url", None))
        if not face_url:
            raise VeedFabricAdapterError("VEED_FABRIC_FACE_REQUIRED")
        if not audio_url:
            raise VeedFabricAdapterError("VEED_FABRIC_AUDIO_REQUIRED")

        duration_sec = self._extract_duration_sec(payload)
        if duration_sec > self.max_duration_sec:
            raise VeedFabricAdapterError(
                f"VEED_FABRIC_MAX_DURATION_EXCEEDED requested={duration_sec}s max={self.max_duration_sec}s"
            )

        resolution = self._extract_resolution(payload)
        request_json = {
            "image_url": face_url,
            "audio_url": audio_url,
            "resolution": resolution,
        }
        submit_meta = {
            "provider_name": self.provider_name,
            "provider_model_name": self.application,
            "resolution": resolution,
            "duration_sec": duration_sec or None,
            "input_contract": "image_url+audio_url",
        }
        return ProviderPrepareResult(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            request_json=request_json,
            submit_meta=submit_meta,
        )

    async def submit(self, request_json: Dict[str, Any], idempotency_key: str) -> ProviderSubmitResult:
        if fal_client is None:
            raise VeedFabricAdapterError(f"fal_client_unavailable: {_FAL_IMPORT_ERROR}")
        try:
            handle = await fal_client.submit_async(self.application, arguments=dict(request_json or {}), hint=idempotency_key)
        except Exception as exc:  # pragma: no cover
            raise VeedFabricAdapterError(f"VEED_FABRIC_SUBMIT_FAILED: {exc}") from exc

        provider_job_id = self._safe_str(getattr(handle, "request_id", None))
        if not provider_job_id:
            raise VeedFabricAdapterError("VEED_FABRIC_MISSING_REQUEST_ID")
        raw_response = {
            "request_id": provider_job_id,
            "status_url": getattr(handle, "status_url", None),
            "response_url": getattr(handle, "response_url", None),
        }
        return ProviderSubmitResult(provider_job_id=provider_job_id, raw_response=raw_response)

    @staticmethod
    def _normalize_status(status_obj: Any) -> str:
        cls_name = status_obj.__class__.__name__.lower()
        if "completed" in cls_name:
            return "succeeded"
        if "queued" in cls_name:
            return "queued"
        if "progress" in cls_name or "inprogress" in cls_name:
            return "processing"
        raw_status = str(getattr(status_obj, "status", None) or "").strip().lower()
        if raw_status in {"completed", "success", "succeeded"}:
            return "succeeded"
        if raw_status in {"queued", "in_queue", "pending"}:
            return "queued"
        if raw_status in {"in_progress", "processing", "running"}:
            return "processing"
        if raw_status in {"failed", "error"}:
            return "failed"
        return "processing"

    @staticmethod
    def _extract_video_url(result_obj: Any) -> Optional[str]:
        if isinstance(result_obj, dict):
            video = result_obj.get("video")
            if isinstance(video, dict):
                return str(video.get("url") or "").strip() or None
            return str(result_obj.get("video_url") or "").strip() or None
        return None

    async def poll(self, provider_job_id: str) -> ProviderPollResult:
        if fal_client is None:
            raise VeedFabricAdapterError(f"fal_client_unavailable: {_FAL_IMPORT_ERROR}")
        try:
            status_obj = await fal_client.status_async(self.application, provider_job_id, with_logs=False)
        except Exception as exc:  # pragma: no cover
            raise VeedFabricAdapterError(f"VEED_FABRIC_STATUS_FAILED: {exc}") from exc

        normalized = self._normalize_status(status_obj)
        if normalized != "succeeded":
            error_message = None
            if normalized == "failed":
                error_message = self._safe_str(getattr(status_obj, "error", None)) or self._safe_str(getattr(status_obj, "message", None)) or "VEED_FABRIC_FAILED"
            return ProviderPollResult(status=normalized, video_url=None, share_url=None, error_message=error_message)

        try:
            result_obj = await fal_client.result_async(self.application, provider_job_id)
        except Exception as exc:  # pragma: no cover
            raise VeedFabricAdapterError(f"VEED_FABRIC_RESULT_FAILED: {exc}") from exc

        video_url = self._extract_video_url(result_obj)
        if not video_url:
            raise VeedFabricAdapterError("VEED_FABRIC_RESULT_MISSING_VIDEO_URL")
        return ProviderPollResult(status="succeeded", video_url=video_url, share_url=None, error_message=None)
