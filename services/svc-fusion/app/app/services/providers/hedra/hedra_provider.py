from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional

from app.services.providers.base import (
    ProviderClient,
    ProviderEstimate,
    ProviderPollResult,
    ProviderPrepareInput,
    ProviderPrepareResult,
    ProviderSubmitResult,
)

from .hedra_client import HedraApiError, HedraClient


class HedraProviderError(RuntimeError):
    pass


class HedraProvider(ProviderClient):
    provider_name = "hedra"
    provider_version = "public-api.v1"

    def __init__(self, client: Optional[HedraClient] = None) -> None:
        self.client = client or HedraClient()

    async def estimate(self, request_payload: Dict[str, Any]) -> ProviderEstimate:
        video = request_payload.get("video") or {}
        duration_sec = video.get("duration_sec")
        duration_ms = video.get("duration_ms")
        estimated_units = "1"
        try:
            if duration_sec is not None:
                estimated_units = str(max(1, int(math.ceil(float(duration_sec) / 60.0))))
            elif duration_ms is not None:
                estimated_units = str(max(1, int(math.ceil(float(duration_ms) / 60000.0))))
        except Exception:
            estimated_units = "1"

        model = await self._choose_model(request_payload)
        return ProviderEstimate(
            estimated_units=estimated_units,
            unit_type="minute",
            provider_meta={
                "provider": self.provider_name,
                "provider_model_name": model.get("name"),
                "provider_model_id": model.get("id"),
            },
        )

    async def prepare(self, spec: ProviderPrepareInput) -> ProviderPrepareResult:
        payload = spec.request_payload or {}
        video = payload.get("video") or {}
        provider_options = self._coerce_dict(payload.get("provider_options"))
        tags = self._coerce_dict(payload.get("tags"))

        model = await self._choose_model(payload)
        model_name = str(model.get("name") or "")
        model_id = str(model.get("id") or "")
        aspect_ratio = self._pick_aspect_ratio(model, video, provider_options)
        resolution = self._pick_resolution(model, video, provider_options)
        duration_ms = self._pick_duration_ms(model, video, provider_options)

        prompt, prompt_source = self._select_prompt(
            payload=payload,
            provider_options=provider_options,
            tags=tags,
        )
        if not prompt:
            prompt = self._default_prompt_for_model(model_name)
            prompt_source = "default"

        start_image_asset_id: Optional[str] = None
        if bool(model.get("requires_start_frame")) or spec.resolved_face_url:
            if not spec.resolved_face_url:
                raise HedraProviderError("Hedra model requires face image/start frame")
            start_image_asset_id = await self._upload_remote_asset(
                url=spec.resolved_face_url,
                asset_type="image",
            )

        reference_image_ids: List[str] = []
        for ref_url in list(spec.reference_image_urls or []):
            asset_id = await self._upload_remote_asset(url=ref_url, asset_type="image")
            if asset_id:
                reference_image_ids.append(asset_id)

        audio_asset_id: Optional[str] = None
        voice_mode = str(payload.get("voice_mode") or "").strip().lower()

        if voice_mode == "audio":
            if not spec.resolved_audio_url:
                raise HedraProviderError("voice_mode=audio requires resolved audio input")
            audio_asset_id = await self._upload_remote_asset(
                url=spec.resolved_audio_url,
                asset_type="audio",
            )
        else:
            voice_tts = self._coerce_dict(payload.get("voice_tts"))
            script = str(voice_tts.get("script") or "").strip()
            voice_id = str(voice_tts.get("voice_id") or "").strip()
            if script and voice_id:
                audio_asset_id = await self.client.generate_tts_audio_asset_id(
                    text=script,
                    voice_id=voice_id,
                    language=str(voice_tts.get("language") or provider_options.get("language") or "English"),
                    stability=self._as_optional_float(provider_options.get("tts_stability")),
                    speed=self._as_optional_float(provider_options.get("tts_speed")),
                    model_id=self._as_optional_str(provider_options.get("tts_model_id")),
                    poll_seconds=int(provider_options.get("tts_poll_seconds") or os.getenv("HEDRA_TTS_POLL_SECONDS", "3")),
                    timeout_seconds=int(provider_options.get("tts_timeout_seconds") or os.getenv("HEDRA_TTS_TIMEOUT_SECONDS", "300")),
                )

        if bool(model.get("requires_audio_input")) and not audio_asset_id:
            raise HedraProviderError("Hedra model requires audio input")

        if "omnihuman" in model_name.lower() and not audio_asset_id:
            raise HedraProviderError("Omnihuman requires audio input in practice; provide audio or TTS")

        generation_payload: Dict[str, Any] = {
            "type": "video",
            "ai_model_id": model_id,
            "generated_video_inputs": {
                "text_prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
            },
            "batch_size": int(provider_options.get("batch_size") or 1),
        }
        if duration_ms is not None:
            generation_payload["generated_video_inputs"]["duration_ms"] = duration_ms

        if start_image_asset_id:
            generation_payload["start_keyframe_id"] = start_image_asset_id
        if reference_image_ids:
            generation_payload["reference_image_ids"] = reference_image_ids
        if audio_asset_id:
            generation_payload["audio_id"] = audio_asset_id

        return ProviderPrepareResult(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            request_json=generation_payload,
            submit_meta={
                "provider_model_name": model_name,
                "provider_model_id": model_id,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "duration_ms": duration_ms,
                "requested_audio_duration_ms": self._extract_requested_duration_ms(payload, provider_options, video),
                "reference_image_count": len(reference_image_ids),
                "has_start_frame": bool(start_image_asset_id),
                "has_audio": bool(audio_asset_id),
                "prompt_source": prompt_source,
                "prompt_preview": prompt[:160],
                "prompt_fields_present": self._available_prompt_fields(payload, provider_options, tags),
            },
        )

    async def submit(self, payload: Dict[str, Any], idempotency_key: str) -> ProviderSubmitResult:
        try:
            result = await self.client.submit_generation(payload)
        except HedraApiError as exc:
            raise HedraProviderError(str(exc)) from exc
        return ProviderSubmitResult(
            provider_job_id=result.generation_id,
            raw_response=result.raw_response,
        )

    async def poll(self, provider_job_id: str) -> ProviderPollResult:
        try:
            data = await self.client.get_generation_status(provider_job_id)
        except HedraApiError as exc:
            raise HedraProviderError(str(exc)) from exc

        raw_status = str(data.get("status") or "").strip().lower()
        if raw_status in {"processing", "pending", "queued", "running", "in_progress", "submitted", ""}:
            status = "processing"
        elif raw_status in {"complete", "completed", "success", "succeeded", "ready", "finalizing"}:
            status = "succeeded" if data.get("url") or data.get("download_url") else ("processing" if raw_status == "finalizing" else "succeeded")
        elif raw_status in {"failed", "error"}:
            status = "failed"
        elif raw_status in {"canceled", "cancelled"}:
            status = "canceled"
        else:
            status = raw_status or "unknown"

        video_url = str(data.get("download_url") or data.get("url") or "").strip() or None
        streaming_url = str(data.get("streaming_url") or "").strip() or None
        error_message = (
            self._as_optional_str(data.get("error_message"))
            or self._as_optional_str(data.get("error"))
        )

        return ProviderPollResult(
            status=status,
            video_url=video_url,
            share_url=video_url or streaming_url,
            raw_response=data,
            error_message=error_message,
            error_code="HEDRA_STATUS_FAILED" if status == "failed" else None,
        )

    async def get_share_url(self, provider_job_id: str) -> Optional[str]:
        try:
            poll = await self.poll(provider_job_id)
            return poll.share_url or poll.video_url
        except Exception:
            return None

    async def _choose_model(self, request_payload: Dict[str, Any]) -> Dict[str, Any]:
        models = await self.client.list_models()
        provider_options = self._coerce_dict(request_payload.get("provider_options"))
        video = request_payload.get("video") or {}

        requested_name_raw = self._as_optional_str(provider_options.get("model_name"))
        requested_name = None
        if requested_name_raw and requested_name_raw.strip().lower() not in {"auto", "default"}:
            requested_name = requested_name_raw.strip()

        duration_ms = self._extract_requested_duration_ms(request_payload, provider_options, video)

        aspect_ratio = str(
            provider_options.get("aspect_ratio")
            or video.get("aspect_ratio")
            or os.getenv("DF_FUSION_DEFAULT_ASPECT_RATIO", "9:16")
        ).strip()

        shot_type = str(provider_options.get("shot_type") or request_payload.get("shot_type") or "").strip().lower()
        voice_mode = str(request_payload.get("voice_mode") or "").strip().lower()

        if requested_name:
            found = self._find_model(models, requested_name)
            if not found:
                raise HedraProviderError(f"hedra_model_not_found: {requested_name}")

            chosen_name = str(found.get("name") or "").strip().lower()
            if "omnia" in chosen_name and (voice_mode == "audio" or (duration_ms is not None and duration_ms > 8000)):
                avatar = self._find_model(models, "Hedra Avatar")
                if avatar:
                    return avatar
            return found

        if (
            aspect_ratio == "16:9"
            and shot_type in {"wide", "wide_experimental", "landscape_experimental"}
            and str(os.getenv("DF_HEDRA_ENABLE_OMNIHUMAN", "1")).strip().lower() in {"1", "true", "yes", "y"}
        ):
            found = self._find_model(models, "Omnihuman 1.5 I2V")
            if found:
                return found

        if duration_ms is not None and duration_ms > 8000:
            found = self._find_model(models, "Hedra Avatar")
            if found:
                return found

        if voice_mode == "audio":
            found = self._find_model(models, "Hedra Avatar")
            if found:
                return found

        found = self._find_model(models, "Hedra Omnia")
        if found:
            return found

        found = self._find_model(models, "Hedra Avatar")
        if found:
            return found

        raise HedraProviderError("No supported Hedra model found in account")

    async def _upload_remote_asset(self, *, url: str, asset_type: str) -> str:
        try:
            content, filename, content_type = await self.client.fetch_bytes(url)
            created = await self.client.create_asset(name=filename, asset_type=asset_type)
            asset_id = str(created.get("id") or "").strip()
            if not asset_id:
                raise HedraProviderError(f"asset_create_missing_id for {asset_type}")
            await self.client.upload_asset_bytes(
                asset_id=asset_id,
                filename=filename,
                data=content,
                content_type=content_type,
            )
            return asset_id
        except HedraApiError as exc:
            raise HedraProviderError(str(exc)) from exc


    def _select_prompt(
        self,
        *,
        payload: Dict[str, Any],
        provider_options: Dict[str, Any],
        tags: Dict[str, Any],
    ) -> tuple[str, str]:
        prompt_priority = [
            "user_prompt",
            "performance_prompt",
            "motion_prompt",
            "movement_prompt",
            "emotion_prompt",
            "expression_prompt",
            "gesture_prompt",
            "body_motion_prompt",
            "creative_direction",
            "prompt",
        ]
        prompt_sources = [
            ("payload", payload),
            ("provider_options", provider_options),
            ("tags", tags),
        ]

        for field_name in prompt_priority:
            for source_name, source_dict in prompt_sources:
                value = self._as_optional_str(source_dict.get(field_name))
                if value:
                    return value, f"{source_name}.{field_name}"

        return "", "default"

    def _available_prompt_fields(
        self,
        payload: Dict[str, Any],
        provider_options: Dict[str, Any],
        tags: Dict[str, Any],
    ) -> List[str]:
        found: List[str] = []
        for field_name in [
            "user_prompt",
            "performance_prompt",
            "motion_prompt",
            "movement_prompt",
            "emotion_prompt",
            "expression_prompt",
            "gesture_prompt",
            "body_motion_prompt",
            "creative_direction",
            "prompt",
        ]:
            if any(
                self._as_optional_str(source.get(field_name))
                for source in (payload, provider_options, tags)
            ):
                found.append(field_name)
        return found

    def _pick_aspect_ratio(self, model: Dict[str, Any], video: Dict[str, Any], provider_options: Dict[str, Any]) -> str:
        requested = str(
            provider_options.get("aspect_ratio")
            or video.get("aspect_ratio")
            or os.getenv("DF_FUSION_DEFAULT_ASPECT_RATIO", "9:16")
        ).strip()
        supported = [str(v).strip() for v in list(model.get("aspect_ratios") or []) if v]
        if supported and requested not in supported:
            return supported[0]
        return requested or (supported[0] if supported else "9:16")

    def _pick_resolution(self, model: Dict[str, Any], video: Dict[str, Any], provider_options: Dict[str, Any]) -> str:
        requested = str(
            provider_options.get("resolution")
            or video.get("resolution")
            or os.getenv("DF_FUSION_DEFAULT_RESOLUTION", "720p")
        ).strip()
        supported = [str(v).strip() for v in list(model.get("resolutions") or []) if v]
        if supported and requested not in supported:
            return supported[0]
        return requested or (supported[0] if supported else "720p")

    def _pick_duration_ms(self, model: Dict[str, Any], video: Dict[str, Any], provider_options: Dict[str, Any]) -> Optional[int]:
        requested_int = self._extract_requested_duration_ms(
            {
                "video": video,
                "provider_options": provider_options,
            },
            provider_options,
            video,
        )
        if requested_int is None:
            return None

        max_duration = model.get("max_duration_ms")
        try:
            max_duration_int = int(max_duration) if max_duration is not None else None
        except Exception:
            max_duration_int = None

        if max_duration_int is not None:
            return min(requested_int, max_duration_int)
        return requested_int

    def _extract_requested_duration_ms(
        self,
        request_payload: Dict[str, Any],
        provider_options: Dict[str, Any],
        video: Dict[str, Any],
    ) -> Optional[int]:
        voice_audio = self._coerce_dict(request_payload.get("voice_audio"))
        tags = self._coerce_dict(request_payload.get("tags"))

        requested_ms = (
            provider_options.get("requested_audio_duration_ms")
            or provider_options.get("duration_ms")
            or video.get("duration_ms")
            or request_payload.get("audio_duration_ms")
            or request_payload.get("duration_ms")
            or voice_audio.get("audio_duration_ms")
            or voice_audio.get("duration_ms")
            or tags.get("requested_audio_duration_ms")
        )

        if requested_ms is None:
            requested_sec = (
                provider_options.get("requested_audio_duration_sec")
                or provider_options.get("duration_sec")
                or video.get("duration_sec")
                or request_payload.get("audio_duration_sec")
                or request_payload.get("duration_sec")
                or voice_audio.get("audio_duration_sec")
                or voice_audio.get("duration_sec")
                or tags.get("requested_audio_duration_sec")
            )
            if requested_sec is not None:
                try:
                    requested_ms = int(float(requested_sec) * 1000.0)
                except Exception:
                    requested_ms = None

        if requested_ms is None:
            return None

        try:
            requested_int = int(float(requested_ms))
        except Exception:
            return None

        return requested_int if requested_int > 0 else None

    def _default_prompt_for_model(self, model_name: str) -> str:
        model_name_l = model_name.lower()
        if "omnia" in model_name_l:
            return "Warm expressive presenter shot, subtle upper-body motion, premium cinematic realism"
        if "avatar" in model_name_l:
            return "A person speaking warmly to the camera"
        if "omnihuman" in model_name_l:
            return "Cinematic presenter shot, graceful body movement, premium realism"
        return "A person speaking to the camera"

    def _find_model(self, models: List[Dict[str, Any]], desired_name: str) -> Optional[Dict[str, Any]]:
        desired = desired_name.strip().lower()
        exact = None
        contains = None
        for model in models:
            name = str(model.get("name") or "").strip().lower()
            if name == desired:
                exact = model
                break
            if desired in name and contains is None:
                contains = model
        return exact or contains

    @staticmethod
    def _coerce_dict(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        return {}

    @staticmethod
    def _as_optional_str(value: Any) -> Optional[str]:
        if value is None:
            return None
        s = str(value).strip()
        return s or None

    @staticmethod
    def _as_optional_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except Exception:
            return None
