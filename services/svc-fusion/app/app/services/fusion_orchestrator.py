from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import httpx

PRICING_IMPORT_ERROR: Optional[str] = None

try:
    from desifaces_shared.pricing.client import PricingClientError, SvcPricingClient
    from desifaces_shared.pricing.models import (
        PricingCommitRequest,
        PricingReleaseRequest,
        PricingReserveRequest,
    )
    from desifaces_shared.pricing.orchestration import (
        PricingReserveSpec,
        PricingCommitSpec,
        PricingReleaseSpec,
        apply_pricing_snapshot,
        build_commit_request,
        build_pricing_summary,
        build_release_request,
        build_reserve_request,
        make_committed_artifact,
        make_released_artifact,
        make_reserved_artifact,
    )
except Exception as pricing_import_error:
    PRICING_IMPORT_ERROR = str(pricing_import_error)
    logging.getLogger("fusion_orchestrator").exception(
        "svc_fusion_pricing_import_failed",
        extra={"error": PRICING_IMPORT_ERROR},
    )

    class PricingClientError(Exception):
        pass

    @dataclass
    class PricingReserveRequest:
        user_id: str
        service_name: str
        service_action: str
        sku_code: str
        units: str
        external_ref_type: str
        external_ref_id: str
        idempotency_key: str
        meta: Dict[str, Any]

    @dataclass
    class PricingCommitRequest:
        user_id: str
        reservation_id: str
        actual_units: str
        external_ref_type: str
        external_ref_id: str
        idempotency_key: str
        meta: Dict[str, Any]

    @dataclass
    class PricingReleaseRequest:
        user_id: str
        reservation_id: str
        reason: str
        external_ref_type: str
        external_ref_id: str
        idempotency_key: str
        meta: Dict[str, Any]

    @dataclass
    class PricingReserveSpec:
        user_id: str
        service_name: str
        service_action: str
        sku_code: str
        units: str
        external_ref_id: str
        idempotency_key: str
        meta: Dict[str, Any]
        quote_id: Optional[str] = None
        preview_fingerprint: Optional[str] = None
        external_ref_type: str = "studio_job"

    @dataclass
    class PricingCommitSpec:
        user_id: str
        reservation_id: str
        actual_units: str
        external_ref_id: str
        idempotency_key: str
        meta: Dict[str, Any]
        external_ref_type: str = "studio_job"

    @dataclass
    class PricingReleaseSpec:
        user_id: str
        reservation_id: str
        reason: str
        external_ref_id: str
        idempotency_key: str
        meta: Dict[str, Any]
        external_ref_type: str = "studio_job"

    def build_reserve_request(spec: PricingReserveSpec) -> PricingReserveRequest:
        return PricingReserveRequest(
            user_id=spec.user_id,
            service_name=spec.service_name,
            service_action=spec.service_action,
            sku_code=spec.sku_code,
            units=spec.units,
            external_ref_type=spec.external_ref_type,
            external_ref_id=spec.external_ref_id,
            idempotency_key=spec.idempotency_key,
            meta=spec.meta,
        )

    def build_commit_request(spec: PricingCommitSpec) -> PricingCommitRequest:
        return PricingCommitRequest(
            user_id=spec.user_id,
            reservation_id=spec.reservation_id,
            actual_units=spec.actual_units,
            external_ref_type=spec.external_ref_type,
            external_ref_id=spec.external_ref_id,
            idempotency_key=spec.idempotency_key,
            meta=spec.meta,
        )

    def build_release_request(spec: PricingReleaseSpec) -> PricingReleaseRequest:
        return PricingReleaseRequest(
            user_id=spec.user_id,
            reservation_id=spec.reservation_id,
            reason=spec.reason,
            external_ref_type=spec.external_ref_type,
            external_ref_id=spec.external_ref_id,
            idempotency_key=spec.idempotency_key,
            meta=spec.meta,
        )

    def build_pricing_summary(pricing: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return {}

    def apply_pricing_snapshot(
        target: Dict[str, Any],
        *,
        pricing: Optional[Dict[str, Any]] = None,
        pricing_summary: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        target["pricing"] = dict(pricing or {})
        target["pricing_summary"] = dict(pricing_summary or {})
        return target

    def make_reserved_artifact(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"pricing": {}, "pricing_summary": {}}

    def make_committed_artifact(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"pricing": {}, "pricing_summary": {}}

    def make_released_artifact(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"pricing": {}, "pricing_summary": {}}

    class SvcPricingClient:
        enabled = False

        @classmethod
        def from_env(cls, service_name: str) -> "SvcPricingClient":
            return cls()

        async def reserve(self, req: PricingReserveRequest):
            raise PricingClientError("pricing client unavailable")

        async def commit(self, req: PricingCommitRequest):
            raise PricingClientError("pricing client unavailable")

        async def release(self, req: PricingReleaseRequest):
            raise PricingClientError("pricing client unavailable")


from app.config import settings
from app.domain.enums import StepCode
from app.domain.models import FusionJobCreate
from app.repos.artifacts_repo import ArtifactsRepo
from app.repos.digital_performances_repo import DigitalPerformancesRepo
from app.repos.fusion_jobs_repo import FusionJobsRepo
from app.repos.provider_runs_repo import ProviderRunsRepo
from app.repos.steps_repo import StepsRepo
from app.services.artifact_service import ArtifactService
from app.services.idempotency_service import provider_idempotency_key, request_hash
from app.services.providers.base import (
    ProviderClient,
    ProviderPrepareInput,
    ProviderPrepareResult,
    ProviderPollResult,
    ProviderSubmitResult,
)
from app.services.providers.kling_adapter import KlingAdapter, KlingAdapterError
from app.services.providers.luma_adapter import LumaAdapter, LumaAdapterError
from app.services.providers.runway_adapter import RunwayAdapter, RunwayAdapterError
from app.services.providers.omnihuman_adapter import OmniHumanAdapter, OmniHumanAdapterError
from app.services.providers.veed_fabric_adapter import VeedFabricAdapter, VeedFabricAdapterError
try:
    from app.services.heygen_service import HeyGenService
except Exception as heygen_import_error:
    HeyGenService = None  # type: ignore[assignment]
    HEYGEN_IMPORT_ERROR = str(heygen_import_error)
else:
    HEYGEN_IMPORT_ERROR = None

logger = logging.getLogger("fusion_orchestrator")


def _preview_url(url: Optional[str], keep: int = 96) -> Optional[str]:
    if not url:
        return None
    s = str(url).strip()
    if len(s) <= keep:
        return s
    return s[:keep] + "..."


class FusionProviderError(RuntimeError):
    pass


class _DisabledPricingClient:
    enabled = False

    async def reserve(self, req: PricingReserveRequest):
        raise PricingClientError("pricing client unavailable")

    async def commit(self, req: PricingCommitRequest):
        raise PricingClientError("pricing client unavailable")

    async def release(self, req: PricingReleaseRequest):
        raise PricingClientError("pricing client unavailable")


@dataclass
class _PreparedProviderCache:
    provider_name: str
    provider_version: str
    request_json: Dict[str, Any]
    submit_meta: Dict[str, Any]


def _is_provider_degraded_message(message: Any) -> bool:
    msg = str(message or '').strip().lower()
    if not msg:
        return False
    markers = (
        'provider_degraded_downstream_unavailable',
        'provider_degraded',
        'downstream_service_unavailable',
        'downstream service unavailable',
        'provider degraded',
    )
    return any(marker in msg for marker in markers)


def _classify_error(e: Exception) -> str:
    msg = str(e).lower()

    if isinstance(e, (FusionProviderError, KlingAdapterError, LumaAdapterError, RunwayAdapterError, OmniHumanAdapterError, VeedFabricAdapterError)):
        if _is_provider_degraded_message(msg):
            return "PROVIDER_DEGRADED"
        if "insufficient credits" in msg:
            return "PROVIDER_INSUFFICIENT_CREDITS"
        if "timeout" in msg or "timed out" in msg:
            return "PROVIDER_TIMEOUT"
        if "face" in msg and "required" in msg:
            return "PROVIDER_FACE_REQUIRED"
        if "audio" in msg and "required" in msg:
            return "PROVIDER_AUDIO_REQUIRED"
        if "model" in msg and "not found" in msg:
            return "PROVIDER_MODEL_NOT_FOUND"
        return "PROVIDER_API_ERROR"

    if "heygen" in msg:
        if "timeout" in msg or "timed out" in msg:
            return "HEYGEN_TIMEOUT"
        if "provider disabled" in msg or "not wired" in msg:
            return "PROVIDER_DISABLED"
        return "HEYGEN_API_ERROR"

    if _is_provider_degraded_message(msg):
        return "PROVIDER_DEGRADED"
    if "provider disabled" in msg or "disabled for production" in msg:
        return "PROVIDER_DISABLED"
    if "requires" in msg and ("face" in msg or "audio" in msg):
        return "INVALID_REQUEST"
    if "pricing" in msg:
        return "PRICING_ERROR"
    if "timeout" in msg or "timed out" in msg:
        return "PROVIDER_TIMEOUT"
    return "FUSION_FAILED"


def _url_base(u: Optional[str]) -> Optional[str]:
    """
    Make request_hash stable when caller supplies SAS URLs.
    Drops querystring; keeps scheme+host+path.
    """
    if not u:
        return None
    s = str(u).strip()
    if not s:
        return None
    try:
        p = urlparse(s)
        if not p.scheme or not p.netloc:
            return s
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return s.split("?", 1)[0]


def _extract_pricing_error_code(e: Exception) -> str:
    msg = str(e or "")
    for code in (
        "PRICING_INSUFFICIENT_CREDITS",
        "PRICING_UNKNOWN_OR_INACTIVE_VARIANT",
        "PRICING_VARIANT_ZERO_QTY_LINES",
        "PRICING_CLIENT_DISABLED",
    ):
        if code in msg:
            return code
    if "pricing client unavailable" in msg.lower():
        return "PRICING_CLIENT_DISABLED"
    return "PRICING_RESERVATION_FAILED"


def _extract_talking_photo_id(obj: Dict[str, Any]) -> Optional[str]:
    """
    Best-effort extractor for newer HeyGen photo-avatar style responses.

    We do NOT treat plain image_key as talking_photo_id.
    """
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    candidates = [
        data.get("talking_photo_id"),
        obj.get("talking_photo_id"),
        data.get("photo_avatar_id"),
        obj.get("photo_avatar_id"),
        data.get("avatar_id"),
        obj.get("avatar_id"),
    ]
    if data.get("id") and not data.get("image_key"):
        candidates.append(data.get("id"))
    if obj.get("id") and not obj.get("image_key"):
        candidates.append(obj.get("id"))

    for v in candidates:
        if v:
            return str(v).strip()
    return None


def _provider_poll_timeout_seconds() -> int:
    try:
        return max(
            60,
            int(
                os.getenv(
                    "DF_FUSION_PROVIDER_TIMEOUT_SECONDS",
                    str(getattr(settings, "JOB_POLL_MAX_SECONDS", 360)),
                )
            ),
        )
    except Exception:
        return 360


def _provider_poll_interval_seconds() -> int:
    try:
        return max(
            2,
            int(
                os.getenv(
                    "DF_FUSION_PROVIDER_POLL_SECONDS",
                    str(getattr(settings, "JOB_POLL_INTERVAL_SECONDS", 5)),
                )
            ),
        )
    except Exception:
        return 5


def _adaptive_provider_poll_interval_seconds(
    *,
    elapsed_s: float,
    provider_name: Optional[str] = None,
    base_interval_s: Optional[int] = None,
) -> int:
    base = max(2, int(base_interval_s or _provider_poll_interval_seconds()))
    provider = str(provider_name or "").strip().lower()

    if provider == "heygen_av4":
        if elapsed_s < 20:
            return max(2, min(base, 3))
        if elapsed_s < 90:
            return max(3, base)
        if elapsed_s < 240:
            return max(5, base)
        return max(8, base + 2)

    if elapsed_s < 15:
        return max(2, min(base, 2))
    if elapsed_s < 60:
        return max(3, min(base, 4))
    if elapsed_s < 180:
        return max(5, base)
    return max(8, base + 2)


def _normalize_provider_status(raw_status: Optional[str]) -> str:
    s = str(raw_status or "").strip().lower()

    if s in {"completed", "complete", "success", "succeeded", "ready"}:
        return "succeeded"

    if s in {"failed", "error"}:
        return "failed"

    if s in {"canceled", "cancelled"}:
        return "canceled"

    if s in {
        "processing",
        "pending",
        "queued",
        "waiting",
        "running",
        "in_progress",
        "submitted",
    }:
        return "processing"

    return "unknown"


class FusionOrchestrator:
    """
    Provider-driven Fusion orchestration.

    Key behavior:
      - UI should prefer artifact IDs; Fusion mints fresh SAS at run time.
      - Provider-specific payload construction lives behind provider.prepare().
      - Provider idempotency is job-scoped to avoid cross-job reuse.
      - Pricing reserve / commit / release stays centralized here.
      - Default provider is HeyGen for talking clips; Kling/Luma/Runway use the generic provider path.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.jobs = FusionJobsRepo(pool)
        self.steps = StepsRepo(pool)
        self.runs = ProviderRunsRepo(pool)
        self.artifacts = ArtifactsRepo(pool)
        self.perfs = DigitalPerformancesRepo(pool)

        self.artifact_service = ArtifactService()
        self.default_provider_name = str(os.getenv("DF_FUSION_PROVIDER", "omnihuman_v15") or "omnihuman_v15").strip().lower()
        self.alias_heygen_to_hedra = str(
            os.getenv("DF_FUSION_ALIAS_HEYGEN_TO_HEDRA", "0")
        ).strip().lower() in {"1", "true", "yes", "y"}
        self.hedra_enabled = str(
            os.getenv("DF_FUSION_ENABLE_HEDRA", "0")
        ).strip().lower() in {"1", "true", "yes", "y"}
        self._provider_cache: Dict[str, ProviderClient] = {}

        try:
            self.pricing_client = SvcPricingClient.from_env(service_name="svc-fusion")
        except Exception as e:
            logger.exception(
                "svc_fusion_pricing_client_init_failed",
                extra={"error": str(e)},
            )
            self.pricing_client = _DisabledPricingClient()

    def _resolve_provider_name(self, requested: Optional[str]) -> str:
        raw = str(requested or self.default_provider_name or "omnihuman_v15").strip().lower()
        if raw in {"heygen", "heygen_av4", "heygen_v2", "native"}:
            return "heygen_av4"
        if raw in {"omnihuman_v15", "omnihuman"}:
            return "omnihuman_v15"
        if raw in {"veed", "veed_fabric", "veed_fabric_1", "fabric", "fabric_1_0", "veed/fabric-1.0"}:
            return "veed_fabric"
        if raw in {"kling", "kling_i2v", "kling_t2v"}:
            return "kling"
        if raw in {"luma", "luma_ray2", "luma_ray_2"}:
            return "luma"
        if raw in {"runway", "runway_gen3", "runway_gen4"}:
            return "runway"
        return raw

    def _get_provider(self, provider_name: str) -> ProviderClient:
        name = self._resolve_provider_name(provider_name)
        cached = self._provider_cache.get(name)
        if cached is not None:
            return cached

        if name == "omnihuman_v15":
            provider: ProviderClient = OmniHumanAdapter()
        elif name == "veed_fabric":
            provider = VeedFabricAdapter()
        elif name == "kling":
            provider = KlingAdapter()
        elif name == "luma":
            provider = LumaAdapter()
        elif name == "runway":
            provider = RunwayAdapter()
        else:
            raise ValueError(f"unsupported_fusion_provider: {provider_name}")

        self._provider_cache[name] = provider
        return provider

    def _prepared_log_meta(self, prepared: Optional[ProviderPrepareResult]) -> Dict[str, Any]:
        if not prepared:
            return {}
        request_json = self._coerce_dict(getattr(prepared, "request_json", None))
        submit_meta = self._coerce_dict(getattr(prepared, "submit_meta", None))
        return {
            "provider_name": getattr(prepared, "provider_name", None),
            "provider_version": getattr(prepared, "provider_version", None),
            "request_keys": sorted(request_json.keys()),
            "image_url": _preview_url(request_json.get("image_url")),
            "audio_url": _preview_url(request_json.get("audio_url")),
            "resolution": request_json.get("resolution") or submit_meta.get("resolution"),
            "duration_sec": submit_meta.get("duration_sec"),
            "model_id": submit_meta.get("model_id") or submit_meta.get("provider_model_name"),
            "prompt_preview": submit_meta.get("prompt_preview"),
        }

    def _poll_log_meta(self, poll: Optional[ProviderPollResult]) -> Dict[str, Any]:
        if not poll:
            return {}
        return {
            "status": getattr(poll, "status", None),
            "video_url": _preview_url(getattr(poll, "video_url", None)),
            "share_url": _preview_url(getattr(poll, "share_url", None)),
            "error_message": getattr(poll, "error_message", None),
        }

    async def _attempt_provider_timeout_recovery(
        self,
        *,
        provider: Optional[ProviderClient],
        provider_name: str,
        provider_job_id: Optional[str],
        job_id: str,
        attempts: int = 3,
        sleep_s: float = 2.0,
    ) -> Optional[ProviderPollResult]:
        if provider is None or not provider_job_id:
            return None
        if provider_name != "omnihuman_v15":
            return None

        last_poll: Optional[ProviderPollResult] = None
        for attempt in range(1, attempts + 1):
            try:
                poll = await provider.poll(provider_job_id)
                last_poll = poll
                video_url = str(getattr(poll, "video_url", None) or "").strip()
                logger.info(
                    "fusion provider timeout recovery poll job_id=%s provider=%s provider_job_id=%s attempt=%s/%s status=%s has_video_url=%s",
                    job_id, provider_name, provider_job_id, attempt, attempts, getattr(poll, "status", None), bool(video_url)
                )
                if video_url:
                    return poll
            except Exception as exc:
                logger.warning(
                    "fusion provider timeout recovery error job_id=%s provider=%s provider_job_id=%s attempt=%s/%s error=%s",
                    job_id, provider_name, provider_job_id, attempt, attempts, str(exc)
                )
            if attempt < attempts:
                await asyncio.sleep(sleep_s)
        return last_poll

    def _requested_billed_units(self, req: FusionJobCreate) -> int:
        try:
            video = req.video.model_dump()
        except Exception:
            video = {}
        duration_sec = video.get("duration_sec")
        duration_ms = video.get("duration_ms")
        try:
            if duration_sec is not None:
                return max(1, int(math.ceil(float(duration_sec) / 60.0)))
            if duration_ms is not None:
                return max(1, int(math.ceil(float(duration_ms) / 60000.0)))
        except Exception:
            pass
        return 1


    def _provider_disabled_message(self, provider_name: str) -> str:
        if provider_name == "heygen_av4" and HEYGEN_IMPORT_ERROR:
            return f"HeyGen provider is not wired correctly: {HEYGEN_IMPORT_ERROR}"
        if provider_name == "heygen_av4" and HeyGenService is None:
            return "HeyGen provider is not wired correctly in this deployment."
        if provider_name == "omnihuman_v15":
            return "OmniHuman provider is not wired correctly in this deployment."
        if provider_name == "veed_fabric":
            return "VEED Fabric provider is not wired correctly in this deployment."
        return f"Provider {provider_name} is not available."


    def _is_internal_child_request_payload(self, payload_json: Dict[str, Any]) -> bool:
        billing_context = self._coerce_dict(payload_json.get("billing_context"))
        mode = str(billing_context.get("mode") or "").strip().lower()
        if mode == "internal_child":
            return True
        return bool(billing_context.get("pricing_suppressed"))

    def _is_internal_child_pricing(self, pricing: Optional[Dict[str, Any]]) -> bool:
        pricing = self._coerce_dict(pricing)
        mode = str(pricing.get("billing_mode") or "").strip().lower()
        if mode == "internal_child":
            return True
        if bool(pricing.get("pricing_suppressed")):
            return True
        meta = self._coerce_dict(pricing.get("meta"))
        billing_context = self._coerce_dict(meta.get("billing_context"))
        child_mode = str(billing_context.get("mode") or "").strip().lower()
        return child_mode == "internal_child" or bool(billing_context.get("pricing_suppressed"))

    def _build_internal_child_pricing_block(
        self,
        *,
        req: FusionJobCreate,
        payload_json: Dict[str, Any],
        provider_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        provider_name = provider_name or self._resolve_provider_name(getattr(req, "provider", None))
        billing_context = self._coerce_dict(payload_json.get("billing_context"))
        parent_story_job_id = str(
            billing_context.get("parent_story_job_id")
            or payload_json.get("parent_story_job_id")
            or ""
        ).strip() or None
        render_kind = str(payload_json.get("render_kind") or "child_render").strip() or "child_render"
        return {
            "enabled": False,
            "pricing_suppressed": True,
            "state": "internal_child",
            "service_name": "svc-fusion",
            "service_action": "fusion.child.render",
            "variant_code": None,
            "sku_code": None,
            "leaf_sku_code": None,
            "estimated_units": "0",
            "unit_type": "internal",
            "reservation_id": None,
            "reservation_status": None,
            "quote_id": None,
            "reserved_units": None,
            "actual_units": None,
            "billed_units": None,
            "released_units": None,
            "amount": None,
            "currency": None,
            "ledger_entry_id": None,
            "billing_mode": "internal_child",
            "billing_account_id": None,
            "settlement_mode": None,
            "pricing_mode": "internal_child",
            "entitlement_source": "internal_child",
            "entitlement_reason": "parent_storytelling_job",
            "tier_code": None,
            "disabled_reason": None,
            "meta": {
                "provider": provider_name,
                "voice_mode": req.voice_mode.value,
                "video": req.video.model_dump(),
                "render_kind": render_kind,
                "parent_service": str(billing_context.get("parent_service") or "svc-fusion-extension"),
                "parent_story_job_id": parent_story_job_id,
                "billing_context": billing_context,
                "has_face_artifact_id": bool(getattr(req, "face_artifact_id", None)),
                "has_audio_artifact_id": bool(
                    getattr(getattr(req, "voice_audio", None), "audio_artifact_id", None)
                ),
                "has_provider_options": bool(getattr(req, "provider_options", None)),
            },
        }

    def _heygen_dimensions_for_request(self, req: FusionJobCreate) -> Dict[str, int]:
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

    async def _download_face_image_tempfile(self, face_url: str) -> str:
        suffix = ".png"
        try:
            parsed = urlparse(str(face_url))
            _, ext = os.path.splitext(parsed.path or "")
            if ext and len(ext) <= 5:
                suffix = ext
        except Exception:
            pass

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(face_url)
            resp.raise_for_status()
            data = resp.content

        fd, temp_path = tempfile.mkstemp(prefix="df_fusion_face_", suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            raise
        return temp_path

    def _extract_video_direction_prompt(self, payload_json: Dict[str, Any]) -> str:
        provider_options = self._coerce_dict(payload_json.get("provider_options"))
        tags = self._coerce_dict(payload_json.get("tags"))
        candidates = [
            payload_json.get("user_prompt"),
            payload_json.get("video_prompt"),
            payload_json.get("performance_prompt"),
            payload_json.get("motion_prompt"),
            payload_json.get("movement_prompt"),
            payload_json.get("gesture_prompt"),
            payload_json.get("body_motion_prompt"),
            payload_json.get("emotion_prompt"),
            payload_json.get("expression_prompt"),
            payload_json.get("creative_direction"),
            payload_json.get("prompt"),
            provider_options.get("user_prompt"),
            provider_options.get("prompt"),
            tags.get("prompt_preview"),
            tags.get("user_prompt"),
            tags.get("prompt"),
        ]
        for value in candidates:
            s = str(value or "").strip()
            if s:
                return s
        return ""

    async def _run_heygen_direct_job(
        self,
        *,
        job_id: str,
        req: FusionJobCreate,
        provider_idem: str,
        payload_json: Dict[str, Any],
    ) -> Dict[str, Any]:
        if HeyGenService is None:
            raise RuntimeError(self._provider_disabled_message("heygen_av4"))
        if req.voice_mode.value != "audio":
            raise RuntimeError("HeyGen production path currently requires audio mode")

        resolved_face_url = await self._resolve_face_url(job_id, req)
        resolved_audio_url = await self._resolve_audio_url(job_id, req)
        logger.info("fusion.heygen submit prep job_id=%s resolved_face_url=%s resolved_audio_url=%s", job_id, _preview_url(resolved_face_url), _preview_url(resolved_audio_url))
        await self.artifacts.add_artifact(
            job_id,
            "provider_audio_ref",
            resolved_audio_url,
            content_type="text/uri-list",
            meta_json={"provider": "heygen_av4"},
        )

        creative_prompt = self._extract_video_direction_prompt(payload_json)
        dimensions = self._heygen_dimensions_for_request(req)
        temp_face_path = await self._download_face_image_tempfile(resolved_face_url)
        try:
            svc = HeyGenService()
            submit_fn = getattr(svc, "submit_video_from_azure_assets", None)
            if callable(submit_fn):
                try:
                    result = await submit_fn(
                        req=req,
                        face_image_path=temp_face_path,
                        audio_blob_path=resolved_audio_url,
                        idempotency_key=provider_idem,
                        dimension=dimensions,
                        test_mode=False,
                        creative_prompt=creative_prompt or None,
                    )
                except TypeError:
                    result = await submit_fn(
                        face_image_path=temp_face_path,
                        audio_blob_path=resolved_audio_url,
                        idempotency_key=provider_idem,
                        dimension=dimensions,
                        test_mode=False,
                        creative_prompt=creative_prompt or None,
                    )
            else:
                legacy_fn = getattr(svc, "create_video_from_azure_assets", None)
                if not callable(legacy_fn):
                    raise RuntimeError("HeyGenService missing submit/create video method")
                result = await legacy_fn(
                    face_image_path=temp_face_path,
                    audio_blob_path=resolved_audio_url,
                    idempotency_key=provider_idem,
                    dimension=dimensions,
                    test_mode=False,
                )
        finally:
            try:
                os.remove(temp_face_path)
            except Exception:
                pass

        result_dict = self._coerce_dict(result)
        provider_job_id = self._string_or_none(
            result_dict.get("provider_job_id")
            or result_dict.get("video_id")
            or result_dict.get("id")
            or self._coerce_dict(result_dict.get("data")).get("video_id")
            or self._coerce_dict(result_dict.get("data")).get("id")
        )
        if not provider_job_id:
            raise RuntimeError("HeyGen submit did not return a provider job id")

        logger.info("fusion.heygen submit ok job_id=%s provider_job_id=%s share_url=%s", job_id, provider_job_id, _preview_url(self._string_or_none(result_dict.get("share_url"))))
        return {
            "provider_job_id": provider_job_id,
            "share_url": self._string_or_none(result_dict.get("share_url")),
            "raw_response": self._coerce_dict(result_dict.get("raw_response")) or result_dict,
            "submit_meta": {
                "provider": "heygen_av4",
                "path": "direct_service_submit_only",
                "dimensions": dimensions,
                "prompt_preview": (creative_prompt or "")[:160] or None,
                "has_audio": True,
                "audio_url_preview": resolved_audio_url[:120],
            },
        }

    async def _poll_heygen_direct_job(self, provider_job_id: str) -> Dict[str, Any]:
        if HeyGenService is None:
            raise RuntimeError(self._provider_disabled_message("heygen_av4"))

        svc = HeyGenService()
        poll_fn = getattr(svc, "poll_video", None)
        if callable(poll_fn):
            result = await poll_fn(provider_job_id)
            return self._coerce_dict(result)

        status_fn = getattr(svc, "get_video_status", None)
        if callable(status_fn):
            result = await status_fn(provider_job_id)
            return self._coerce_dict(result)

        raise RuntimeError("HeyGenService missing poll/status method")

    async def _mark_job_processing_for_retry(
        self,
        *,
        job_id: str,
        provider_name: str,
        provider_job_id: str,
        run_id: Optional[str],
        performance_id: Optional[str],
        user_id: str,
        reason: str,
        retry_after_s: int,
        meta_json: Optional[Dict[str, Any]] = None,
    ) -> None:
        retry_meta = {
            "provider": provider_name,
            "provider_job_id": provider_job_id,
            "recovery_reason": reason,
            "retry_after_s": retry_after_s,
            **(meta_json or {}),
        }
        try:
            await self.jobs.set_status(job_id, "processing")
        except Exception:
            logger.warning("fusion_set_processing_status_failed", extra={"job_id": job_id})
        try:
            await self.steps.upsert_step(
                job_id,
                StepCode.provider_poll.value,
                "running",
                attempt=0,
                meta_json=retry_meta,
            )
        except Exception:
            logger.warning("fusion_step_retry_mark_failed", extra={"job_id": job_id})
        try:
            if run_id:
                await self.runs.update_status(run_id, "processing", meta_json=retry_meta)
        except Exception:
            logger.warning("fusion_run_retry_mark_failed", extra={"job_id": job_id, "run_id": run_id})
        try:
            perf_id = performance_id or await self.perfs.upsert_performance(
                user_id=user_id,
                provider=provider_name,
                provider_job_id=provider_job_id,
                status="processing",
                share_url=None,
                meta_json={"job_id": job_id, **retry_meta},
            )
            await self.perfs.upsert_fusion_job_output(job_id, perf_id)
        except Exception:
            logger.warning("fusion_perf_retry_mark_failed", extra={"job_id": job_id, "performance_id": performance_id})

    # -------------------------------------------------------------------------
    # Generic helpers
    # -------------------------------------------------------------------------
    @staticmethod
    def _coerce_dict(v: Any) -> Dict[str, Any]:
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                vv = json.loads(v)
                return vv if isinstance(vv, dict) else {}
            except Exception:
                return {}
        try:
            if hasattr(v, "keys"):
                return {k: v[k] for k in v.keys()}
        except Exception:
            pass
        try:
            vv = dict(v)
            return vv if isinstance(vv, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _pricing_resp_get(resp: Any, key: str, default: Any = None) -> Any:
        if resp is None:
            return default
        if isinstance(resp, dict):
            value = resp.get(key, default)
        else:
            value = getattr(resp, key, default)
        if hasattr(value, "value"):
            try:
                return value.value
            except Exception:
                return default if value is None else value
        return value

    @staticmethod
    def _string_or_none(v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @staticmethod
    def _normalize_settlement_mode(v: Any) -> str:
        s = str(v or "").strip().lower()
        if s in {"postpaid", "invoice", "bill", "billed"}:
            return "postpaid"
        if s in {"prepaid", "credit", "credits", "wallet", "payg"}:
            return "prepaid"
        if s in {"hybrid", "mixed"}:
            return "hybrid"
        return s

    def _canonicalize_pricing_entitlement(
        self,
        pricing: Optional[Dict[str, Any]],
        *,
        resp: Any = None,
    ) -> Dict[str, Any]:
        out = dict(pricing or {})

        billing_account_id = self._string_or_none(
            self._pricing_resp_get(resp, "billing_account_id") if resp is not None else None
        ) or self._string_or_none(out.get("billing_account_id"))
        settlement_mode = self._normalize_settlement_mode(
            self._pricing_resp_get(resp, "settlement_mode") if resp is not None else out.get("settlement_mode")
        ) or self._normalize_settlement_mode(out.get("settlement_mode"))
        billing_mode = self._string_or_none(
            self._pricing_resp_get(resp, "billing_mode") if resp is not None else None
        ) or self._string_or_none(out.get("billing_mode"))
        pricing_mode = self._string_or_none(
            self._pricing_resp_get(resp, "pricing_mode") if resp is not None else None
        ) or self._string_or_none(out.get("pricing_mode"))

        explicit_tier = self._string_or_none(
            self._pricing_resp_get(resp, "tier_code") if resp is not None else out.get("tier_code")
        ) or ""
        explicit_source = self._string_or_none(
            self._pricing_resp_get(resp, "entitlement_source") if resp is not None else out.get("entitlement_source")
        ) or ""
        explicit_reason = self._string_or_none(
            self._pricing_resp_get(resp, "entitlement_reason") if resp is not None else out.get("entitlement_reason")
        ) or ""

        weak_tier = bool(billing_account_id and explicit_tier.lower() == "free")
        weak_source = bool(billing_account_id and explicit_source.lower() == "module_gate_fallback")

        if billing_account_id:
            out["billing_account_id"] = billing_account_id
        if settlement_mode:
            out["settlement_mode"] = settlement_mode
        if billing_mode:
            out["billing_mode"] = billing_mode
        if pricing_mode:
            out["pricing_mode"] = pricing_mode

        if explicit_tier and not weak_tier:
            out["tier_code"] = explicit_tier
        elif billing_account_id and settlement_mode == "postpaid":
            out["tier_code"] = "enterprise"
        elif billing_account_id and settlement_mode == "hybrid":
            out["tier_code"] = "business"
        elif explicit_tier:
            out["tier_code"] = explicit_tier

        if explicit_source and not weak_source:
            out["entitlement_source"] = explicit_source
        elif billing_account_id and settlement_mode == "postpaid":
            out["entitlement_source"] = "credit_account"
        elif billing_account_id:
            out["entitlement_source"] = "billing_account"
        elif explicit_source:
            out["entitlement_source"] = explicit_source

        if explicit_reason:
            out["entitlement_reason"] = explicit_reason
        elif billing_account_id and (weak_tier or weak_source):
            out["entitlement_reason"] = "billing_account_context_override"
        elif billing_account_id and not self._string_or_none(out.get("entitlement_reason")):
            out["entitlement_reason"] = "billing_account_context_fallback"

        return out

    def _sas_ttl_hours(self) -> int:
        ttl = getattr(settings, "AZURE_SAS_EXPIRY_HOURS", None)
        try:
            if ttl:
                return max(1, int(ttl))
        except Exception:
            pass
        return 4
    @staticmethod
    def _is_azure_blob_url(url: Optional[str]) -> bool:
        s = str(url or "").strip()
        if not s:
            return False
        try:
            host = (urlparse(s).netloc or "").lower()
        except Exception:
            return False
        return host.endswith(".blob.core.windows.net")

    async def _refresh_input_url_if_azure_blob(self, url: Optional[str]) -> Optional[str]:
        source_url = str(url or "").strip()
        if not source_url:
            return None
        if not self._is_azure_blob_url(source_url):
            return source_url
        try:
            return await self.artifact_service.mint_read_sas_for_url(
                source_url,
                ttl_hours=self._sas_ttl_hours(),
            )
        except Exception:
            logger.exception(
                "fusion_refresh_input_sas_failed",
                extra={"url_preview": _preview_url(source_url)},
            )
            return source_url


    def _pricing_required(self) -> bool:
        v = str(os.getenv("DF_PRICING_REQUIRED", "0")).strip().lower()
        return v in {"1", "true", "yes", "y"}

    def _pricing_enabled(self) -> bool:
        try:
            return bool(getattr(self.pricing_client, "enabled", False))
        except Exception:
            return False

    def _pricing_disabled_reason(self) -> str:
        if PRICING_IMPORT_ERROR:
            return f"pricing_import_failed: {PRICING_IMPORT_ERROR}"
        return "svc-fusion pricing client is disabled or not configured"

    def _fusion_pricing_variant_code(self) -> str:
        """
        Reserve must send pricing_variants.code, not the leaf pricing_skus.code.
        """
        candidate = str(os.getenv("DF_PRICING_VARIANT_FUSION_VIDEO", "") or "").strip()
        if candidate:
            return candidate

        legacy = str(os.getenv("DF_PRICING_SKU_FUSION_VIDEO", "") or "").strip()
        if legacy:
            # Map previously-used/wrong values to the correct active variant.
            if legacy in {"fusion.video.generate", "FUSION_TALK_MIN"}:
                return "FUSION_TALKING_VIDEO"
            return legacy

        return "FUSION_TALKING_VIDEO"

    def _fusion_pricing_leaf_sku_code(self) -> str:
        candidate = str(os.getenv("DF_PRICING_LEAF_SKU_FUSION_VIDEO", "") or "").strip()
        if candidate:
            if candidate == "FUSION_TALKING_VIDEO":
                return "FUSION_TALK_MIN"
            return candidate
        return "FUSION_TALK_MIN"

    # -------------------------------------------------------------------------
    # Pricing helpers
    # -------------------------------------------------------------------------
    def _build_initial_pricing_block(self, req: FusionJobCreate) -> Dict[str, Any]:
        variant_code = self._fusion_pricing_variant_code()
        leaf_sku_code = self._fusion_pricing_leaf_sku_code()
        pricing_enabled = self._pricing_enabled()
        state = "pending_reservation" if pricing_enabled else "disabled"
        disabled_reason = None if pricing_enabled else self._pricing_disabled_reason()

        estimated_units = str(self._requested_billed_units(req))
        estimated_minutes = estimated_units
        provider_name = self._resolve_provider_name(getattr(req, "provider", None))

        return {
            "enabled": pricing_enabled,
            "state": state,
            "service_name": "svc-fusion",
            "service_action": "fusion.video.generate",
            "variant_code": variant_code,
            "sku_code": variant_code,
            "leaf_sku_code": leaf_sku_code,
            "estimated_units": estimated_units,
            "unit_type": "minute",
            "reservation_id": None,
            "reservation_status": None,
            "quote_id": None,
            "reserved_units": None,
            "actual_units": None,
            "billed_units": None,
            "released_units": None,
            "amount": None,
            "currency": None,
            "ledger_entry_id": None,
            "billing_mode": None,
            "billing_account_id": None,
            "settlement_mode": None,
            "pricing_mode": None,
            "entitlement_source": None,
            "entitlement_reason": None,
            "tier_code": None,
            "disabled_reason": disabled_reason,
            "meta": {
                "provider": provider_name,
                "voice_mode": req.voice_mode.value,
                "video": req.video.model_dump(),
                "variant_code": variant_code,
                "leaf_sku_code": leaf_sku_code,
                "minutes": estimated_minutes,
                "requested_units": estimated_units,
                "has_face_artifact_id": bool(getattr(req, "face_artifact_id", None)),
                "has_audio_artifact_id": bool(
                    getattr(getattr(req, "voice_audio", None), "audio_artifact_id", None)
                ),
                "has_provider_options": bool(getattr(req, "provider_options", None)),
            },
        }

    @staticmethod
    def _merge_pricing_block(current: Optional[Dict[str, Any]], **updates: Any) -> Dict[str, Any]:
        out = dict(current or {})
        for key, value in updates.items():
            if value is not None:
                out[key] = value
        return out

    def _pricing_from_payload_meta(
        self,
        payload_json: Optional[Dict[str, Any]],
        meta_json: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        payload = self._coerce_dict(payload_json)
        meta = self._coerce_dict(meta_json)

        pricing = self._coerce_dict(payload.get("pricing"))
        if pricing:
            return self._canonicalize_pricing_entitlement(pricing)

        pricing = self._coerce_dict(meta.get("pricing"))
        if pricing:
            return self._canonicalize_pricing_entitlement(pricing)

        return {}

    async def _persist_pricing_block(
        self,
        job_id: str,
        pricing: Dict[str, Any],
        pricing_summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        pricing = self._canonicalize_pricing_entitlement(dict(pricing or {}))
        pricing_summary = dict(pricing_summary or build_pricing_summary(pricing))

        q = """
        UPDATE public.studio_jobs
        SET
          payload_json = COALESCE(payload_json, '{}'::jsonb)
                         || jsonb_build_object(
                              'pricing', $2::jsonb,
                              'pricing_summary', $3::jsonb
                            ),
          meta_json = COALESCE(meta_json, '{}'::jsonb)
                      || jsonb_build_object(
                           'pricing', $2::jsonb,
                           'pricing_summary', $3::jsonb,
                           'pricing_state', COALESCE($4::text, ''),
                           'pricing_enabled', $5::bool,
                           'pricing_billing_mode', NULLIF($6::text, ''),
                           'pricing_settlement_mode', NULLIF($7::text, ''),
                           'pricing_billing_account_id', NULLIF($8::text, '')
                         ),
          updated_at = now()
        WHERE id = $1::uuid
          AND studio_type = 'fusion'
        """
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    q,
                    job_id,
                    json.dumps(pricing or {}),
                    json.dumps(pricing_summary or {}),
                    str(pricing.get("state") or ""),
                    bool(pricing.get("enabled", False)),
                    str(pricing.get("billing_mode") or ""),
                    str(pricing.get("settlement_mode") or ""),
                    str(pricing.get("billing_account_id") or ""),
                )
        except Exception:
            logger.exception("fusion_pricing_persist_failed", extra={"job_id": job_id})

    async def _merge_job_meta(self, job_id: str, patch: Dict[str, Any]) -> None:
        if not patch:
            return
        q = """
        UPDATE public.studio_jobs
        SET
          meta_json = COALESCE(meta_json, '{}'::jsonb) || $2::jsonb,
          updated_at = now()
        WHERE id = $1::uuid
          AND studio_type = 'fusion'
        """
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(q, job_id, json.dumps(patch or {}))
        except Exception:
            logger.exception("fusion_job_meta_patch_failed", extra={"job_id": job_id, "patch_keys": sorted((patch or {}).keys())})

    async def _load_job_meta(self, job_id: str) -> Dict[str, Any]:
        q = """
        SELECT meta_json
        FROM public.studio_jobs
        WHERE id = $1::uuid
          AND studio_type = 'fusion'
        LIMIT 1
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(q, job_id)
            if not row:
                return {}
            return self._coerce_dict(row["meta_json"])
        except Exception:
            logger.exception("fusion_job_meta_load_failed", extra={"job_id": job_id})
            return {}

    async def _persist_provider_prepare_cache(
        self,
        job_id: str,
        *,
        provider_name: str,
        provider_version: str,
        request_json: Dict[str, Any],
        submit_meta: Optional[Dict[str, Any]] = None,
        resolved_face_url: Optional[str] = None,
        resolved_audio_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
    ) -> None:
        cache = {
            "provider_name": provider_name,
            "provider_version": provider_version,
            "request_json": dict(request_json or {}),
            "submit_meta": dict(submit_meta or {}),
            "resolved_face_url": resolved_face_url,
            "resolved_audio_url": resolved_audio_url,
            "reference_image_urls": list(reference_image_urls or []),
        }
        await self._merge_job_meta(job_id, {"provider_prepare_cache": cache})

    async def _load_provider_prepare_cache(
        self,
        job_id: str,
        *,
        provider_name: str,
    ) -> Optional[_PreparedProviderCache]:
        meta = await self._load_job_meta(job_id)
        cache = self._coerce_dict(meta.get("provider_prepare_cache"))
        if not cache:
            return None
        cached_provider = str(cache.get("provider_name") or "").strip().lower()
        if cached_provider != str(provider_name or "").strip().lower():
            return None
        request_json = self._coerce_dict(cache.get("request_json"))
        if not request_json:
            return None
        return _PreparedProviderCache(
            provider_name=str(cache.get("provider_name") or provider_name),
            provider_version=str(cache.get("provider_version") or "v1"),
            request_json=request_json,
            submit_meta=self._coerce_dict(cache.get("submit_meta")),
        )

    async def _persist_light_status(
        self,
        job_id: str,
        *,
        provider: Optional[str] = None,
        provider_job_id: Optional[str] = None,
        provider_status: Optional[str] = None,
        primary_video_url: Optional[str] = None,
        share_url: Optional[str] = None,
        progress_pct: Optional[float] = None,
        error_message: Optional[str] = None,
    ) -> None:
        light_status = {
            "provider": provider,
            "provider_job_id": provider_job_id,
            "provider_status": provider_status,
            "primary_video_url": primary_video_url,
            "share_url": share_url,
            "progress_pct": progress_pct,
            "error_message": error_message,
        }
        patch = {
            "provider": provider,
            "provider_job_id": provider_job_id,
            "provider_status": provider_status,
            "primary_video_url": primary_video_url,
            "share_url": share_url,
            "progress_pct": progress_pct,
            "light_status": light_status,
        }
        await self._merge_job_meta(job_id, patch)

    async def _has_video_artifact(self, job_id: str) -> bool:
        try:
            rows = await self.artifacts.list_artifacts(job_id)
        except Exception:
            logger.exception("fusion_list_artifacts_failed", extra={"job_id": job_id})
            return False
        for row in rows or []:
            kind = str((row or {}).get("kind") or "").strip().lower()
            content_type = str((row or {}).get("content_type") or "").strip().lower()
            url = str((row or {}).get("url") or "").strip()
            if kind == "video" and url:
                return True
            if content_type.startswith("video/") and url:
                return True
        return False

    async def _finalize_from_primary_video_url(
        self,
        *,
        job_id: str,
        job: Dict[str, Any],
        req: FusionJobCreate,
        provider_name: str,
        provider_job_id: str,
        primary_video_url: str,
        pricing: Optional[Dict[str, Any]] = None,
    ) -> str:
        pricing = dict(pricing or {})
        user_id = str(job.get("user_id") or "").strip()
        final_video_url = primary_video_url

        await self.steps.upsert_step(job_id, StepCode.finalize.value, "running", attempt=0)

        already_has_video = await self._has_video_artifact(job_id)
        if not already_has_video:
            final_video_url = await self.artifact_service.persist_video_artifact(
                primary_video_url,
                user_id=user_id,
                job_id=job_id,
                provider_job_id=provider_job_id,
            )
            await self.artifacts.add_artifact(
                job_id,
                "video",
                final_video_url,
                content_type="video/mp4",
                meta_json={
                    "provider": provider_name,
                    "provider_job_id": provider_job_id,
                    "recovered_from_primary_video_url": True,
                },
            )

        try:
            perf_id = await self.perfs.upsert_performance(
                user_id=user_id,
                provider=provider_name,
                provider_job_id=provider_job_id,
                status="processing",
                share_url=None,
                meta_json={
                    "job_id": job_id,
                    "provider_job_id": provider_job_id,
                    "recovered_finalize": True,
                },
            )
            await self.perfs.upsert_fusion_job_output(job_id, perf_id)
            await self.perfs.mark_ready(
                perf_id,
                share_url=None,
                meta_json={
                    "job_id": job_id,
                    "provider_job_id": provider_job_id,
                    "video_url": final_video_url,
                    "status": "ready",
                    "user_id": user_id,
                    "provider": provider_name,
                    "recovered_finalize": True,
                },
            )
        except Exception:
            logger.warning("fusion_recovery_perf_mark_ready_failed", extra={"job_id": job_id, "provider_job_id": provider_job_id})

        latest_pricing = await self._load_latest_pricing(job_id)
        if latest_pricing:
            pricing = latest_pricing

        pricing_state = str(pricing.get("state") or "").strip().lower()
        if pricing.get("enabled") and pricing_state not in {"committed", "released"}:
            pricing = await self._commit_pricing_for_job(
                job_id=job_id,
                user_id=user_id,
                pricing=pricing,
                actual_units=self._requested_billed_units(req),
            )

        await self._persist_light_status(
            job_id,
            provider=provider_name,
            provider_job_id=provider_job_id,
            provider_status="succeeded",
            primary_video_url=final_video_url,
        )
        await self.steps.upsert_step(job_id, StepCode.finalize.value, "succeeded", attempt=0)
        await self.jobs.set_status(job_id, "succeeded")
        return final_video_url

    async def claim_stale_processing_job_ids(
        self,
        *,
        limit: int,
        stale_seconds: int,
        claim_ttl_seconds: int,
        owner: Optional[str] = None,
    ) -> List[str]:
        owner = owner or f"fusion-recovery:{os.getpid()}"
        if hasattr(self.jobs, "claim_stale_processing_jobs"):
            return await self.jobs.claim_stale_processing_jobs(
                studio_type="fusion",
                limit=limit,
                stale_seconds=stale_seconds,
                claim_ttl_seconds=claim_ttl_seconds,
                owner=owner,
            )
        q = """
        WITH cand AS (
            SELECT id
            FROM public.studio_jobs
            WHERE studio_type = $1
              AND status = 'processing'
              AND updated_at < now() - make_interval(secs => $2::int)
              AND COALESCE(NULLIF(meta_json->>'recovery_claimed_at', '')::timestamptz, to_timestamp(0))
                    < now() - make_interval(secs => $3::int)
            ORDER BY updated_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT $4
        )
        UPDATE public.studio_jobs j
        SET meta_json = COALESCE(j.meta_json, '{}'::jsonb)
                        || jsonb_build_object(
                             'recovery_claimed_at', now()::text,
                             'recovery_owner', $5::text,
                             'recovery_reason', 'stale_processing',
                             'recovery_attempts', COALESCE((NULLIF(j.meta_json->>'recovery_attempts', ''))::int, 0) + 1
                           ),
            updated_at = now()
        FROM cand
        WHERE j.id = cand.id
        RETURNING j.id::text
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q, "fusion", int(stale_seconds), int(claim_ttl_seconds), int(limit), owner)
        return [str(r["id"]) for r in rows]

    async def recover_job(self, job_id: str) -> None:
        job = await self.jobs.get_job(job_id)
        if not job:
            return
        status = str(job.get("status") or "").strip().lower()
        if status in {"succeeded", "failed", "blocked", "canceled", "cancelled"}:
            return

        payload_json = job.get("payload_json")
        if isinstance(payload_json, str):
            payload_json = json.loads(payload_json)
        if not isinstance(payload_json, dict):
            raise ValueError(f"Unexpected payload_json type during recovery: {type(payload_json)}")
        req = FusionJobCreate.model_validate(payload_json)

        meta = await self._load_job_meta(job_id)
        light = self._coerce_dict(meta.get("light_status"))
        provider_name = self._resolve_provider_name(
            meta.get("provider") or light.get("provider") or payload_json.get("provider") or getattr(req, "provider", None)
        )
        provider_job_id = str(
            meta.get("provider_job_id") or light.get("provider_job_id") or ""
        ).strip()
        primary_video_url = str(
            meta.get("primary_video_url") or light.get("primary_video_url") or ""
        ).strip()
        pricing = await self._load_latest_pricing(job_id)

        await self._merge_job_meta(job_id, {"recovery_last_started_at": datetime.utcnow().isoformat() + "Z"})

        if primary_video_url:
            await self._finalize_from_primary_video_url(
                job_id=job_id,
                job=job,
                req=req,
                provider_name=provider_name,
                provider_job_id=provider_job_id or f"recovered:{job_id}",
                primary_video_url=primary_video_url,
                pricing=pricing,
            )
            return

        await self.run_job(job_id)

    async def recover_stale_processing_jobs_once(
        self,
        *,
        limit: int,
        stale_seconds: int,
        claim_ttl_seconds: int,
        owner: Optional[str] = None,
    ) -> int:
        job_ids = await self.claim_stale_processing_job_ids(
            limit=limit,
            stale_seconds=stale_seconds,
            claim_ttl_seconds=claim_ttl_seconds,
            owner=owner,
        )
        if not job_ids:
            return 0
        results = await asyncio.gather(*(self.recover_job(job_id) for job_id in job_ids), return_exceptions=True)
        for job_id, result in zip(job_ids, results):
            if isinstance(result, Exception):
                logger.exception("fusion_recover_job_failed", extra={"job_id": job_id}, exc_info=(type(result), result, result.__traceback__))
        return len(job_ids)

    async def _load_latest_pricing(self, job_id: str) -> Dict[str, Any]:
        q = """
        SELECT payload_json, meta_json
        FROM public.studio_jobs
        WHERE id = $1::uuid
          AND studio_type = 'fusion'
        LIMIT 1
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(q, job_id)
            if not row:
                return {}
            payload_json = self._coerce_dict(row["payload_json"])
            meta_json = self._coerce_dict(row["meta_json"])
            return self._pricing_from_payload_meta(payload_json, meta_json)
        except Exception:
            logger.exception("fusion_pricing_load_failed", extra={"job_id": job_id})
            return {}

    async def _await_reserved_pricing(
        self,
        job_id: str,
        *,
        max_wait_s: float = 8.0,
        poll_s: float = 0.25,
    ) -> Dict[str, Any]:
        if not self._pricing_enabled():
            return {}

        deadline = asyncio.get_running_loop().time() + max_wait_s
        last_pricing: Dict[str, Any] = {}

        while True:
            pricing = await self._load_latest_pricing(job_id)
            last_pricing = pricing or {}

            if not last_pricing.get("enabled"):
                return last_pricing

            state = str(last_pricing.get("state") or "").strip().lower()
            reservation_id = str(last_pricing.get("reservation_id") or "").strip()

            if state in {"reserved", "committed", "released", "reservation_failed", "commit_failed", "release_failed"}:
                return last_pricing

            if reservation_id and state in {"pending_reservation", ""}:
                out = dict(last_pricing)
                out["state"] = "reserved"
                out["reservation_status"] = out.get("reservation_status") or "reserved"
                return out

            if asyncio.get_running_loop().time() >= deadline:
                return last_pricing

            await asyncio.sleep(poll_s)

    async def _reserve_pricing_for_job(
        self,
        *,
        job_id: str,
        user_id: str,
        pricing: Dict[str, Any],
    ) -> Dict[str, Any]:
        if self._is_internal_child_pricing(pricing):
            return pricing
        if not self._pricing_enabled():
            return pricing

        variant_code = str(
            pricing.get("variant_code")
            or pricing.get("sku_code")
            or self._fusion_pricing_variant_code()
        ).strip() or self._fusion_pricing_variant_code()
        leaf_sku_code = str(
            pricing.get("leaf_sku_code") or self._fusion_pricing_leaf_sku_code()
        ).strip() or self._fusion_pricing_leaf_sku_code()

        requested_units = str(pricing.get("estimated_units") or "1").strip() or "1"

        meta = self._coerce_dict(pricing.get("meta"))
        minutes = str(meta.get("minutes") or requested_units).strip() or requested_units

        meta = {
            **meta,
            "variant_code": variant_code,
            "leaf_sku_code": leaf_sku_code,
            "minutes": minutes,
            "requested_units": requested_units,
            "service_action": str(pricing.get("service_action") or "fusion.video.generate"),
        }

        req = PricingReserveRequest(
            user_id=str(user_id),
            service_name="svc-fusion",
            service_action=str(pricing.get("service_action") or "fusion.video.generate"),
            sku_code=variant_code,
            units=requested_units,
            external_ref_type="studio_job",
            external_ref_id=str(job_id),
            idempotency_key=f"svc-fusion:job:{job_id}:reserve",
            meta=meta,
        )

        try:
            logger.info(
                "fusion_pricing_reserve_request",
                extra={
                    "user_id": req.user_id,
                    "service_name": req.service_name,
                    "service_action": req.service_action,
                    "variant_code": variant_code,
                    "leaf_sku_code": leaf_sku_code,
                    "sku_code": req.sku_code,
                    "units": req.units,
                    "external_ref_type": req.external_ref_type,
                    "external_ref_id": req.external_ref_id,
                    "idempotency_key": req.idempotency_key,
                    "meta": req.meta,
                },
            )
            resp = await self.pricing_client.reserve(req)
            reserve_status = str(self._pricing_resp_get(resp, "status", "reserved") or "reserved")

            pricing = self._merge_pricing_block(
                pricing,
                state="reserved",
                variant_code=self._pricing_resp_get(resp, "variant_code") or variant_code,
                sku_code=self._pricing_resp_get(resp, "variant_code") or variant_code,
                leaf_sku_code=self._pricing_resp_get(resp, "sku_code") or leaf_sku_code,
                reservation_id=self._pricing_resp_get(resp, "reservation_id"),
                quote_id=self._pricing_resp_get(resp, "quote_id"),
                reserved_units=self._pricing_resp_get(resp, "reserved_units") or pricing.get("estimated_units"),
                reservation_status=reserve_status,
                amount=self._pricing_resp_get(resp, "amount"),
                currency=self._pricing_resp_get(resp, "currency"),
                billing_mode=self._pricing_resp_get(resp, "billing_mode"),
                billing_account_id=self._pricing_resp_get(resp, "billing_account_id"),
                settlement_mode=self._pricing_resp_get(resp, "settlement_mode"),
                pricing_mode=self._pricing_resp_get(resp, "pricing_mode"),
                entitlement_source=self._pricing_resp_get(resp, "entitlement_source"),
                entitlement_reason=self._pricing_resp_get(resp, "entitlement_reason"),
                tier_code=self._pricing_resp_get(resp, "tier_code"),
                meta=meta,
                disabled_reason=None,
            )
            pricing = self._canonicalize_pricing_entitlement(pricing, resp=resp)
            await self._persist_pricing_block(job_id, pricing, build_pricing_summary(pricing))
            return pricing
        except Exception as e:
            logger.exception(
                "fusion_pricing_reserve_failed",
                extra={
                    "job_id": job_id,
                    "user_id": user_id,
                    "variant_code": variant_code,
                    "leaf_sku_code": leaf_sku_code,
                },
            )
            pricing = self._merge_pricing_block(
                pricing,
                state="reservation_failed",
                variant_code=variant_code,
                sku_code=variant_code,
                leaf_sku_code=leaf_sku_code,
                meta=meta,
                error=str(e),
            )
            await self._persist_pricing_block(job_id, pricing)
            if isinstance(e, PricingClientError):
                raise
            raise PricingClientError(str(e)) from e

    async def _commit_pricing_for_job(
        self,
        *,
        job_id: str,
        user_id: str,
        pricing: Dict[str, Any],
        actual_units: int,
    ) -> Dict[str, Any]:
        if self._is_internal_child_pricing(pricing):
            return pricing
        if not self._pricing_enabled():
            return pricing

        latest_pricing = await self._load_latest_pricing(job_id)
        if latest_pricing:
            pricing = latest_pricing

        reservation_id = str(pricing.get("reservation_id") or "").strip()
        state = str(pricing.get("state") or "").strip().lower()

        if (not reservation_id) or (state not in {"reserved", "commit_failed"}):
            awaited = await self._await_reserved_pricing(job_id)
            if awaited:
                pricing = awaited
                reservation_id = str(pricing.get("reservation_id") or "").strip()
                state = str(pricing.get("state") or "").strip().lower()

        if not reservation_id:
            pricing = self._merge_pricing_block(
                pricing,
                state="commit_failed",
                actual_units=str(max(1, int(actual_units))),
                error="missing_reservation_id_at_commit",
            )
            await self._persist_pricing_block(job_id, pricing)
            return pricing

        if state not in {"reserved", "commit_failed"}:
            return pricing

        variant_code = str(
            pricing.get("variant_code")
            or pricing.get("sku_code")
            or self._fusion_pricing_variant_code()
        ).strip() or self._fusion_pricing_variant_code()
        leaf_sku_code = str(
            pricing.get("leaf_sku_code") or self._fusion_pricing_leaf_sku_code()
        ).strip() or self._fusion_pricing_leaf_sku_code()

        try:
            commit_meta = {
                "variant_code": variant_code,
                "sku_code": variant_code,
                "leaf_sku_code": leaf_sku_code,
                "minutes": str(max(1, int(actual_units))),
                "service_action": pricing.get("service_action"),
                "requested_units": pricing.get("estimated_units"),
            }
            commit_spec = PricingCommitSpec(
                user_id=str(user_id),
                reservation_id=reservation_id,
                actual_units=str(max(1, int(actual_units))),
                external_ref_id=str(job_id),
                idempotency_key=f"svc-fusion:job:{job_id}:commit",
                meta=commit_meta,
            )
            resp = await self.pricing_client.commit(build_commit_request(commit_spec))
            commit_artifact = make_committed_artifact(
                resp,
                base_pricing=pricing,
                actual_units=str(max(1, int(actual_units))),
                meta=commit_meta,
            )
            commit_status = str(self._pricing_resp_get(resp, "status", "committed") or "committed")

            pricing = dict(pricing or {})
            artifact_pricing = dict(self._coerce_dict(commit_artifact.get("pricing")))
            artifact_summary = dict(self._coerce_dict(commit_artifact.get("pricing_summary")))
            pricing.update(artifact_pricing)

            pricing["enabled"] = True
            pricing["state"] = "committed"
            pricing["variant_code"] = self._pricing_resp_get(resp, "variant_code") or variant_code
            pricing["sku_code"] = self._pricing_resp_get(resp, "variant_code") or variant_code
            pricing["leaf_sku_code"] = self._pricing_resp_get(resp, "sku_code") or leaf_sku_code
            pricing["commit_status"] = commit_status
            pricing["reservation_status"] = commit_status
            pricing["actual_units"] = str(max(1, int(actual_units)))
            pricing["amount"] = self._pricing_resp_get(resp, "amount") or pricing.get("amount")
            pricing["currency"] = self._pricing_resp_get(resp, "currency") or pricing.get("currency")
            pricing["billing_mode"] = self._pricing_resp_get(resp, "billing_mode") or pricing.get("billing_mode")
            pricing["billing_account_id"] = self._pricing_resp_get(resp, "billing_account_id") or pricing.get("billing_account_id")
            pricing["settlement_mode"] = self._pricing_resp_get(resp, "settlement_mode") or pricing.get("settlement_mode")
            pricing["pricing_mode"] = self._pricing_resp_get(resp, "pricing_mode") or pricing.get("pricing_mode")
            pricing["entitlement_source"] = self._pricing_resp_get(resp, "entitlement_source") or pricing.get("entitlement_source")
            pricing["entitlement_reason"] = self._pricing_resp_get(resp, "entitlement_reason") or pricing.get("entitlement_reason")
            pricing["tier_code"] = self._pricing_resp_get(resp, "tier_code") or pricing.get("tier_code")
            pricing["ledger_entry_id"] = self._pricing_resp_get(resp, "ledger_entry_id") or pricing.get("ledger_entry_id")
            pricing["invoice_id"] = self._pricing_resp_get(resp, "invoice_id") or pricing.get("invoice_id")
            pricing["disabled_reason"] = None
            pricing["error"] = None
            pricing["error_code"] = None

            meta_block = dict(self._coerce_dict(pricing.get("meta")))
            meta_block.update(commit_meta)
            pricing["meta"] = meta_block
            pricing = self._canonicalize_pricing_entitlement(pricing, resp=resp)

            await self._persist_pricing_block(
                job_id,
                pricing,
                artifact_summary or build_pricing_summary(pricing),
            )
            return pricing
        except Exception as e:
            logger.exception(
                "fusion_pricing_commit_failed",
                extra={"job_id": job_id, "reservation_id": reservation_id, "user_id": user_id},
            )
            pricing = self._merge_pricing_block(
                pricing,
                state="commit_failed",
                actual_units=str(max(1, int(actual_units))),
                error=str(e),
            )
            await self._persist_pricing_block(job_id, pricing)
            return pricing

    async def _release_pricing_for_job(
        self,
        *,
        job_id: str,
        user_id: str,
        pricing: Dict[str, Any],
        reason: str,
    ) -> Dict[str, Any]:
        if self._is_internal_child_pricing(pricing):
            return pricing
        if not self._pricing_enabled():
            return pricing

        latest_pricing = await self._load_latest_pricing(job_id)
        if latest_pricing:
            pricing = latest_pricing

        reservation_id = str(pricing.get("reservation_id") or "").strip()
        state = str(pricing.get("state") or "").strip().lower()

        if (not reservation_id) or (state not in {"reserved", "release_failed"}):
            awaited = await self._await_reserved_pricing(job_id)
            if awaited:
                pricing = awaited
                reservation_id = str(pricing.get("reservation_id") or "").strip()
                state = str(pricing.get("state") or "").strip().lower()

        if not reservation_id:
            return pricing

        if state not in {"reserved", "release_failed"}:
            return pricing

        variant_code = str(
            pricing.get("variant_code")
            or pricing.get("sku_code")
            or self._fusion_pricing_variant_code()
        ).strip() or self._fusion_pricing_variant_code()
        leaf_sku_code = str(
            pricing.get("leaf_sku_code") or self._fusion_pricing_leaf_sku_code()
        ).strip() or self._fusion_pricing_leaf_sku_code()

        try:
            release_meta = {
                "variant_code": variant_code,
                "sku_code": variant_code,
                "leaf_sku_code": leaf_sku_code,
                "minutes": str(pricing.get("estimated_units") or "1"),
                "service_action": pricing.get("service_action"),
            }
            release_spec = PricingReleaseSpec(
                user_id=str(user_id),
                reservation_id=reservation_id,
                reason=reason,
                external_ref_id=str(job_id),
                idempotency_key=f"svc-fusion:job:{job_id}:release",
                meta=release_meta,
            )
            resp = await self.pricing_client.release(build_release_request(release_spec))
            release_artifact = make_released_artifact(
                resp,
                base_pricing=pricing,
                meta=release_meta,
            )
            release_status = str(self._pricing_resp_get(resp, "status", "released") or "released")

            pricing = dict(pricing or {})
            artifact_pricing = dict(self._coerce_dict(release_artifact.get("pricing")))
            artifact_summary = dict(self._coerce_dict(release_artifact.get("pricing_summary")))
            pricing.update(artifact_pricing)

            pricing["enabled"] = True
            pricing["state"] = "released"
            pricing["variant_code"] = self._pricing_resp_get(resp, "variant_code") or variant_code
            pricing["sku_code"] = self._pricing_resp_get(resp, "variant_code") or variant_code
            pricing["leaf_sku_code"] = self._pricing_resp_get(resp, "sku_code") or leaf_sku_code
            pricing["release_status"] = release_status
            pricing["reservation_status"] = release_status
            pricing["amount"] = self._pricing_resp_get(resp, "amount") or pricing.get("amount")
            pricing["currency"] = self._pricing_resp_get(resp, "currency") or pricing.get("currency")
            pricing["billing_mode"] = self._pricing_resp_get(resp, "billing_mode") or pricing.get("billing_mode")
            pricing["billing_account_id"] = self._pricing_resp_get(resp, "billing_account_id") or pricing.get("billing_account_id")
            pricing["settlement_mode"] = self._pricing_resp_get(resp, "settlement_mode") or pricing.get("settlement_mode")
            pricing["pricing_mode"] = self._pricing_resp_get(resp, "pricing_mode") or pricing.get("pricing_mode")
            pricing["entitlement_source"] = self._pricing_resp_get(resp, "entitlement_source") or pricing.get("entitlement_source")
            pricing["entitlement_reason"] = self._pricing_resp_get(resp, "entitlement_reason") or pricing.get("entitlement_reason")
            pricing["tier_code"] = self._pricing_resp_get(resp, "tier_code") or pricing.get("tier_code")
            pricing["disabled_reason"] = None
            pricing["error"] = None
            pricing["error_code"] = None

            meta_block = dict(self._coerce_dict(pricing.get("meta")))
            meta_block.update(release_meta)
            pricing["meta"] = meta_block
            pricing = self._canonicalize_pricing_entitlement(pricing, resp=resp)

            await self._persist_pricing_block(
                job_id,
                pricing,
                artifact_summary or build_pricing_summary(pricing),
            )
            return pricing
        except Exception as e:
            logger.exception(
                "fusion_pricing_release_failed",
                extra={"job_id": job_id, "user_id": user_id, "reason": reason},
            )
            pricing = self._merge_pricing_block(
                pricing,
                state="release_failed",
                error=str(e),
            )
            await self._persist_pricing_block(job_id, pricing)
            return pricing

    # -------------------------------------------------------------------------
    # Job create + input resolution
    # -------------------------------------------------------------------------
    async def create_job(self, user_id: str, req: FusionJobCreate) -> str:
        """
        Create a new fusion job.

        IMPORTANT:
        - Prefer stable IDs (artifact IDs / blob paths / stripped URLs) for hashing.
        - Pricing-enabled jobs start as pricing_pending and only become queued after reserve succeeds.
        - Insufficient credits is a soft product block: return a blocked job with job_id.
        """
        face_artifact_id = getattr(req, "face_artifact_id", None)

        voice_audio = getattr(req, "voice_audio", None)
        audio_artifact_id = getattr(voice_audio, "audio_artifact_id", None) if voice_audio else None

        provider_name = self._resolve_provider_name(getattr(req, "provider", None))
        payload = dict(req.model_dump())
        payload["provider"] = provider_name
        internal_child = self._is_internal_child_request_payload(payload)

        stable_spec: Dict[str, Any] = {
            "provider": provider_name,
            "voice_mode": req.voice_mode.value,
            "face_artifact_id": str(face_artifact_id) if face_artifact_id else None,
            "audio_artifact_id": str(audio_artifact_id) if audio_artifact_id else None,
            "face_image_url_base": (
                _url_base(str(req.face_image_url)) if getattr(req, "face_image_url", None) else None
            ),
            "voice_audio_url_base": (
                _url_base(str(voice_audio.audio_url)) if (voice_audio and voice_audio.audio_url) else None
            ),
            "voice_id": req.voice_tts.voice_id if req.voice_tts else None,
            "script": req.voice_tts.script if req.voice_tts else None,
            "video": req.video.model_dump(),
            "provider_options": self._coerce_dict(payload.get("provider_options")),
        }

        disable_create_dedupe = str(os.getenv("DF_FUSION_DISABLE_CREATE_DEDUPE", "0")).strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
        }
        if disable_create_dedupe:
            stable_spec["request_nonce"] = uuid4().hex

        stable_spec = {k: v for k, v in stable_spec.items() if v is not None}
        req_hash = request_hash(stable_spec)

        pricing_enabled = self._pricing_enabled() and not internal_child
        initial_status = "pricing_pending" if pricing_enabled else "queued"
        logger.info("fusion.create_job start user_id=%s provider=%s voice_mode=%s pricing_enabled=%s internal_child=%s has_face_artifact_id=%s has_audio_artifact_id=%s has_face_image_url=%s", user_id, provider_name, req.voice_mode.value, pricing_enabled, internal_child, bool(face_artifact_id), bool(audio_artifact_id), bool(getattr(req, "face_image_url", None)))

        job_id = await self.jobs.insert_job(
            user_id=user_id,
            request_hash=req_hash,
            payload=payload,
            initial_status=initial_status,
        )

        pricing = (
            self._build_internal_child_pricing_block(req=req, payload_json=payload, provider_name=provider_name)
            if internal_child
            else self._build_initial_pricing_block(req)
        )
        await self._persist_pricing_block(job_id, pricing)

        if not internal_child and self._pricing_required() and not pricing_enabled:
            reason = self._pricing_disabled_reason()
            pricing = self._merge_pricing_block(
                pricing,
                state="disabled",
                error=reason,
                disabled_reason=reason,
            )
            await self._persist_pricing_block(job_id, pricing)
            try:
                await self.jobs.set_status(
                    job_id,
                    "failed",
                    error_code="PRICING_CLIENT_DISABLED",
                    error_message=reason,
                )
            except Exception:
                logger.exception("fusion_set_failed_status_pricing_disabled", extra={"job_id": job_id})
            raise PricingClientError(reason)

        provider_disabled_reason = None
        if provider_name == "heygen_av4" and HeyGenService is None:
            provider_disabled_reason = self._provider_disabled_message("heygen_av4")

        if provider_disabled_reason:
            pricing = self._merge_pricing_block(
                pricing,
                state="disabled",
                error=provider_disabled_reason,
                disabled_reason=provider_disabled_reason,
            )
            await self._persist_pricing_block(job_id, pricing)
            try:
                await self.jobs.set_status(
                    job_id,
                    "blocked",
                    error_code="PROVIDER_DISABLED",
                    error_message=provider_disabled_reason,
                )
            except Exception:
                logger.exception("fusion_set_blocked_status_provider_disabled", extra={"job_id": job_id})
            return job_id

        if pricing_enabled:
            try:
                await self._reserve_pricing_for_job(
                    job_id=job_id,
                    user_id=str(user_id),
                    pricing=pricing,
                )
                await self.jobs.set_status(job_id, "queued")
            except PricingClientError as e:
                error_code = _extract_pricing_error_code(e)
                blocked = error_code == "PRICING_INSUFFICIENT_CREDITS"

                try:
                    await self.jobs.set_status(
                        job_id,
                        "blocked" if blocked else "failed",
                        error_code=error_code,
                        error_message=str(e),
                    )
                except Exception:
                    logger.exception(
                        "fusion_set_failed_status_after_reservation_error",
                        extra={"job_id": job_id},
                    )

                if blocked:
                    return job_id

                raise

        logger.info("fusion.create_job queued job_id=%s provider=%s initial_status=%s", job_id, provider_name, initial_status)
        return job_id


    async def _resolve_face_url(self, job_id: str, req: FusionJobCreate) -> str:
        """
        Resolve face input to a fetchable URL/SAS.

        Production rule:
          - if face_artifact_id is present, prefer it and mint a fresh SAS
          - else if a direct Azure Blob URL is supplied, refresh the SAS
          - else return the direct URL unchanged
        """
        face_artifact_id = getattr(req, "face_artifact_id", None)

        if face_artifact_id:
            row = await self.artifacts.get_artifact_by_id(str(face_artifact_id))
            if not row:
                raise ValueError(f"face_artifact_not_found: {face_artifact_id}")

            kind = str(row.get("kind") or "")
            if kind and kind not in ("face", "image", "face_image"):
                logger.warning(
                    "face_artifact_kind_unexpected",
                    extra={"job_id": job_id, "kind": kind, "artifact_id": str(face_artifact_id)},
                )

            face_url = await self.artifact_service.mint_read_sas_for_artifact(
                dict(row),
                ttl_hours=self._sas_ttl_hours(),
            )

            await self.artifacts.add_artifact(
                job_id,
                "resolved_face_sas_url",
                face_url,
                content_type="text/uri-list",
                meta_json={"source": "artifact_id", "artifact_id": str(face_artifact_id)},
            )
            return str(face_url)

        direct_face_url = getattr(req, "face_image_url", None)
        if direct_face_url:
            refreshed_face_url = await self._refresh_input_url_if_azure_blob(str(direct_face_url))
            if refreshed_face_url and refreshed_face_url != str(direct_face_url):
                await self.artifacts.add_artifact(
                    job_id,
                    "resolved_face_sas_url",
                    refreshed_face_url,
                    content_type="text/uri-list",
                    meta_json={"source": "face_image_url_refresh"},
                )
            return str(refreshed_face_url or direct_face_url)

        raise ValueError("Provide face_image_url or face_artifact_id")


    async def _resolve_audio_url(self, job_id: str, req: FusionJobCreate) -> str:
        """
        Resolve audio input to a fetchable Azure Blob SAS URL.

        Production rule:
          - if voice_audio.audio_artifact_id or audio_asset_id is present, prefer it and mint a fresh SAS
          - else if a direct Azure Blob URL is supplied, refresh the SAS
          - else return the direct URL unchanged
        """
        audio_artifact_id = None
        direct_audio_url = None
        if req.voice_audio:
            direct_audio_url = str(req.voice_audio.audio_url) if getattr(req.voice_audio, 'audio_url', None) else None
            audio_artifact_id = (
                str(getattr(req.voice_audio, 'audio_artifact_id', None) or '').strip()
                or str(getattr(req.voice_audio, 'audio_asset_id', None) or '').strip()
                or None
            )

        if audio_artifact_id:
            row = await self.artifacts.get_artifact_by_id(audio_artifact_id)  # type: ignore[attr-defined]
            if not row:
                raise ValueError(f"audio_artifact_not_found: {audio_artifact_id}")

            audio_url = await self.artifact_service.mint_read_sas_for_artifact(
                dict(row),
                ttl_hours=self._sas_ttl_hours(),
            )

            await self.artifacts.add_artifact(
                job_id,
                "resolved_audio_sas_url",
                audio_url,
                content_type="text/uri-list",
                meta_json={"source": "voice_audio.audio_artifact_id", "artifact_id": audio_artifact_id},
            )
            return str(audio_url)

        if direct_audio_url:
            refreshed_audio_url = await self._refresh_input_url_if_azure_blob(direct_audio_url)
            if refreshed_audio_url and refreshed_audio_url != direct_audio_url:
                await self.artifacts.add_artifact(
                    job_id,
                    "resolved_audio_sas_url",
                    refreshed_audio_url,
                    content_type="text/uri-list",
                    meta_json={"source": "voice_audio.audio_url_refresh"},
                )
            return str(refreshed_audio_url or direct_audio_url)

        raise ValueError("voice_mode=audio requires voice_audio.audio_url or voice_audio.audio_artifact_id")

    async def _resolve_talking_photo_id(self, job_id: str, req: FusionJobCreate) -> str:
        raise ValueError("heygen_talking_photo_id flow is retired; use Hedra provider inputs instead")

    async def _resolve_reference_image_urls(
        self,
        job_id: str,
        payload_json: Dict[str, Any],
    ) -> List[str]:
        provider_options = self._coerce_dict(payload_json.get("provider_options"))
        direct_urls = payload_json.get("reference_image_urls") or provider_options.get("reference_image_urls") or []
        artifact_ids = (
            payload_json.get("reference_image_artifact_ids")
            or provider_options.get("reference_image_artifact_ids")
            or []
        )

        out: List[str] = []
        seen = set()

        def _append(url: Optional[str]) -> None:
            if not url:
                return
            u = str(url).strip()
            if not u or u in seen:
                return
            seen.add(u)
            out.append(u)

        if isinstance(direct_urls, list):
            for item in direct_urls:
                if item:
                    _append(str(item))

        if isinstance(artifact_ids, list):
            for artifact_id in artifact_ids:
                if not artifact_id:
                    continue
                row = await self.artifacts.get_artifact_by_id(str(artifact_id))
                if not row:
                    logger.warning(
                        "reference_image_artifact_not_found",
                        extra={"job_id": job_id, "artifact_id": str(artifact_id)},
                    )
                    continue
                sas_url = await self.artifact_service.mint_read_sas_for_artifact(
                    dict(row),
                    ttl_hours=self._sas_ttl_hours(),
                )
                await self.artifacts.add_artifact(
                    job_id,
                    "resolved_reference_image_sas_url",
                    sas_url,
                    content_type="text/uri-list",
                    meta_json={"source": "reference_image_artifact_id", "artifact_id": str(artifact_id)},
                )
                _append(str(sas_url))

        return out

    # -------------------------------------------------------------------------
    # Job runner
    # -------------------------------------------------------------------------
    async def run_job(self, job_id: str) -> None:
        logger.info("fusion.run_job start job_id=%s", job_id)
        job = await self.jobs.get_job(job_id)
        if not job:
            logger.warning("job_not_found", extra={"job_id": job_id})
            return

        status = str(job.get("status") or "")
        if status in ("blocked", "succeeded", "failed", "canceled"):
            logger.info("job_terminal_skip", extra={"job_id": job_id, "status": status})
            return

        payload_json = job["payload_json"]
        if isinstance(payload_json, str):
            payload_json = json.loads(payload_json)
        if not isinstance(payload_json, dict):
            raise ValueError(f"Unexpected payload_json type: {type(payload_json)}")

        req = FusionJobCreate.model_validate(payload_json)

        provider_name = self._resolve_provider_name(payload_json.get("provider") or getattr(req, "provider", None))
        logger.info("fusion.run_job loaded job_id=%s provider=%s status=%s voice_mode=%s", job_id, provider_name, status, req.voice_mode.value)
        provider: Optional[ProviderClient] = None
        provider_version = "direct.v2" if provider_name == "heygen_av4" else "v1"
        if provider_name != "heygen_av4":
            provider = self._get_provider(provider_name)
            provider_version = str(getattr(provider, "provider_version", "v1") or "v1")

        req_hash = str(job["request_hash"])
        idem = provider_idempotency_key(provider_name, provider_version, req_hash)
        provider_idem = f"{idem}:{job_id}"

        run_id: Optional[str] = None
        provider_job_id: Optional[str] = None
        last_poll: Optional[ProviderPollResult] = None
        performance_id: Optional[str] = None
        prepared: Optional[ProviderPrepareResult] = None
        pricing: Dict[str, Any] = {}

        user_id = str(job.get("user_id") or "").strip()

        try:
            pricing = await self._load_latest_pricing(job_id)
            internal_child = self._is_internal_child_pricing(pricing) or self._is_internal_child_request_payload(payload_json)

            if (not internal_child) and self._pricing_required() and not self._pricing_enabled():
                reason = self._pricing_disabled_reason()
                await self.jobs.set_status(
                    job_id,
                    "failed",
                    error_code="PRICING_CLIENT_DISABLED",
                    error_message=reason,
                )
                return

            if self._pricing_enabled() and not internal_child:
                latest_pricing = await self._await_reserved_pricing(job_id)
                if latest_pricing:
                    pricing = latest_pricing

                pricing_state = str(pricing.get("state") or "").strip().lower()
                reservation_id = str(pricing.get("reservation_id") or "").strip()

                if pricing.get("enabled") and pricing_state == "reservation_failed":
                    await self.jobs.set_status(
                        job_id,
                        "failed",
                        error_code="PRICING_RESERVATION_FAILED",
                        error_message=str(pricing.get("error") or "Pricing reservation failed"),
                    )
                    return

                if pricing.get("enabled") and not reservation_id:
                    await self.jobs.set_status(
                        job_id,
                        "failed",
                        error_code="PRICING_NOT_RESERVED",
                        error_message="Pricing reservation did not complete before job execution",
                    )
                    return

            if provider_name == "heygen_av4":
                provider_timeout_s = max(
                    300,
                    int(os.getenv("DF_FUSION_HEYGEN_TIMEOUT_SECONDS", "600") or "600"),
                )
                poll_interval_s = max(
                    3,
                    int(os.getenv("DF_FUSION_PROVIDER_POLL_SECONDS", "5") or "5"),
                )
                retry_after_s = max(
                    30,
                    int(os.getenv("DF_FUSION_HEYGEN_RETRY_AFTER_SECONDS", "60") or "60"),
                )
                logger.info(
                    "fusion_provider_routing",
                    extra={
                        "job_id": job_id,
                        "provider": provider_name,
                        "provider_version": provider_version,
                        "provider_timeout_s": provider_timeout_s,
                        "poll_interval_s": poll_interval_s,
                    },
                )
                await self.steps.upsert_step(job_id, StepCode.provider_submit.value, "running", attempt=0)

                existing = self._coerce_dict(await self.runs.get_by_idempotency_key(provider_idem))
                existing_provider_job_id = str(existing.get("provider_job_id") or "").strip()
                if existing_provider_job_id:
                    provider_job_id = existing_provider_job_id
                    run_id = str(existing.get("id") or "") or None
                    submit_meta = self._coerce_dict(existing.get("meta_json"))
                    logger.info(
                        "reuse_provider_job_same_job_only",
                        extra={"job_id": job_id, "provider_job_id": provider_job_id, "idempotency_key": provider_idem},
                    )
                else:
                    request_json = {
                        "provider": provider_name,
                        "voice_mode": req.voice_mode.value,
                        "video": req.video.model_dump(),
                        "prompt_preview": self._extract_video_direction_prompt(payload_json)[:160] or None,
                    }
                    run_id = await self.runs.create_run(
                        job_id=job_id,
                        provider=provider_name,
                        idempotency_key=provider_idem,
                        request_json=request_json,
                    )
                    logger.info("fusion.heygen submit start job_id=%s provider_idem=%s", job_id, provider_idem)
                    heygen_submit = await self._run_heygen_direct_job(
                        job_id=job_id,
                        req=req,
                        provider_idem=provider_idem,
                        payload_json=payload_json,
                    )
                    provider_job_id = str(heygen_submit.get("provider_job_id") or "").strip()
                    submit_meta = self._coerce_dict(heygen_submit.get("submit_meta"))
                    raw_response = self._coerce_dict(heygen_submit.get("raw_response"))
                    if run_id:
                        await self.runs.mark_submitted(run_id, provider_job_id or provider_idem, raw_response)
                        await self.runs.update_status(
                            run_id,
                            "processing",
                            meta_json={
                                "raw": raw_response,
                                "provider_job_id": provider_job_id,
                                "provider": provider_name,
                                "submit_meta": submit_meta,
                            },
                        )

                    await self.steps.upsert_step(
                        job_id,
                        StepCode.provider_submit.value,
                        "succeeded",
                        attempt=0,
                        meta_json={
                            "provider": provider_name,
                            "provider_job_id": provider_job_id,
                            "idempotency_key": provider_idem,
                            "submit_meta": submit_meta,
                        },
                    )

                await self.jobs.set_status(job_id, "processing")
                await self._persist_light_status(job_id, provider=provider_name, provider_job_id=provider_job_id or provider_idem, provider_status="processing")

                performance_id = await self.perfs.upsert_performance(
                    user_id=str(job["user_id"]),
                    provider=provider_name,
                    provider_job_id=provider_job_id or provider_idem,
                    status="processing",
                    share_url=None,
                    meta_json={
                        "job_id": job_id,
                        "request_hash": req_hash,
                        "idempotency_key": provider_idem,
                        "voice_mode": req.voice_mode.value,
                        "provider_version": provider_version,
                        "prompt_preview": self._extract_video_direction_prompt(payload_json)[:160] or None,
                    },
                )
                await self.perfs.upsert_fusion_job_output(job_id, performance_id)
                await self.steps.upsert_step(
                    job_id,
                    StepCode.provider_poll.value,
                    "running",
                    attempt=0,
                    meta_json={"provider_job_id": provider_job_id, "provider": provider_name},
                )

                started = asyncio.get_running_loop().time()
                last_status = None
                poll_errors = 0
                while True:
                    elapsed = asyncio.get_running_loop().time() - started
                    if elapsed > provider_timeout_s:
                        logger.error("fusion.heygen poll timeout job_id=%s provider_job_id=%s elapsed_s=%s last_status=%s", job_id, provider_job_id, int(elapsed), last_status)
                        await self._mark_job_processing_for_retry(
                            job_id=job_id,
                            provider_name=provider_name,
                            provider_job_id=provider_job_id or provider_idem,
                            run_id=run_id,
                            performance_id=performance_id,
                            user_id=str(job["user_id"]),
                            reason="heygen_poll_timeout_pending_retry",
                            retry_after_s=retry_after_s,
                            meta_json={"elapsed_s": int(elapsed)},
                        )
                        return

                    try:
                        poll_data = await self._poll_heygen_direct_job(provider_job_id or provider_idem)
                        poll_errors = 0
                    except Exception as exc:
                        poll_errors += 1
                        if poll_errors >= 5:
                            await self._mark_job_processing_for_retry(
                                job_id=job_id,
                                provider_name=provider_name,
                                provider_job_id=provider_job_id or provider_idem,
                                run_id=run_id,
                                performance_id=performance_id,
                                user_id=str(job["user_id"]),
                                reason="heygen_poll_error_pending_retry",
                                retry_after_s=retry_after_s,
                                meta_json={"poll_errors": poll_errors, "last_error": str(exc)},
                            )
                            return
                        await asyncio.sleep(_adaptive_provider_poll_interval_seconds(elapsed_s=elapsed, provider_name=provider_name, base_interval_s=poll_interval_s))
                        continue

                    raw_status = str(poll_data.get("status") or "").strip()
                    normalized_status = _normalize_provider_status(raw_status)
                    video_url = str(poll_data.get("video_url") or "").strip()
                    if raw_status != last_status:
                        logger.info("fusion.heygen poll transition job_id=%s provider_job_id=%s raw_status=%s normalized_status=%s elapsed_s=%s has_video_url=%s", job_id, provider_job_id, raw_status, normalized_status, int(elapsed), bool(video_url))
                    last_poll = ProviderPollResult(
                        status=normalized_status if normalized_status != "unknown" else (raw_status or "processing"),
                        video_url=video_url or None,
                        raw_response=self._coerce_dict(poll_data.get("raw_response")),
                        error_message=self._string_or_none(poll_data.get("error_message")),
                    )

                    if run_id and raw_status != last_status:
                        last_status = raw_status
                        try:
                            await self.runs.update_status(
                                run_id,
                                normalized_status if normalized_status != "unknown" else (raw_status or "processing"),
                                meta_json={
                                    "raw": poll_data.get("raw_response"),
                                    "provider_job_id": provider_job_id,
                                    "raw_status": raw_status,
                                    "normalized_status": normalized_status,
                                    "provider": provider_name,
                                },
                            )
                        except Exception:
                            logger.warning(
                                "provider_run_status_update_failed",
                                extra={"job_id": job_id, "run_id": run_id},
                            )

                    if video_url:
                        await self.steps.upsert_step(job_id, StepCode.provider_poll.value, "succeeded", attempt=0)
                        break

                    if normalized_status == "processing":
                        await self._persist_light_status(job_id, provider=provider_name, provider_job_id=provider_job_id, provider_status=normalized_status)
                        await asyncio.sleep(_adaptive_provider_poll_interval_seconds(elapsed_s=elapsed, provider_name=provider_name, base_interval_s=poll_interval_s))
                        continue

                    if normalized_status == "failed":
                        raise RuntimeError(poll_data.get("error_message") or f"HeyGen failed with status={raw_status!r}")

                    if normalized_status == "canceled":
                        raise RuntimeError(f"HeyGen canceled with status={raw_status!r}")

                    if normalized_status == "succeeded":
                        raise RuntimeError("HeyGen returned succeeded but video_url is missing")

                    await self._persist_light_status(job_id, provider=provider_name, provider_job_id=provider_job_id, provider_status=normalized_status)
                    await asyncio.sleep(_adaptive_provider_poll_interval_seconds(elapsed_s=elapsed, provider_name=provider_name, base_interval_s=poll_interval_s))

                await self.steps.upsert_step(job_id, StepCode.finalize.value, "running", attempt=0)
                logger.info("fusion finalize start job_id=%s provider=%s provider_job_id=%s video_url=%s", job_id, provider_name, provider_job_id, _preview_url(last_poll.video_url if last_poll else None))
                final_video_url = await self.artifact_service.persist_video_artifact(
                    str(last_poll.video_url),
                    user_id=str(job["user_id"]),
                    job_id=job_id,
                    provider_job_id=provider_job_id or provider_idem,
                )
                await self.artifacts.add_artifact(
                    job_id,
                    "video",
                    final_video_url,
                    content_type="video/mp4",
                    meta_json={
                        "provider": provider_name,
                        "provider_job_id": provider_job_id or provider_idem,
                        "provider_meta": submit_meta,
                    },
                )
                await self._persist_light_status(job_id, provider=provider_name, provider_job_id=provider_job_id or provider_idem, provider_status="succeeded", primary_video_url=final_video_url)

                if performance_id:
                    try:
                        await self.perfs.mark_ready(
                            performance_id,
                            share_url=None,
                            meta_json={
                                "job_id": job_id,
                                "provider_job_id": provider_job_id or provider_idem,
                                "video_url": final_video_url,
                                "status": "ready",
                                "user_id": str(job["user_id"]),
                                "provider": provider_name,
                            },
                        )
                    except Exception:
                        logger.warning(
                            "perf_mark_ready_failed",
                            extra={"job_id": job_id, "performance_id": performance_id},
                        )

                pricing = await self._commit_pricing_for_job(
                    job_id=job_id,
                    user_id=user_id,
                    pricing=pricing,
                    actual_units=self._requested_billed_units(req),
                )

                await self.steps.upsert_step(job_id, StepCode.finalize.value, "succeeded", attempt=0)
                await self.jobs.set_status(job_id, "succeeded")
                return

            await self.steps.upsert_step(job_id, StepCode.provider_submit.value, "running", attempt=0)

            existing = self._coerce_dict(await self.runs.get_by_idempotency_key(provider_idem))
            existing_provider_job_id = str(existing.get("provider_job_id") or "").strip()

            if existing_provider_job_id:
                provider_job_id = existing_provider_job_id
                run_id = str(existing.get("id") or "")
                logger.info(
                    "reuse_provider_job_same_job_only",
                    extra={"job_id": job_id, "provider_job_id": provider_job_id, "idempotency_key": provider_idem},
                )
            else:
                resolved_face_url = await self._resolve_face_url(job_id, req)
                resolved_audio_url = None
                has_explicit_voice_audio = bool(
                    req.voice_audio and (
                        getattr(req.voice_audio, "audio_url", None)
                        or getattr(req.voice_audio, "audio_artifact_id", None)
                        or getattr(req.voice_audio, "audio_asset_id", None)
                    )
                )
                if req.voice_mode.value == "audio" and has_explicit_voice_audio:
                    resolved_audio_url = await self._resolve_audio_url(job_id, req)
                    await self.artifacts.add_artifact(
                        job_id,
                        "provider_audio_ref",
                        resolved_audio_url,
                        content_type="text/uri-list",
                        meta_json={"provider": provider_name},
                    )

                cached_prepared = await self._load_provider_prepare_cache(job_id, provider_name=provider_name)
                if cached_prepared is not None:
                    prepared = cached_prepared
                    logger.info("fusion provider prepare cache hit job_id=%s provider=%s request_keys=%s", job_id, provider_name, sorted((prepared.request_json or {}).keys()))
                else:
                    reference_image_urls = await self._resolve_reference_image_urls(job_id, payload_json)
                    logger.info("fusion provider inputs job_id=%s provider=%s face_url=%s has_audio=%s reference_image_count=%s", job_id, provider_name, _preview_url(resolved_face_url), bool(resolved_audio_url), len(reference_image_urls))

                    prepared = await provider.prepare(
                        ProviderPrepareInput(
                            job_id=job_id,
                            user_id=user_id,
                            request_payload=payload_json,
                            resolved_face_url=resolved_face_url,
                            resolved_audio_url=resolved_audio_url,
                            reference_image_urls=reference_image_urls,
                        )
                    )
                    await self._persist_provider_prepare_cache(
                        job_id,
                        provider_name=prepared.provider_name,
                        provider_version=prepared.provider_version,
                        request_json=prepared.request_json,
                        submit_meta=prepared.submit_meta,
                        resolved_face_url=resolved_face_url,
                        resolved_audio_url=resolved_audio_url,
                        reference_image_urls=reference_image_urls,
                    )

                logger.info("fusion provider prepared job_id=%s provider=%s prepared=%s", job_id, provider_name, self._prepared_log_meta(prepared))
                try:
                    await self.steps.upsert_step(
                        job_id,
                        StepCode.provider_submit.value,
                        "running",
                        attempt=0,
                        meta_json={
                            "provider": prepared.provider_name,
                            "provider_version": prepared.provider_version,
                            "idempotency_key": provider_idem,
                            "submit_meta": prepared.submit_meta,
                        },
                    )
                except Exception:
                    pass

                run_id = await self.runs.create_run(
                    job_id=job_id,
                    provider=provider_name,
                    idempotency_key=provider_idem,
                    request_json=prepared.request_json,
                )
                logger.info("fusion provider submit start job_id=%s provider=%s provider_idem=%s", job_id, provider_name, provider_idem)
                submit_res: ProviderSubmitResult = await provider.submit(prepared.request_json, provider_idem)
                provider_job_id = submit_res.provider_job_id
                logger.info("fusion provider submit ok job_id=%s provider=%s provider_job_id=%s raw_response_keys=%s", job_id, provider_name, provider_job_id, sorted(self._coerce_dict(submit_res.raw_response).keys()))
                if not provider_job_id:
                    raise FusionProviderError("provider_job_id missing after submit")
                await self.runs.mark_submitted(run_id, provider_job_id, submit_res.raw_response)

            if not provider_job_id:
                raise FusionProviderError("provider_job_id missing after submit/reuse")

            await self.steps.upsert_step(
                job_id,
                StepCode.provider_submit.value,
                "succeeded",
                attempt=0,
                meta_json={
                    "provider": provider_name,
                    "provider_job_id": provider_job_id,
                    "idempotency_key": provider_idem,
                    "submit_meta": prepared.submit_meta if prepared else {},
                },
            )

            performance_id = await self.perfs.upsert_performance(
                user_id=str(job["user_id"]),
                provider=provider_name,
                provider_job_id=provider_job_id,
                status="processing",
                share_url=None,
                meta_json={
                    "job_id": job_id,
                    "request_hash": req_hash,
                    "idempotency_key": provider_idem,
                    "voice_mode": req.voice_mode.value,
                    "provider_version": provider_version,
                },
            )
            await self.perfs.upsert_fusion_job_output(job_id, performance_id)

            await self.steps.upsert_step(
                job_id,
                StepCode.provider_poll.value,
                "running",
                attempt=0,
                meta_json={"provider_job_id": provider_job_id, "provider": provider_name},
            )

            started = asyncio.get_running_loop().time()
            last_status: Optional[str] = None
            poll_errors = 0
            provider_timeout_s = _provider_poll_timeout_seconds()
            poll_interval_s = _provider_poll_interval_seconds()

            while True:
                elapsed = asyncio.get_running_loop().time() - started
                if elapsed > provider_timeout_s:
                    logger.error("fusion provider poll timeout job_id=%s provider=%s provider_job_id=%s elapsed_s=%s last_status=%s last_poll=%s", job_id, provider_name, provider_job_id, int(elapsed), last_status, self._poll_log_meta(last_poll))
                    recovered = await self._attempt_provider_timeout_recovery(
                        provider=provider,
                        provider_name=provider_name,
                        provider_job_id=provider_job_id,
                        job_id=job_id,
                        attempts=3,
                        sleep_s=2.0,
                    )
                    recovered_video_url = str(getattr(recovered, "video_url", None) or "").strip() if recovered else ""
                    if recovered_video_url:
                        logger.info("fusion provider timeout recovery succeeded job_id=%s provider=%s provider_job_id=%s video_url=%s", job_id, provider_name, provider_job_id, _preview_url(recovered_video_url))
                        last_poll = recovered
                        break
                    raise FusionProviderError("Provider polling timed out after %ss provider=%s provider_job_id=%s last_status=%s" % (int(elapsed), provider_name, provider_job_id, last_status or "<unset>"))

                try:
                    poll = await provider.poll(provider_job_id)
                    poll_errors = 0
                except Exception as exc:
                    poll_errors += 1
                    logger.warning("fusion provider poll error job_id=%s provider=%s provider_job_id=%s poll_errors=%s error=%s", job_id, provider_name, provider_job_id, poll_errors, str(exc))
                    if poll_errors >= 5:
                        raise FusionProviderError("Provider poll failed repeatedly provider=%s provider_job_id=%s: %s" % (provider_name, provider_job_id, exc)) from exc
                    await asyncio.sleep(_adaptive_provider_poll_interval_seconds(elapsed_s=elapsed, provider_name=provider_name, base_interval_s=poll_interval_s))
                    continue

                last_poll = poll

                raw_status = str(getattr(poll, "status", None) or "").strip()
                normalized_status = _normalize_provider_status(raw_status)
                video_url = str(getattr(poll, "video_url", None) or "").strip()
                if raw_status != last_status:
                    logger.info("fusion provider poll transition job_id=%s provider=%s provider_job_id=%s raw_status=%s normalized_status=%s elapsed_s=%s has_video_url=%s error_message=%s", job_id, provider_name, provider_job_id, raw_status, normalized_status, int(elapsed), bool(video_url), getattr(poll, "error_message", None))
                    await self._persist_light_status(job_id, provider=provider_name, provider_job_id=provider_job_id, provider_status=normalized_status or raw_status, error_message=getattr(poll, "error_message", None))

                if run_id and raw_status != last_status:
                    last_status = raw_status
                    try:
                        await self.runs.update_status(
                            run_id,
                            normalized_status if normalized_status != "unknown" else (raw_status or "processing"),
                            meta_json={
                                "raw": poll.raw_response,
                                "provider_job_id": provider_job_id,
                                "raw_status": raw_status,
                                "normalized_status": normalized_status,
                                "provider": provider_name,
                            },
                        )
                    except Exception:
                        logger.warning(
                            "provider_run_status_update_failed",
                            extra={"job_id": job_id, "run_id": run_id},
                        )

                if video_url:
                    if run_id:
                        await self.runs.update_status(
                            run_id,
                            "succeeded",
                            meta_json={
                                "raw": poll.raw_response,
                                "provider_job_id": provider_job_id,
                                "raw_status": raw_status,
                                "normalized_status": normalized_status,
                                "provider": provider_name,
                            },
                        )
                    await self.steps.upsert_step(job_id, StepCode.provider_poll.value, "succeeded", attempt=0)
                    break

                if normalized_status == "processing":
                    await asyncio.sleep(_adaptive_provider_poll_interval_seconds(elapsed_s=elapsed, provider_name=provider_name, base_interval_s=poll_interval_s))
                    continue

                if normalized_status == "failed":
                    if run_id:
                        await self.runs.update_status(
                            run_id,
                            "failed",
                            meta_json={
                                "error": poll.error_message or "Provider failed",
                                "raw": poll.raw_response,
                                "provider_job_id": provider_job_id,
                                "raw_status": raw_status,
                                "normalized_status": normalized_status,
                                "provider": provider_name,
                            },
                        )
                    raise FusionProviderError(poll.error_message or f"Provider failed with status={raw_status!r}")

                if normalized_status == "canceled":
                    if run_id:
                        await self.runs.update_status(
                            run_id,
                            "failed",
                            meta_json={
                                "error": "Provider canceled",
                                "raw": poll.raw_response,
                                "provider_job_id": provider_job_id,
                                "raw_status": raw_status,
                                "normalized_status": normalized_status,
                                "provider": provider_name,
                            },
                        )
                    raise FusionProviderError(f"Provider canceled with status={raw_status!r}")

                if normalized_status == "succeeded":
                    if run_id:
                        await self.runs.update_status(
                            run_id,
                            "failed",
                            meta_json={
                                "error": "Provider success but missing video_url",
                                "raw": poll.raw_response,
                                "provider_job_id": provider_job_id,
                                "raw_status": raw_status,
                                "normalized_status": normalized_status,
                                "provider": provider_name,
                            },
                        )
                    raise FusionProviderError("Provider returned succeeded but video_url is missing")

                await self._persist_light_status(job_id, provider=provider_name, provider_job_id=provider_job_id, provider_status=normalized_status)
                await asyncio.sleep(_adaptive_provider_poll_interval_seconds(elapsed_s=elapsed, provider_name=provider_name, base_interval_s=poll_interval_s))

            await self.steps.upsert_step(job_id, StepCode.finalize.value, "running", attempt=0)

            final_video_url = await self.artifact_service.persist_video_artifact(
                last_poll.video_url,
                user_id=str(job["user_id"]),
                job_id=job_id,
                provider_job_id=provider_job_id,
            )

            await self.artifacts.add_artifact(
                job_id,
                "video",
                final_video_url,
                content_type="video/mp4",
                meta_json={
                    "provider": provider_name,
                    "provider_job_id": provider_job_id,
                    "provider_meta": prepared.submit_meta if prepared else {},
                },
            )
            await self._persist_light_status(job_id, provider=provider_name, provider_job_id=provider_job_id, provider_status="succeeded", primary_video_url=final_video_url)

            share_url_val: Optional[str] = getattr(last_poll, "share_url", None) or None
            if share_url_val:
                await self.artifacts.add_artifact(
                    job_id,
                    "share_url",
                    share_url_val,
                    content_type="text/uri-list",
                    meta_json={"provider": provider_name, "provider_job_id": provider_job_id},
                )

            if performance_id:
                try:
                    await self.perfs.mark_ready(
                        performance_id,
                        share_url=share_url_val,
                        meta_json={
                            "job_id": job_id,
                            "provider_job_id": provider_job_id,
                            "video_url": final_video_url,
                            "status": "ready",
                            "user_id": str(job["user_id"]),
                            "provider": provider_name,
                        },
                    )
                except Exception:
                    logger.warning(
                        "perf_mark_ready_failed",
                        extra={"job_id": job_id, "performance_id": performance_id},
                    )

            pricing = await self._commit_pricing_for_job(
                job_id=job_id,
                user_id=user_id,
                pricing=pricing,
                actual_units=self._requested_billed_units(req),
            )

            await self.steps.upsert_step(job_id, StepCode.finalize.value, "succeeded", attempt=0)
            await self.jobs.set_status(job_id, "succeeded")
            logger.info("fusion.run_job succeeded job_id=%s provider=%s provider_job_id=%s final_video_url=%s", job_id, provider_name, provider_job_id, _preview_url(final_video_url))
            return

        except Exception as e:
            msg = str(e)
            code = _classify_error(e)
            logger.exception(
                "fusion_job_failed",
                extra={
                    "job_id": job_id,
                    "error_code": code,
                    "error": msg,
                    "provider_job_id": provider_job_id,
                    "run_id": run_id,
                    "performance_id": performance_id,
                    "provider": provider_name,
                },
            )

            try:
                if not self._is_internal_child_pricing(pricing):
                    pricing = await self._release_pricing_for_job(
                        job_id=job_id,
                        user_id=user_id,
                        pricing=pricing,
                        reason=code.lower(),
                    )
            except Exception:
                logger.exception("fusion_pricing_release_in_except_failed", extra={"job_id": job_id})

            try:
                if run_id:
                    await self.runs.update_status(
                        run_id,
                        "failed",
                        meta_json={
                            "error_code": code,
                            "error": msg,
                            "provider_job_id": provider_job_id,
                            "user_id": str(job["user_id"]),
                            "provider": provider_name,
                        },
                    )
            except Exception:
                pass

            try:
                await self._persist_light_status(
                    job_id,
                    provider=provider_name,
                    provider_job_id=provider_job_id,
                    provider_status=("provider_degraded_retrying" if code == "PROVIDER_DEGRADED" else "failed"),
                    error_message=msg,
                )
            except Exception:
                logger.warning("fusion_light_status_update_failed_on_error", extra={"job_id": job_id})

            await self.jobs.set_status(job_id, "failed", error_code=code, error_message=msg)

            try:
                if not performance_id and provider_job_id:
                    performance_id = await self.perfs.upsert_performance(
                        user_id=str(job["user_id"]),
                        provider=provider_name,
                        provider_job_id=provider_job_id,
                        status="failed",
                        share_url=None,
                        meta_json={
                            "job_id": job_id,
                            "request_hash": req_hash,
                            "idempotency_key": provider_idem,
                            "user_id": str(job["user_id"]),
                            "provider": provider_name,
                        },
                    )
                    await self.perfs.upsert_fusion_job_output(job_id, performance_id)

                if performance_id:
                    await self.perfs.mark_failed(
                        performance_id,
                        error_code=code,
                        error_message=msg,
                        meta_json={"job_id": job_id, "user_id": str(job["user_id"]), "provider": provider_name},
                    )
            except Exception:
                logger.warning(
                    "perf_mark_failed_failed",
                    extra={"job_id": job_id, "performance_id": performance_id},
                )

            try:
                await self.steps.fail_step(
                    job_id,
                    StepCode.provider_poll.value,
                    attempt=0,
                    error_code=code,
                    error_message=msg,
                )
            except Exception:
                pass
            try:
                await self.steps.fail_step(
                    job_id,
                    StepCode.finalize.value,
                    attempt=0,
                    error_code=code,
                    error_message=msg,
                )
            except Exception:
                pass
            return
