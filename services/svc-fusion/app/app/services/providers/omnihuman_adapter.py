from __future__ import annotations

import asyncio
import logging
import math
import os
import tempfile
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import fal_client
import httpx

from app.services.providers.base import (
    ProviderClient,
    ProviderEstimate,
    ProviderPrepareInput,
    ProviderPrepareResult,
    ProviderPollResult,
    ProviderSubmitResult,
)
from app.services.providers.fal_queue import FalQueueClient, FalQueueError
from app.services.artifact_service import ArtifactService


class OmniHumanAdapterError(RuntimeError):
    pass


logger = logging.getLogger("fusion.providers.omnihuman")


def _is_downstream_degraded_error(message: Optional[str]) -> bool:
    msg = str(message or "").strip().lower()
    if not msg:
        return False
    markers = (
        "downstream_service_unavailable",
        "downstream service unavailable",
        "service unavailable",
        "provider_degraded",
        "provider degraded",
    )
    return any(marker in msg for marker in markers)


def _preview_url(url: Optional[str], keep: int = 96) -> Optional[str]:
    if not url:
        return None
    s = str(url).strip()
    if len(s) <= keep:
        return s
    return s[:keep] + "..."


class OmniHumanAdapter(ProviderClient):
    """
    Production-ready OmniHuman v1.5 adapter.

    Key behavior:
    - Uses fal_client-backed queue transport through FalQueueClient
    - Uploads remote face/audio inputs to fal storage before submit
      to avoid provider-side file_download_error on Azure SAS URLs
    - Refreshes Azure Blob SAS URLs before provider input download/upload
    - Enforces OmniHuman duration/resolution limits
    - Keeps DesiFaces tier policy:
        free  -> default 720p
        paid  -> default 1080p
    - Optional product-level cap:
        DF_OMNIHUMAN_ENFORCE_PRODUCT_30S=1 by default
    """

    provider_name = "omnihuman_v15"
    provider_version = "fal.client.v2"

    def __init__(self) -> None:
        self.queue = FalQueueClient()
        self.model_id = (
            os.getenv("DF_OMNIHUMAN_MODEL_ID")
            or os.getenv("FAL_OMNIHUMAN_MODEL_ID")
            or "fal-ai/bytedance/omnihuman/v1.5"
        ).strip()

        self.default_turbo_mode = str(
            os.getenv("DF_OMNIHUMAN_TURBO_MODE", "0")
        ).strip().lower() in {"1", "true", "yes", "y"}

        self.enforce_product_30s = str(
            os.getenv("DF_OMNIHUMAN_ENFORCE_PRODUCT_30S", "1")
        ).strip().lower() in {"1", "true", "yes", "y"}

        self.upload_inputs_to_fal = str(
            os.getenv("DF_OMNIHUMAN_UPLOAD_INPUTS_TO_FAL", "1")
        ).strip().lower() in {"1", "true", "yes", "y"}

        self.input_download_timeout_s = int(
            os.getenv("DF_OMNIHUMAN_INPUT_DOWNLOAD_TIMEOUT_S", "300")
        )

        self.result_retry_attempts = int(
            os.getenv("DF_OMNIHUMAN_RESULT_RETRY_ATTEMPTS", "8")
        )
        self.result_retry_sleep_s = float(
            os.getenv("DF_OMNIHUMAN_RESULT_RETRY_SLEEP_S", "2.0")
        )
        self.artifact_service = ArtifactService()
        self.input_sas_ttl_hours = int(os.getenv("DF_OMNIHUMAN_INPUT_SAS_HOURS", "4"))

    async def estimate(self, request_payload: Dict[str, Any]) -> ProviderEstimate:
        duration_sec = self._duration_seconds(request_payload)
        estimated_units = str(max(1, int(math.ceil(duration_sec / 60.0)))) if duration_sec else "1"
        return ProviderEstimate(
            estimated_units=estimated_units,
            unit_type="minute",
            provider_meta={
                "provider": self.provider_name,
                "model_id": self.model_id,
                "duration_sec": duration_sec,
            },
        )

    async def prepare(self, spec: ProviderPrepareInput) -> ProviderPrepareResult:
        logger.info(
            "omnihuman.prepare start job_id=%s user_id=%s has_face_url=%s has_audio_url=%s",
            spec.job_id,
            spec.user_id,
            bool(spec.resolved_face_url),
            bool(spec.resolved_audio_url),
        )
        if not spec.resolved_face_url:
            raise OmniHumanAdapterError("omnihuman_v15 requires resolved_face_url")
        if not spec.resolved_audio_url:
            raise OmniHumanAdapterError("omnihuman_v15 requires resolved_audio_url")

        request_payload = dict(spec.request_payload or {})
        duration_sec = self._duration_seconds(request_payload)
        resolution = self._choose_resolution(request_payload, duration_sec)
        turbo_mode = self._choose_turbo_mode(request_payload)
        prompt = self._build_prompt(request_payload)

        self._validate_duration(duration_sec, resolution)

        source_face_url = spec.resolved_face_url
        source_audio_url = spec.resolved_audio_url

        if self.upload_inputs_to_fal:
            face_url = await self._upload_remote_file_to_fal(
                source_face_url,
                suffix_hint=self._suffix_from_url(source_face_url, ".png"),
            )
            audio_url = await self._upload_remote_file_to_fal(
                source_audio_url,
                suffix_hint=self._suffix_from_url(source_audio_url, ".mp3"),
            )
        else:
            face_url = await self._refresh_azure_blob_input_url(source_face_url)
            audio_url = await self._refresh_azure_blob_input_url(source_audio_url)

        request_json: Dict[str, Any] = {
            "image_url": face_url,
            "audio_url": audio_url,
            "resolution": resolution,
            "turbo_mode": turbo_mode,
        }
        if prompt:
            request_json["prompt"] = prompt

        logger.info(
            "omnihuman.prepare resolved job_id=%s duration_sec=%s resolution=%s turbo_mode=%s source_face_url=%s source_audio_url=%s fal_face_url=%s fal_audio_url=%s",
            spec.job_id,
            duration_sec,
            resolution,
            turbo_mode,
            _preview_url(source_face_url),
            _preview_url(source_audio_url),
            _preview_url(face_url),
            _preview_url(audio_url),
        )
        return ProviderPrepareResult(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            request_json=request_json,
            submit_meta={
                "provider": self.provider_name,
                "provider_model_name": self.model_id,
                "model_id": self.model_id,
                "resolution": resolution,
                "turbo_mode": turbo_mode,
                "duration_sec": duration_sec,
                "product_30s_cap": self.enforce_product_30s,
                "prompt_preview": prompt[:160] if prompt else None,
                "upload_inputs_to_fal": self.upload_inputs_to_fal,
                "source_face_url": source_face_url,
                "source_audio_url": source_audio_url,
                "fal_face_url": face_url,
                "fal_audio_url": audio_url,
            },
        )

    async def submit(self, payload: Dict[str, Any], idempotency_key: str) -> ProviderSubmitResult:
        logger.info(
            "omnihuman.submit start model_id=%s idempotency_key=%s image_url=%s audio_url=%s resolution=%s turbo_mode=%s",
            self.model_id,
            idempotency_key,
            _preview_url(payload.get("image_url")),
            _preview_url(payload.get("audio_url")),
            payload.get("resolution"),
            payload.get("turbo_mode"),
        )
        try:
            resp = await self.queue.submit(
                self.model_id,
                payload,
                idempotency_key=idempotency_key,
            )
        except FalQueueError as e:
            raise OmniHumanAdapterError(str(e)) from e

        provider_job_id = str(resp.get("request_id") or resp.get("id") or "").strip()
        if not provider_job_id:
            raise OmniHumanAdapterError(f"omnihuman_submit_missing_request_id: {resp}")

        raw = dict(resp or {})
        raw["provider_model_name"] = self.model_id
        raw["request_id"] = provider_job_id

        logger.info("omnihuman.submit ok model_id=%s provider_job_id=%s", self.model_id, provider_job_id)
        return ProviderSubmitResult(
            provider_job_id=provider_job_id,
            raw_response=raw,
        )

    async def poll(self, provider_job_id: str) -> ProviderPollResult:
        logger.debug("omnihuman.poll start model_id=%s provider_job_id=%s", self.model_id, provider_job_id)
        try:
            status_json = await self.queue.status(self.model_id, provider_job_id, logs=True)
        except FalQueueError as e:
            raise OmniHumanAdapterError(str(e)) from e

        status, error_message = self.queue.normalize_status(status_json)
        logger.info(
            "omnihuman.poll status model_id=%s provider_job_id=%s normalized_status=%s error_message=%s",
            self.model_id,
            provider_job_id,
            status,
            error_message,
        )

        if status == "processing":
            return ProviderPollResult(
                status="processing",
                raw_response=status_json,
            )

        if status == "canceled":
            return ProviderPollResult(
                status="canceled",
                raw_response=status_json,
                error_message=error_message,
            )

        if status == "failed":
            return ProviderPollResult(
                status="failed",
                raw_response=status_json,
                error_message=error_message or "omnihuman request failed",
            )

        last_result_json: Dict[str, Any] = {}
        for attempt in range(1, max(1, self.result_retry_attempts) + 1):
            try:
                result_json = await self.queue.result(self.model_id, provider_job_id)
                last_result_json = result_json or {}
            except FalQueueError as e:
                err_msg = str(e)
                logger.warning(
                    "omnihuman.poll result fetch retry model_id=%s provider_job_id=%s attempt=%s/%s err=%s",
                    self.model_id,
                    provider_job_id,
                    attempt,
                    self.result_retry_attempts,
                    err_msg,
                )
                if attempt >= max(1, self.result_retry_attempts):
                    if _is_downstream_degraded_error(err_msg):
                        return ProviderPollResult(
                            status="failed",
                            raw_response=status_json,
                            error_message=f"PROVIDER_DEGRADED_DOWNSTREAM_UNAVAILABLE: {err_msg}",
                        )
                    return ProviderPollResult(
                        status="processing",
                        raw_response=status_json,
                        error_message=f"result_not_ready: {err_msg}",
                    )
                await asyncio.sleep(self.result_retry_sleep_s)
                continue

            video_url = self.queue.extract_video_url(result_json)
            logger.info(
                "omnihuman.poll result model_id=%s provider_job_id=%s attempt=%s/%s has_video_url=%s video_url=%s",
                self.model_id,
                provider_job_id,
                attempt,
                self.result_retry_attempts,
                bool(video_url),
                _preview_url(video_url),
            )
            if video_url:
                return ProviderPollResult(
                    status="succeeded",
                    video_url=video_url,
                    raw_response=result_json,
                )

            if attempt < max(1, self.result_retry_attempts):
                await asyncio.sleep(self.result_retry_sleep_s)

        return ProviderPollResult(
            status="processing",
            raw_response=last_result_json or status_json,
            error_message="result_missing_video_url_pending_retry",
        )

    async def get_share_url(self, provider_job_id: str) -> Optional[str]:
        return None

    def _is_azure_blob_url(self, url: str) -> bool:
        try:
            host = (urlparse(url).netloc or "").lower()
        except Exception:
            return False
        return host.endswith(".blob.core.windows.net")

    async def _refresh_azure_blob_input_url(self, url: str) -> str:
        source_url = str(url or "").strip()
        if not source_url or not self._is_azure_blob_url(source_url):
            return source_url
        try:
            refreshed = await self.artifact_service.mint_read_sas_for_url(
                source_url,
                ttl_hours=self.input_sas_ttl_hours,
            )
            logger.info(
                "omnihuman.refresh_input_sas ok source_url=%s refreshed_url=%s",
                _preview_url(source_url),
                _preview_url(refreshed),
            )
            return refreshed
        except Exception:
            logger.exception(
                "omnihuman.refresh_input_sas failed source_url=%s",
                _preview_url(source_url),
            )
            return source_url

    async def _upload_remote_file_to_fal(self, url: str, *, suffix_hint: str) -> str:
        logger.info(
            "omnihuman.upload_input start source_url=%s suffix_hint=%s",
            _preview_url(url),
            suffix_hint,
        )
        if not url:
            raise OmniHumanAdapterError("missing remote file url for fal upload")

        if self._is_fal_hosted_url(url):
            return url

        source_url = str(url).strip()
        tmp_path: Optional[str] = None
        last_error: Optional[Exception] = None

        candidate_urls: List[str] = [source_url]
        refreshed_url = await self._refresh_azure_blob_input_url(source_url)
        if refreshed_url and refreshed_url != source_url:
            candidate_urls.insert(0, refreshed_url)

        for attempt_index, candidate_url in enumerate(candidate_urls, start=1):
            try:
                async with httpx.AsyncClient(
                    timeout=self.input_download_timeout_s,
                    follow_redirects=True,
                ) as client:
                    resp = await client.get(candidate_url)
                    resp.raise_for_status()
                    data = resp.content
                    logger.info(
                        "omnihuman.upload_input downloaded source_url=%s candidate_url=%s attempt=%s/%s bytes=%s",
                        _preview_url(source_url),
                        _preview_url(candidate_url),
                        attempt_index,
                        len(candidate_urls),
                        len(data or b""),
                    )

                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix_hint) as tmp:
                    tmp.write(data)
                    tmp_path = tmp.name

                uploaded_url = await asyncio.to_thread(fal_client.upload_file, tmp_path)
                logger.info(
                    "omnihuman.upload_input uploaded source_url=%s candidate_url=%s fal_url=%s",
                    _preview_url(source_url),
                    _preview_url(candidate_url),
                    _preview_url(str(uploaded_url or "")),
                )
                if not uploaded_url:
                    raise OmniHumanAdapterError("fal upload returned empty url")
                return str(uploaded_url)
            except Exception as e:
                last_error = e
                logger.warning(
                    "omnihuman.upload_input attempt failed source_url=%s candidate_url=%s attempt=%s/%s err=%s",
                    _preview_url(source_url),
                    _preview_url(candidate_url),
                    attempt_index,
                    len(candidate_urls),
                    str(e),
                )
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                    tmp_path = None

        raise OmniHumanAdapterError(f"fal upload failed for input asset: {last_error}") from last_error

    def _is_fal_hosted_url(self, url: str) -> bool:
        try:
            host = (urlparse(url).netloc or "").lower()
        except Exception:
            return False
        return (
            host.endswith(".fal.media")
            or host.endswith(".fal.ai")
            or "falserverless" in host
            or "fal.media" in host
        )

    def _suffix_from_url(self, url: str, default: str) -> str:
        try:
            path = urlparse(url).path or ""
            base = path.rsplit("/", 1)[-1]
            if "." in base:
                ext = "." + base.rsplit(".", 1)[-1].lower()
                if 1 < len(ext) <= 8:
                    return ext
        except Exception:
            pass
        return default

    def _duration_seconds(self, request_payload: Dict[str, Any]) -> float:
        video = request_payload.get("video") if isinstance(request_payload.get("video"), dict) else {}
        try:
            if video.get("duration_sec") is not None:
                return max(0.0, float(video.get("duration_sec")))
            if video.get("duration_ms") is not None:
                return max(0.0, float(video.get("duration_ms")) / 1000.0)
        except Exception:
            pass

        provider_options = request_payload.get("provider_options") if isinstance(request_payload.get("provider_options"), dict) else {}
        try:
            if provider_options.get("duration_sec") is not None:
                return max(0.0, float(provider_options.get("duration_sec")))
            if provider_options.get("duration_ms") is not None:
                return max(0.0, float(provider_options.get("duration_ms")) / 1000.0)
        except Exception:
            pass
        return 0.0

    def _validate_duration(self, duration_sec: float, resolution: str) -> None:
        if duration_sec <= 0:
            return

        if duration_sec > 60.0:
            raise OmniHumanAdapterError(
                f"omnihuman_v15_provider_limit_exceeded: duration_sec={duration_sec:.2f}. "
                "Provider supports max 60s at 720p."
            )

        if self.enforce_product_30s and duration_sec > 30.0:
            raise OmniHumanAdapterError(
                f"omnihuman_v15_product_cap_exceeded: duration_sec={duration_sec:.2f}. "
                "Use segmented story mode and stitch."
            )

        if resolution == "1080p" and duration_sec > 30.0:
            raise OmniHumanAdapterError(
                f"omnihuman_v15_1080p_limit_exceeded: duration_sec={duration_sec:.2f}. "
                "1080p supports max 30s."
            )

    def _choose_turbo_mode(self, request_payload: Dict[str, Any]) -> bool:
        provider_options = request_payload.get("provider_options") if isinstance(request_payload.get("provider_options"), dict) else {}
        value = provider_options.get("turbo_mode")
        if value is None:
            value = self.default_turbo_mode
        return str(value).strip().lower() in {"1", "true", "yes", "y"}

    def _choose_resolution(self, request_payload: Dict[str, Any], duration_sec: float) -> str:
        provider_options = request_payload.get("provider_options") if isinstance(request_payload.get("provider_options"), dict) else {}
        explicit = str(provider_options.get("resolution") or "").strip().lower()

        tier = self._tier_code(request_payload)
        max_resolution = "720p" if tier == "free" else "1080p"

        if explicit in {"720p", "1080p"}:
            if explicit == "1080p" and max_resolution == "720p":
                return "720p"
            return explicit

        return max_resolution

    def _tier_code(self, request_payload: Dict[str, Any]) -> str:
        provider_options = request_payload.get("provider_options") if isinstance(request_payload.get("provider_options"), dict) else {}
        candidates = [
            provider_options.get("_pricing_tier_code"),
            provider_options.get("tier_code"),
            request_payload.get("pricing_tier_code"),
            request_payload.get("tier_code"),
        ]
        for value in candidates:
            s = str(value or "").strip().lower()
            if not s:
                continue
            if s in {"enterprise", "business", "pro", "professional", "paid"}:
                return "pro"
            if s == "free":
                return "free"
        return "free"

    def _build_prompt(self, request_payload: Dict[str, Any]) -> str:
        provider_options = request_payload.get("provider_options") if isinstance(request_payload.get("provider_options"), dict) else {}
        tags = request_payload.get("tags") if isinstance(request_payload.get("tags"), dict) else {}

        candidates: List[Optional[str]] = [
            provider_options.get("prompt"),
            provider_options.get("user_prompt"),
            request_payload.get("user_prompt"),
            request_payload.get("video_prompt"),
            request_payload.get("performance_prompt"),
            request_payload.get("motion_prompt"),
            request_payload.get("movement_prompt"),
            request_payload.get("gesture_prompt"),
            request_payload.get("body_motion_prompt"),
            request_payload.get("emotion_prompt"),
            request_payload.get("expression_prompt"),
            request_payload.get("creative_direction"),
            request_payload.get("prompt"),
            tags.get("prompt_preview"),
            tags.get("user_prompt"),
            tags.get("prompt"),
        ]

        prompt = ""
        for value in candidates:
            s = str(value or "").strip()
            if s:
                prompt = s
                break

        baseline = (
            "Static or gently pushing medium shot. The person speaks naturally to camera with expressive eyes, "
            "realistic pauses, subtle head movement, and grounded human body language. "
            "Community-driven authenticity, premium realism, not corporate avatar aesthetics."
        )
        if not prompt:
            return baseline

        if "community-driven authenticity" in prompt.lower():
            return prompt

        return f"{prompt.strip()} Community-driven authenticity, premium realism, not corporate avatar aesthetics."
