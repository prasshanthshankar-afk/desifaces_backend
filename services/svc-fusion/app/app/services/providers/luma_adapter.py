from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from app.services.providers.base import (
    ProviderClient,
    ProviderPollResult,
    ProviderPrepareInput,
    ProviderPrepareResult,
    ProviderSubmitResult,
)
from app.services.providers.fal_queue import FalQueueClient, FalQueueError


class LumaAdapterError(RuntimeError):
    pass


class LumaAdapter(ProviderClient):
    provider_name = "luma"
    provider_version = "fal.v2"

    _DELIM = "::"

    def __init__(self) -> None:
        self.client = FalQueueClient()
        self.default_i2v_model = os.getenv(
            "FAL_LUMA_I2V_MODEL",
            "fal-ai/luma-dream-machine/ray-2/image-to-video",
        ).strip()
        self.default_t2v_model = os.getenv(
            "FAL_LUMA_T2V_MODEL",
            "fal-ai/luma-dream-machine/ray-2",
        ).strip()

    def _coerce_int(self, value: Any, default: int) -> int:
        try:
            if value is None:
                return default
            return int(float(value))
        except Exception:
            return default

    def _provider_options(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return payload.get("provider_options") if isinstance(payload.get("provider_options"), dict) else {}

    def _video_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return payload.get("video") if isinstance(payload.get("video"), dict) else {}

    def _tags(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return payload.get("tags") if isinstance(payload.get("tags"), dict) else {}

    def _boolish(self, value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off"}:
            return False
        return default

    def _string(self, value: Any) -> str:
        return str(value or "").strip()

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
            s = self._string(value).lower()
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
            s = self._string(value).lower()
            if s in {"talking_video", "talking", "tv"}:
                return "talking_video"
            if s in {"cinematic_video_direction", "cinematic", "directed"}:
                return "cinematic_video_direction"
        return "talking_video"

    def _is_premium_talking_background_plate(self, payload: Dict[str, Any]) -> bool:
        provider_options = self._provider_options(payload)
        tags = self._tags(payload)
        profile = self._longform_profile(payload)
        quality = self._quality_tier(payload)
        background_mode = self._string(
            provider_options.get("background_mode")
            or payload.get("background_mode")
            or tags.get("background_mode")
        ).lower()
        background_provider = self._string(
            provider_options.get("background_provider")
            or tags.get("background_provider")
        ).lower()
        presenter_with_motion_bg = provider_options.get("presenter_with_motion_bg")
        presenter_with_motion_bg_enabled = False
        if isinstance(presenter_with_motion_bg, dict):
            presenter_with_motion_bg_enabled = self._boolish(presenter_with_motion_bg.get("enabled"))
        else:
            presenter_with_motion_bg_enabled = self._boolish(presenter_with_motion_bg)

        scene_plate_only = self._boolish(
            provider_options.get("scene_plate_only")
            or provider_options.get("background_plate_only")
            or tags.get("scene_plate_only"),
            False,
        )

        return (
            profile == "talking_video"
            and quality == "premium"
            and (
                background_provider == "luma"
                or background_mode == "movement_based"
                or presenter_with_motion_bg_enabled
                or scene_plate_only
            )
        )

    def _duration_value(self, payload: Dict[str, Any]) -> str:
        provider_options = self._provider_options(payload)
        video = self._video_payload(payload)

        explicit_duration = self._string(provider_options.get("duration") or video.get("duration"))
        if explicit_duration in {"5s", "9s"}:
            return explicit_duration

        dur = self._coerce_int(
            provider_options.get("duration_sec")
            or video.get("duration_sec")
            or payload.get("duration_sec"),
            5,
        )

        if self._is_premium_talking_background_plate(payload):
            prefer_long_plate = self._boolish(
                provider_options.get("prefer_long_plate")
                or provider_options.get("use_9s_background_plate")
                or os.getenv("LUMA_TALKING_PREMIUM_LONG_PLATE", "1"),
                True,
            )
            if prefer_long_plate:
                return "9s"
            return "9s" if dur > 7 else "5s"

        return "9s" if dur > 7 else "5s"

    def _resolution(self, payload: Dict[str, Any]) -> str:
        video = self._video_payload(payload)
        provider_options = self._provider_options(payload)

        explicit_requested = self._string(provider_options.get("resolution") or video.get("resolution"))
        if explicit_requested:
            requested = explicit_requested
        elif self._is_premium_talking_background_plate(payload):
            requested = self._string(os.getenv("LUMA_TALKING_PREMIUM_BG_DEFAULT_RESOLUTION") or "720p")
        else:
            requested = "720p"

        if requested not in {"540p", "720p", "1080p"}:
            requested = "720p" if self._is_premium_talking_background_plate(payload) else "720p"

        duration_value = self._duration_value(payload)

        # Current Ray-2 runtime limitation: 9s is not supported at 1080p.
        if duration_value == "9s" and requested == "1080p":
            return "720p"

        return requested

    def _aspect_ratio(self, payload: Dict[str, Any]) -> str:
        video = self._video_payload(payload)
        provider_options = self._provider_options(payload)
        ar = self._string(provider_options.get("aspect_ratio") or video.get("aspect_ratio") or payload.get("aspect_ratio") or "9:16")
        return ar if ar in {"16:9", "9:16", "4:3", "3:4", "21:9", "9:21"} else "9:16"

    def _camera_motion_phrase(self, value: str) -> str:
        mapping = {
            "static": "locked camera with only environmental motion",
            "locked_off": "locked camera with only environmental motion",
            "steady": "stable framing with very gentle environmental motion",
            "subtle_drift": "subtle camera drift with stable composition",
            "gentle_parallax": "gentle parallax depth movement",
            "gentle_push_in": "gentle camera push-in",
            "slow_push_in": "slow cinematic push-in",
            "gentle_pull_out": "gentle camera pull-out",
        }
        return mapping.get(value, "stable framing with natural environmental motion")

    def _motion_prompt_for_premium_background(self, payload: Dict[str, Any], base_prompt: str) -> str:
        provider_options = self._provider_options(payload)
        tags = self._tags(payload)

        motion_intent = self._string(provider_options.get("motion_intent") or tags.get("motion_intent")).lower() or "ambient_realism"
        camera_motion_style = self._string(provider_options.get("camera_motion_style") or tags.get("camera_motion_style")).lower()
        parallax_strength = self._string(provider_options.get("parallax_strength") or "medium").lower()
        ambient_wind = self._boolish(provider_options.get("ambient_wind"), True)
        dynamic_lighting = self._boolish(provider_options.get("dynamic_lighting"), True)
        motion_level = self._string(
            provider_options.get("motion_level")
            or provider_options.get("motion_strength")
            or os.getenv("LUMA_TALKING_PREMIUM_MOTION_LEVEL", "strong")
        ).lower()

        motion_clause = {
            "subtle": "subtle but clearly perceptible real-world background motion",
            "balanced": "natural and clearly visible background motion",
            "noticeable": "noticeable real-world background motion that is easy to perceive while staying realistic",
            "strong": "pronounced but still believable real-world background motion",
        }.get(motion_level, "pronounced but still believable real-world background motion")

        intent_clause = {
            "ambient_realism": "natural physics with believable environmental movement",
            "windy_outdoor": "outdoor breeze moving leaves, trees, grass, and fabrics",
            "festive_liveliness": "festive environmental movement with lights, fabric, and atmospheric motion",
            "urban_life": "subtle city-life movement in the distance with coherent environmental motion",
        }.get(motion_intent, "natural physics with believable environmental movement")

        clauses = []
        if base_prompt:
            clauses.append(base_prompt.rstrip("."))
        else:
            clauses.append("Premium cinematic background plate")
        clauses.append(motion_clause)
        clauses.append(intent_clause)

        if ambient_wind:
            clauses.append("tree leaves, branches, grass, and light fabrics move visibly in a natural breeze")
        if dynamic_lighting:
            clauses.append("soft sunlight, branch shadows, and highlights shift naturally across the scene")
        if parallax_strength == "high":
            clauses.append("pronounced depth movement with believable parallax")
        elif parallax_strength == "medium":
            clauses.append("gentle depth movement with moderate parallax")
        else:
            clauses.append("light depth movement with minimal parallax")

        if camera_motion_style:
            clauses.append(self._camera_motion_phrase(camera_motion_style))

        clauses.append("no foreground talking subject, no lip sync, no text, no title cards, no abrupt motion, no scene cuts")
        return ". ".join([c.strip().rstrip(".") for c in clauses if c and c.strip()]) + "."

    def _prompt(self, payload: Dict[str, Any]) -> str:
        provider_options = self._provider_options(payload)
        tags = self._tags(payload)

        base_prompt = ""
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
            s = self._string(value)
            if s:
                base_prompt = s
                break

        if self._is_premium_talking_background_plate(payload):
            return self._motion_prompt_for_premium_background(payload, base_prompt)

        if base_prompt:
            return base_prompt

        return "Realistic background motion, natural physics, subtle environmental activity, premium cinematic realism."

    def _encode_provider_job_id(self, model_id: str, request_id: str) -> str:
        model = str(model_id or "").strip()
        req = str(request_id or "").strip()
        if not model or not req:
            raise LumaAdapterError("cannot encode provider job id without model_id and request_id")
        return f"{model}{self._DELIM}{req}"

    def _decode_provider_job_id(self, provider_job_id: str) -> Tuple[str, str]:
        raw = str(provider_job_id or "").strip()
        if not raw:
            raise LumaAdapterError("missing provider_job_id")

        if self._DELIM in raw:
            model_id, request_id = raw.split(self._DELIM, 1)
            model_id = model_id.strip()
            request_id = request_id.strip()
            if model_id and request_id:
                return model_id, request_id

        return self.default_i2v_model, raw

    def _validate_request(self, *, model_id: str, request_json: Dict[str, Any], payload: Dict[str, Any]) -> None:
        duration = self._string(request_json.get("duration"))
        resolution = self._string(request_json.get("resolution"))
        prompt = self._string(request_json.get("prompt"))
        image_url = self._string(request_json.get("image_url"))
        end_image_url = self._string(request_json.get("end_image_url"))
        is_i2v = model_id == self.default_i2v_model

        if not prompt:
            raise LumaAdapterError("LUMA_PROMPT_REQUIRED")

        if duration not in {"5s", "9s"}:
            raise LumaAdapterError(f"LUMA_UNSUPPORTED_DURATION: {duration!r}")

        if resolution not in {"540p", "720p", "1080p"}:
            raise LumaAdapterError(f"LUMA_UNSUPPORTED_RESOLUTION: {resolution!r}")

        if duration == "9s" and resolution == "1080p":
            raise LumaAdapterError("LUMA_9S_1080P_UNSUPPORTED")

        if is_i2v and not image_url:
            raise LumaAdapterError("LUMA_IMAGE_URL_REQUIRED_FOR_I2V")

        if image_url and not image_url.startswith(("http://", "https://")):
            raise LumaAdapterError("LUMA_IMAGE_URL_MUST_BE_HTTP")
        if end_image_url and not end_image_url.startswith(("http://", "https://")):
            raise LumaAdapterError("LUMA_END_IMAGE_URL_MUST_BE_HTTP")

        # Scene plates for premium talking video should always request looping.
        if self._is_premium_talking_background_plate(payload) and request_json.get("loop") is not True:
            request_json["loop"] = True

    async def prepare(self, data: ProviderPrepareInput) -> ProviderPrepareResult:
        payload = dict(data.request_payload or {})
        provider_options = self._provider_options(payload)
        reference_urls = list(data.reference_image_urls or payload.get("reference_image_urls") or [])

        image_url = self._string(
            provider_options.get("image_url")
            or provider_options.get("start_image_url")
            or (reference_urls[0] if reference_urls else "")
            or (data.resolved_face_url or "")
        )
        end_image_url = self._string(provider_options.get("end_image_url")) or None

        prompt = self._prompt(payload)
        duration_value = self._duration_value(payload)
        resolution_value = self._resolution(payload)
        aspect_ratio_value = self._aspect_ratio(payload)
        is_premium_talking_background_plate = self._is_premium_talking_background_plate(payload)

        motion_reference_video_url = self._string(
            provider_options.get("motion_reference_video_url")
            or provider_options.get("video_url")
        )

        if provider_options.get("use_video_as_prompt") and motion_reference_video_url and image_url:
            model_id = "fal-ai/video-as-prompt"
            vap_resolution = self._string(provider_options.get("vap_resolution")) or ("720p" if is_premium_talking_background_plate else "720p")
            if vap_resolution not in {"540p", "720p", "1080p"}:
                vap_resolution = "720p"

            request_json = {
                "prompt": prompt,
                "video_url": motion_reference_video_url,
                "image_url": image_url,
                "video_description": self._string(provider_options.get("video_description")) or "reference motion video",
                "aspect_ratio": aspect_ratio_value,
                "resolution": vap_resolution,
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
                    "resolution": request_json["resolution"],
                    "aspect_ratio": request_json["aspect_ratio"],
                    "premium_talking_background_plate": is_premium_talking_background_plate,
                    "scene_plate_only": self._boolish(provider_options.get("scene_plate_only") or provider_options.get("background_plate_only")),
                },
            )

        model_id = self.default_i2v_model if image_url else self.default_t2v_model
        model_id = self._string(provider_options.get("model_name") or model_id) or model_id

        request_json: Dict[str, Any] = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio_value,
            "resolution": resolution_value,
            "duration": duration_value,
        }

        if request_json["duration"] == "9s" and request_json["resolution"] == "1080p":
            request_json["resolution"] = "720p"

        if image_url:
            request_json["image_url"] = image_url
        if end_image_url:
            request_json["end_image_url"] = end_image_url

        explicit_loop = provider_options.get("loop")
        if explicit_loop is not None:
            request_json["loop"] = bool(explicit_loop)
        elif is_premium_talking_background_plate:
            request_json["loop"] = True

        self._validate_request(model_id=model_id, request_json=request_json, payload=payload)

        submit_meta = {
            "provider_name": self.provider_name,
            "provider_model_name": model_id,
            "start_image_url_present": bool(image_url),
            "end_image_url_present": bool(end_image_url),
            "duration": request_json["duration"],
            "resolution": request_json["resolution"],
            "aspect_ratio": request_json["aspect_ratio"],
            "loop": request_json.get("loop"),
            "premium_talking_background_plate": is_premium_talking_background_plate,
            "scene_plate_only": self._boolish(provider_options.get("scene_plate_only") or provider_options.get("background_plate_only")),
            "motion_intent": self._string(provider_options.get("motion_intent") or self._tags(payload).get("motion_intent")) or None,
        }

        return ProviderPrepareResult(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            request_json={"model_id": model_id, "input": request_json},
            submit_meta=submit_meta,
        )

    async def submit(self, payload: Dict[str, Any], idempotency_key: str) -> ProviderSubmitResult:
        model_id = str(payload.get("model_id") or self.default_i2v_model).strip() or self.default_i2v_model
        input_payload = dict(payload.get("input") or {})
        try:
            resp = await self.client.submit(model_id, input_payload, idempotency_key=idempotency_key)
        except FalQueueError as e:
            raise LumaAdapterError(str(e)) from e

        request_id = str(resp.get("request_id") or "").strip()
        if not request_id:
            raise LumaAdapterError("luma submit missing request_id")

        encoded_provider_job_id = self._encode_provider_job_id(model_id, request_id)
        raw = dict(resp or {})
        raw["provider_model_name"] = model_id
        raw["request_id"] = request_id
        raw["encoded_provider_job_id"] = encoded_provider_job_id

        return ProviderSubmitResult(
            provider_job_id=encoded_provider_job_id,
            raw_response=raw,
        )

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
            raise LumaAdapterError(str(e)) from e

        video_url = self.client.extract_video_url(result)
        if not video_url:
            return ProviderPollResult(
                status="failed",
                raw_response={**result, "provider_model_name": model_id, "request_id": request_id},
                error_message="luma result missing video url",
            )

        return ProviderPollResult(
            status="succeeded",
            video_url=video_url,
            raw_response={**result, "provider_model_name": model_id, "request_id": request_id},
        )
