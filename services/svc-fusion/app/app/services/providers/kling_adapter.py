from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from app.services.providers.base import (
    ProviderClient,
    ProviderPollResult,
    ProviderPrepareInput,
    ProviderPrepareResult,
    ProviderSubmitResult,
)
from app.services.providers.fal_queue import FalQueueClient, FalQueueError


class KlingAdapterError(RuntimeError):
    pass


class KlingAdapter(ProviderClient):
    provider_name = "kling"
    provider_version = "fal.v2"

    _DELIM = "::"

    def __init__(self) -> None:
        self.client = FalQueueClient()

        # Generic motion models
        self.default_i2v_model = os.getenv(
            "FAL_KLING_I2V_MODEL",
            "fal-ai/kling-video/v3/standard/image-to-video",
        ).strip()
        self.default_t2v_model = os.getenv(
            "FAL_KLING_T2V_MODEL",
            "fal-ai/kling-video/v3/standard/text-to-video",
        ).strip()

        # Audio-driven avatar models: this is the correct family for talking video.
        self.avatar_standard_model = os.getenv(
            "FAL_KLING_AVATAR_STANDARD_MODEL",
            "fal-ai/kling-video/ai-avatar/v2/standard",
        ).strip()
        self.avatar_pro_model = os.getenv(
            "FAL_KLING_AVATAR_PRO_MODEL",
            "fal-ai/kling-video/ai-avatar/v2/pro",
        ).strip()

        # Optional future two-step path.
        self.lipsync_model = os.getenv(
            "FAL_KLING_LIPSYNC_MODEL",
            "fal-ai/kling-video/lipsync/audio-to-video",
        ).strip()

    def _safe_str(self, value: Any) -> str:
        return str(value or "").strip()

    def _provider_options(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return payload.get("provider_options") if isinstance(payload.get("provider_options"), dict) else {}

    def _video_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return payload.get("video") if isinstance(payload.get("video"), dict) else {}

    def _tags(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return payload.get("tags") if isinstance(payload.get("tags"), dict) else {}

    def _voice_audio(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return payload.get("voice_audio") if isinstance(payload.get("voice_audio"), dict) else {}

    def _quality_tier(self, payload: Dict[str, Any]) -> str:
        provider_options = self._provider_options(payload)
        tags = self._tags(payload)
        for value in (
            provider_options.get("quality_tier"),
            payload.get("quality_tier"),
            tags.get("quality_tier"),
            tags.get("requested_quality_tier"),
            provider_options.get("output_profile"),
            payload.get("output_profile"),
            tags.get("output_profile"),
        ):
            s = self._safe_str(value).lower()
            if not s:
                continue
            if s in {"economy", "eco", "fast", "budget", "veed", "veed_fabric"}:
                return "economy"
            return "premium"
        return "premium"

    def _longform_profile(self, payload: Dict[str, Any]) -> str:
        provider_options = self._provider_options(payload)
        tags = self._tags(payload)
        for value in (
            provider_options.get("longform_profile"),
            payload.get("longform_profile"),
            tags.get("longform_profile"),
            tags.get("requested_longform_profile"),
        ):
            s = self._safe_str(value).lower()
            if s in {"talking_video", "talking", "tv"}:
                return "talking_video"
            if s in {"cinematic_video_direction", "cinematic", "directed"}:
                return "cinematic_video_direction"
        return "talking_video"

    def _is_talking_video(self, payload: Dict[str, Any]) -> bool:
        return self._longform_profile(payload) == "talking_video"

    def _duration_value(self, duration_sec: Optional[int]) -> str:
        if not duration_sec:
            return "5"
        dur = max(3, min(15, int(duration_sec)))
        return str(dur)

    def _aspect_ratio(self, payload: Dict[str, Any]) -> str:
        video = self._video_payload(payload)
        provider_options = self._provider_options(payload)
        ar = self._safe_str(provider_options.get("aspect_ratio") or video.get("aspect_ratio") or payload.get("aspect_ratio") or "9:16")
        return ar if ar in {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "9:21"} else "9:16"

    def _prompt(self, payload: Dict[str, Any]) -> str:
        provider_options = self._provider_options(payload)
        tags = self._tags(payload)
        for value in (
            provider_options.get("prompt"),
            provider_options.get("background_prompt"),
            provider_options.get("motion_prompt"),
            payload.get("user_prompt"),
            payload.get("performance_prompt"),
            payload.get("prompt"),
            tags.get("prompt_preview"),
            tags.get("background_prompt"),
        ):
            s = self._safe_str(value)
            if s:
                return s
        return "Natural speaking performance with realistic lip sync and believable motion."

    def _reference_elements(self, urls: List[str]) -> List[Dict[str, Any]]:
        if not urls:
            return []
        frontal = urls[0]
        refs = urls[1:4]
        return [{"frontal_image_url": frontal, "reference_image_urls": refs}] if refs else []

    def _audio_url(self, payload: Dict[str, Any]) -> str:
        provider_options = self._provider_options(payload)
        voice_audio = self._voice_audio(payload)
        tags = self._tags(payload)
        for value in (
            provider_options.get("audio_url"),
            provider_options.get("voice_audio_url"),
            voice_audio.get("audio_url"),
            payload.get("audio_url"),
            tags.get("voice_audio_url"),
            tags.get("audio_url"),
        ):
            s = self._safe_str(value)
            if s.startswith(("http://", "https://")):
                return s
        return ""

    def _has_audio_driven_avatar_inputs(self, payload: Dict[str, Any], image_url: str) -> bool:
        return bool(image_url and self._audio_url(payload))

    def _choose_avatar_model(self, payload: Dict[str, Any]) -> str:
        provider_options = self._provider_options(payload)
        explicit = self._safe_str(provider_options.get("avatar_model") or provider_options.get("model_name"))
        if explicit and "ai-avatar" in explicit:
            return explicit

        quality = self._quality_tier(payload)
        if quality == "premium":
            use_pro = self._safe_str(os.getenv("KLING_TALKING_VIDEO_PREMIUM_USE_PRO", "0")).lower() in {"1", "true", "yes", "on"}
            return self.avatar_pro_model if use_pro else self.avatar_standard_model
        return self.avatar_standard_model

    def _validate_avatar_request(self, request_json: Dict[str, Any]) -> None:
        image_url = self._safe_str(request_json.get("image_url"))
        audio_url = self._safe_str(request_json.get("audio_url"))
        if not image_url.startswith(("http://", "https://")):
            raise KlingAdapterError("KLING_AVATAR_IMAGE_URL_REQUIRED")
        if not audio_url.startswith(("http://", "https://")):
            raise KlingAdapterError("KLING_AVATAR_AUDIO_URL_REQUIRED")

    def _build_avatar_request(self, payload: Dict[str, Any], image_url: str) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        provider_options = self._provider_options(payload)
        model_id = self._choose_avatar_model(payload)
        audio_url = self._audio_url(payload)
        prompt = self._prompt(payload)
        aspect_ratio = self._aspect_ratio(payload)

        request_json: Dict[str, Any] = {
            "image_url": image_url,
            "audio_url": audio_url,
            "aspect_ratio": aspect_ratio,
        }
        if prompt:
            request_json["prompt"] = prompt

        self._validate_avatar_request(request_json)

        submit_meta = {
            "provider_name": self.provider_name,
            "provider_model_name": model_id,
            "audio_driven_avatar": True,
            "start_image_url_present": True,
            "audio_url_present": True,
            "aspect_ratio": aspect_ratio,
            "quality_tier": self._quality_tier(payload),
            "longform_profile": self._longform_profile(payload),
        }
        return model_id, request_json, submit_meta

    def _encode_provider_job_id(self, model_id: str, request_id: str) -> str:
        model = self._safe_str(model_id)
        req = self._safe_str(request_id)
        if not model or not req:
            raise KlingAdapterError("cannot encode provider job id without model_id and request_id")
        return f"{model}{self._DELIM}{req}"

    def _decode_provider_job_id(self, provider_job_id: str) -> Tuple[str, str]:
        raw = self._safe_str(provider_job_id)
        if not raw:
            raise KlingAdapterError("missing provider_job_id")

        if self._DELIM in raw:
            model_id, request_id = raw.split(self._DELIM, 1)
            model_id = self._safe_str(model_id)
            request_id = self._safe_str(request_id)
            if model_id and request_id:
                return model_id, request_id

        return self.default_i2v_model, raw

    async def prepare(self, data: ProviderPrepareInput) -> ProviderPrepareResult:
        payload = dict(data.request_payload or {})
        provider_options = self._provider_options(payload)
        reference_urls = list(data.reference_image_urls or payload.get("reference_image_urls") or [])

        # Prefer the freshly resolved provider input URL. The orchestrator
        # refreshes Azure Blob SAS URLs immediately before provider preparation.
        # Persisted provider_options/reference URLs may already be expired.
        start_image_url = self._safe_str(
            data.resolved_face_url
            or (reference_urls[0] if reference_urls else "")
            or provider_options.get("start_image_url")
            or provider_options.get("image_url")
        )
        end_image_url = self._safe_str(provider_options.get("end_image_url")) or None
        duration_sec = (
            provider_options.get("duration_sec")
            or self._video_payload(payload).get("duration_sec")
            or payload.get("duration_sec")
            or 5
        )
        prompt = self._prompt(payload)
        motion_reference_video_url = self._safe_str(
            provider_options.get("motion_reference_video_url")
            or provider_options.get("video_url")
        )

        # Launch path for talking video: use Kling Avatar when image + audio are present.
        if self._is_talking_video(payload) and self._has_audio_driven_avatar_inputs(payload, start_image_url):
            model_id, request_json, submit_meta = self._build_avatar_request(payload, start_image_url)
            return ProviderPrepareResult(
                provider_name=self.provider_name,
                provider_version=self.provider_version,
                request_json={"model_id": model_id, "input": request_json},
                submit_meta=submit_meta,
            )

        if provider_options.get("use_video_as_prompt") and motion_reference_video_url and start_image_url:
            model_id = "fal-ai/video-as-prompt"
            request_json = {
                "prompt": prompt,
                "video_url": motion_reference_video_url,
                "image_url": start_image_url,
                "video_description": self._safe_str(provider_options.get("video_description") or "reference motion video"),
                "aspect_ratio": "9:16" if self._aspect_ratio(payload).startswith("9:") else "16:9",
                "resolution": self._safe_str(provider_options.get("vap_resolution") or "720p"),
            }
            return ProviderPrepareResult(
                provider_name=self.provider_name,
                provider_version=self.provider_version,
                request_json={"model_id": model_id, "input": request_json},
                submit_meta={
                    "provider_name": self.provider_name,
                    "provider_model_name": model_id,
                    "video_as_prompt": True,
                    "motion_reference_video_url_present": True,
                },
            )

        # Fallback generic Kling motion path.
        model_id = self._safe_str(provider_options.get("model_name") or self.default_i2v_model) or self.default_i2v_model

        request_json: Dict[str, Any] = {
            "prompt": prompt,
            "duration": self._duration_value(int(duration_sec)),
            "aspect_ratio": self._aspect_ratio(payload),
            "generate_audio": False,
            "negative_prompt": self._safe_str(provider_options.get("negative_prompt") or "blur, distort, low quality"),
        }

        if start_image_url:
            request_json["start_image_url"] = start_image_url
        if end_image_url:
            request_json["end_image_url"] = end_image_url

        multi_prompt = provider_options.get("multi_prompt")
        if isinstance(multi_prompt, list) and multi_prompt:
            request_json["multi_prompt"] = multi_prompt
            request_json["shot_type"] = self._safe_str(provider_options.get("shot_type") or "customize")

        elements = provider_options.get("elements")
        if isinstance(elements, list) and elements:
            request_json["elements"] = elements
        else:
            derived_elements = self._reference_elements(reference_urls)
            if derived_elements:
                request_json["elements"] = derived_elements

        submit_meta = {
            "provider_name": self.provider_name,
            "provider_model_name": model_id,
            "audio_driven_avatar": False,
            "start_image_url_present": bool(start_image_url),
            "end_image_url_present": bool(end_image_url),
            "reference_image_count": len(reference_urls),
            "duration": request_json["duration"],
            "aspect_ratio": request_json["aspect_ratio"],
        }
        return ProviderPrepareResult(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            request_json={"model_id": model_id, "input": request_json},
            submit_meta=submit_meta,
        )

    async def submit(self, payload: Dict[str, Any], idempotency_key: str) -> ProviderSubmitResult:
        model_id = self._safe_str(payload.get("model_id") or self.default_i2v_model) or self.default_i2v_model
        input_payload = dict(payload.get("input") or {})
        try:
            resp = await self.client.submit(model_id, input_payload, idempotency_key=idempotency_key)
        except FalQueueError as e:
            raise KlingAdapterError(str(e)) from e

        request_id = self._safe_str(resp.get("request_id"))
        if not request_id:
            raise KlingAdapterError("kling submit missing request_id")

        encoded_provider_job_id = self._encode_provider_job_id(model_id, request_id)
        raw = dict(resp or {})
        raw["provider_model_name"] = model_id
        raw["request_id"] = request_id
        raw["encoded_provider_job_id"] = encoded_provider_job_id
        return ProviderSubmitResult(provider_job_id=encoded_provider_job_id, raw_response=raw)

    async def poll(self, provider_job_id: str) -> ProviderPollResult:
        model_id, request_id = self._decode_provider_job_id(provider_job_id)
        try:
            st = await self.client.status(model_id, request_id, logs=True)
            normalized, error_message = self.client.normalize_status(st)
            if normalized != "succeeded":
                return ProviderPollResult(
                    status=normalized,
                    raw_response={**st, "provider_model_name": model_id, "request_id": request_id},
                    error_message=error_message,
                )
            result = await self.client.result(model_id, request_id)
        except FalQueueError as e:
            raise KlingAdapterError(str(e)) from e

        video_url = self.client.extract_video_url(result)
        if not video_url:
            return ProviderPollResult(
                status="failed",
                raw_response={**result, "provider_model_name": model_id, "request_id": request_id},
                error_message="kling result missing video url",
            )
        return ProviderPollResult(
            status="succeeded",
            video_url=video_url,
            raw_response={**result, "provider_model_name": model_id, "request_id": request_id},
        )
