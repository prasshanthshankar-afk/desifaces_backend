from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional
from types import SimpleNamespace
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import RequireFusionEnabled, get_current_user_id
from app.db import get_pool
from app.domain.models import ArtifactView, FusionJobCreate, StepView
from app.domain.validators import validate_fusion_request
from app.services.fusion_orchestrator import FusionOrchestrator, PricingClientError
from app.services.artifact_service import ArtifactService
from app.repos.fusion_jobs_repo import FusionJobsRepo
from app.repos.steps_repo import StepsRepo
from app.repos.artifacts_repo import ArtifactsRepo
try:
    from desifaces_shared.pricing.orchestration import (
        PricingPreviewSpec,
        build_preview_request,
        make_preview_artifact,
    )
except Exception:
    from dataclasses import dataclass

    @dataclass
    class PricingPreviewRequest:
        user_id: str
        service_name: str
        service_action: str
        sku_code: str
        units: str
        idempotency_key: str
        meta: Dict[str, Any]

    @dataclass
    class PricingPreviewSpec:
        user_id: str
        service_name: str
        service_action: str
        sku_code: str
        units: str
        idempotency_key: str
        meta: Dict[str, Any]

    def build_preview_request(spec: PricingPreviewSpec) -> PricingPreviewRequest:
        return PricingPreviewRequest(
            user_id=spec.user_id,
            service_name=spec.service_name,
            service_action=spec.service_action,
            sku_code=spec.sku_code,
            units=spec.units,
            idempotency_key=spec.idempotency_key,
            meta=spec.meta,
        )

    def make_preview_artifact(
        resp: Any,
        *,
        service_name: str,
        service_action: str,
        sku_code: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        pricing = {
            "enabled": True,
            "state": "previewed",
            "service_name": service_name,
            "service_action": service_action,
            "variant_code": sku_code,
            "sku_code": sku_code,
            "estimated_units": getattr(resp, "units", None),
            "unit_type": getattr(resp, "unit_type", None),
            "amount": getattr(resp, "amount", None),
            "currency": getattr(resp, "currency", None),
            "quote_id": getattr(resp, "quote_id", None),
            "preview_fingerprint": getattr(resp, "preview_fingerprint", None),
            "meta": dict(meta or {}),
        }
        pricing_summary = {
            "state": "previewed",
            "estimated_units": pricing.get("estimated_units"),
            "unit_type": pricing.get("unit_type"),
            "amount": pricing.get("amount"),
            "currency": pricing.get("currency"),
        }
        return {"pricing": pricing, "pricing_summary": pricing_summary}

logger = logging.getLogger("fusion_jobs")

router = APIRouter()


class FusionPricingPreviewResponse(BaseModel):
    quote_id: Optional[str] = None
    preview_fingerprint: Optional[str] = None
    pricing: Dict[str, Any] = Field(default_factory=dict)
    pricing_summary: Dict[str, Any] = Field(default_factory=dict)
    message: Optional[str] = None


class FusionJobApiView(BaseModel):
    job_id: str
    status: str
    provider: Optional[str] = None
    provider_job_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    pricing: Optional[Dict[str, Any]] = None
    pricing_summary: Optional[Dict[str, Any]] = None
    steps: List[StepView] = Field(default_factory=list)
    artifacts: List[ArtifactView] = Field(default_factory=list)


class FusionJobLightView(BaseModel):
    job_id: str
    status: str
    provider: Optional[str] = None
    provider_job_id: Optional[str] = None
    provider_status: Optional[str] = None
    primary_video_url: Optional[str] = None
    share_url: Optional[str] = None
    progress_pct: Optional[float] = None
    updated_at: Optional[datetime] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    pricing: Optional[Dict[str, Any]] = None
    pricing_summary: Optional[Dict[str, Any]] = None


class FusionRecoverySweepResponse(BaseModel):
    recovered: int = 0
    attempted_job_ids: List[str] = Field(default_factory=list)


_SAS_KINDS = {
    "audio",
    "image",
    "face",
    "face_image",
    "video",
    "resolved_face_sas_url",
    "resolved_audio_sas_url",
}


def _is_azure_blob_url(url: str) -> bool:
    s = (url or "").strip()
    if not s:
        return False
    try:
        p = urlparse(s)
        host = (p.netloc or "").lower()
        return host.endswith(".blob.core.windows.net")
    except Exception:
        return False


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




def _truthy_marker(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    s = str(value).strip().lower()
    return s in {"1", "true", "yes", "y", "on", "internal", "suppressed"}


def _nested_dicts_for_pricing_detection(req_dict: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect all request dicts where suppression markers may appear.

    svc-fusion-extension intentionally duplicates child-job flags across top-level,
    tags, provider_options, billing_context, and pricing because svc-fusion schemas
    have changed over time.
    """
    out: List[Dict[str, Any]] = []

    def add_dict(d: Any) -> None:
        if isinstance(d, dict):
            out.append(d)

    add_dict(req_dict)
    for key in (
        "tags",
        "provider_options",
        "pricing",
        "pricing_context",
        "billing_context",
        "billing",
        "meta",
        "metadata",
        "video",
    ):
        value = req_dict.get(key)
        add_dict(value)
        if isinstance(value, dict):
            add_dict(value.get("pricing"))
            add_dict(value.get("pricing_context"))
            add_dict(value.get("billing_context"))
            add_dict(value.get("billing"))

    return out


def _is_internal_child_pricing_suppressed(req_dict: Dict[str, Any]) -> bool:
    if not isinstance(req_dict, dict):
        return False

    markers = (
        "pricing_suppressed",
        "suppress_pricing",
        "skip_pricing",
        "disable_pricing",
        "internal_job",
        "child_job",
        "child_job_of_billable_longform_parent",
        "bill_to_parent",
        "is_internal_child",
    )
    for d in _nested_dicts_for_pricing_detection(req_dict):
        for key in markers:
            if _truthy_marker(d.get(key)):
                return True

        pricing = _coerce_dict(d.get("pricing"))
        if pricing:
            if pricing.get("enabled") is False:
                return True
            if _truthy_marker(pricing.get("suppressed")) or _truthy_marker(pricing.get("pricing_suppressed")):
                return True
            if str(pricing.get("state") or "").strip().lower() in {"suppressed", "internal", "disabled"}:
                return True

        mode = str(d.get("mode") or d.get("billing_mode") or d.get("settlement_mode") or "").strip().lower()
        if mode in {"internal", "internal_child"}:
            return True

    return False


def _extract_parent_billing_context(req_dict: Dict[str, Any]) -> Dict[str, Any]:
    for d in _nested_dicts_for_pricing_detection(req_dict):
        ctx = _coerce_dict(d.get("billing_context")) or _coerce_dict(d.get("pricing_context"))
        if ctx:
            return ctx
    return {}


def _suppressed_pricing_payload(req_dict: Dict[str, Any]) -> Dict[str, Any]:
    ctx = _extract_parent_billing_context(req_dict)
    tags = _coerce_dict(req_dict.get("tags"))
    provider_options = _coerce_dict(req_dict.get("provider_options"))

    parent_job_id = (
        ctx.get("parent_longform_job_id")
        or ctx.get("billing_parent_job_id")
        or tags.get("parent_longform_job_id")
        or provider_options.get("parent_longform_job_id")
        or req_dict.get("parent_longform_job_id")
    )
    segment_id = ctx.get("segment_id") or tags.get("segment_id") or provider_options.get("segment_id")

    return {
        "enabled": False,
        "state": "suppressed",
        "suppressed": True,
        "pricing_suppressed": True,
        "suppress_pricing": True,
        "billing_mode": "internal",
        "settlement_mode": "internal",
        "service_name": "svc-fusion",
        "service_action": "fusion.video.generate.internal_child",
        "variant_code": "FUSION_INTERNAL_CHILD",
        "sku_code": "FUSION_INTERNAL_CHILD",
        "estimated_units": "0",
        "actual_units": None,
        "billed_units": "0",
        "amount": "0.00",
        "final_amount": "0.00",
        "currency": None,
        "quote_id": None,
        "reservation_id": None,
        "ledger_entry_id": None,
        "parent_service": ctx.get("parent_service") or "svc-fusion-extension",
        "parent_job_id": str(parent_job_id) if parent_job_id else None,
        "parent_longform_job_id": str(parent_job_id) if parent_job_id else None,
        "billing_parent_job_id": str(parent_job_id) if parent_job_id else None,
        "segment_id": str(segment_id) if segment_id else None,
        "reason": ctx.get("reason") or "child_job_of_billable_longform_parent",
        "summary": {
            "display_note": "Internal child render; billing is handled by svc-fusion-extension parent.",
            "display_estimate": "0 credits",
            "display_final": "0 credits",
            "display_delta": "0 credits",
        },
    }


def _pricing_suppressed_summary(pricing: Dict[str, Any]) -> Dict[str, Any]:
    return dict(_coerce_dict(pricing.get("summary")) or {
        "display_note": "Internal child render; billing is handled by svc-fusion-extension parent.",
        "display_estimate": "0 credits",
        "display_final": "0 credits",
        "display_delta": "0 credits",
    })


def _stamp_internal_child_pricing_suppression(req_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve/propagate suppression flags before Pydantic/orchestrator handling."""
    out = dict(req_dict or {})
    pricing = {**_coerce_dict(out.get("pricing")), **_suppressed_pricing_payload(out)}

    ctx = _extract_parent_billing_context(out)
    if not ctx:
        ctx = {
            "parent_service": "svc-fusion-extension",
            "reason": "child_job_of_billable_longform_parent",
        }
    ctx.update(
        {
            "pricing_suppressed": True,
            "suppress_pricing": True,
            "internal_job": True,
            "child_job": True,
            "bill_to_parent": True,
        }
    )

    for key in ("tags", "provider_options", "meta", "metadata"):
        d = _coerce_dict(out.get(key))
        d.update(
            {
                "pricing_suppressed": True,
                "suppress_pricing": True,
                "skip_pricing": True,
                "disable_pricing": True,
                "pricing_enabled": False,
                "internal_job": True,
                "child_job": True,
                "is_internal_child": True,
                "child_job_of_billable_longform_parent": True,
                "bill_to_parent": True,
                "parent_service": ctx.get("parent_service") or "svc-fusion-extension",
                "pricing": pricing,
                "pricing_context": ctx,
                "billing_context": ctx,
            }
        )
        out[key] = d

    out.update(
        {
            "pricing_suppressed": True,
            "suppress_pricing": True,
            "skip_pricing": True,
            "disable_pricing": True,
            "pricing_enabled": False,
            "internal_job": True,
            "child_job": True,
            "is_internal_child": True,
            "child_job_of_billable_longform_parent": True,
            "bill_to_parent": True,
            "pricing": pricing,
            "pricing_context": ctx,
            "billing_context": ctx,
        }
    )
    return out


def _validate_fusion_job_create(payload: Dict[str, Any]) -> FusionJobCreate:
    if hasattr(FusionJobCreate, "model_validate"):
        return FusionJobCreate.model_validate(payload)
    return FusionJobCreate.parse_obj(payload)


class _InternalChildPricingClient:
    """No-op pricing client for internal child render jobs.

    It is intentionally enabled=True so older orchestrator code that checks
    `pricing_client.enabled` can proceed, but every pricing operation returns a
    suppressed/no-charge artifact and never calls svc-pricing.
    """

    enabled = True

    def __init__(self, req_dict: Dict[str, Any]):
        self.req_dict = req_dict
        self.pricing = _suppressed_pricing_payload(req_dict)
        self.pricing_summary = _pricing_suppressed_summary(self.pricing)

    def _response(self) -> SimpleNamespace:
        return SimpleNamespace(
            **self.pricing,
            pricing=self.pricing,
            pricing_summary=self.pricing_summary,
            quote_id=None,
            preview_fingerprint=None,
            reservation_id=None,
            units="0",
            unit_type="internal",
            amount="0.00",
            currency=None,
            message=self.pricing_summary.get("display_note"),
        )

    async def preview(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        return self._response()

    async def reserve(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        return self._response()

    async def commit(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        return self._response()

    async def release(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        return self._response()


def _install_internal_child_pricing_suppression(orch: FusionOrchestrator, req_dict: Dict[str, Any]) -> None:
    """Best-effort guard for older FusionOrchestrator pricing paths."""
    suppressed = _suppressed_pricing_payload(req_dict)
    summary = _pricing_suppressed_summary(suppressed)

    try:
        setattr(orch, "pricing_client", _InternalChildPricingClient(req_dict))
    except Exception:
        logger.debug("unable_to_replace_pricing_client_for_internal_child", exc_info=True)

    # These attributes are harmless when absent and useful if the orchestrator
    # already has suppression checks in newer builds.
    for name, value in (
        ("pricing_suppressed", True),
        ("suppress_pricing", True),
        ("skip_pricing", True),
        ("internal_child_job", True),
        ("_pricing_suppressed", True),
    ):
        try:
            setattr(orch, name, value)
        except Exception:
            pass

    async def _noop_pricing(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"pricing": suppressed, "pricing_summary": summary}

    for method_name in (
        "_reserve_pricing_for_job",
        "reserve_pricing_for_job",
        "_reserve_pricing",
        "_create_pricing_reservation",
        "_commit_pricing_for_job",
        "commit_pricing_for_job",
        "_release_pricing_for_job",
        "release_pricing_for_job",
    ):
        if hasattr(orch, method_name):
            try:
                setattr(orch, method_name, _noop_pricing)
            except Exception:
                pass

def _extract_pricing_view(job: Dict[str, Any]) -> dict | None:
    payload = _coerce_dict(job.get("payload_json"))
    meta = _coerce_dict(job.get("meta_json"))

    pricing = _coerce_dict(payload.get("pricing"))
    if pricing:
        return pricing

    pricing = _coerce_dict(meta.get("pricing"))
    if pricing:
        return pricing

    return None


def _extract_pricing_summary_view(job: Dict[str, Any]) -> dict | None:
    payload = _coerce_dict(job.get("payload_json"))
    meta = _coerce_dict(job.get("meta_json"))

    summary = _coerce_dict(payload.get("pricing_summary"))
    if summary:
        return summary

    summary = _coerce_dict(meta.get("pricing_summary"))
    if summary:
        return summary

    return None


def _extract_provider_view(job: Dict[str, Any]) -> Optional[str]:
    payload = _coerce_dict(job.get("payload_json"))
    meta = _coerce_dict(job.get("meta_json"))
    pricing = _coerce_dict(payload.get("pricing")) or _coerce_dict(meta.get("pricing"))

    provider = (
        payload.get("provider")
        or meta.get("provider")
        or pricing.get("provider")
        or _coerce_dict(meta.get("provider_submit_meta")).get("provider_name")
        or _coerce_dict(meta.get("provider_submit_meta")).get("provider_model_name")
    )
    provider_s = str(provider or "").strip()
    return provider_s or None


def _extract_light_status(job: Dict[str, Any]) -> Dict[str, Any]:
    meta = _coerce_dict(job.get("meta_json"))
    light = _coerce_dict(meta.get("light_status"))
    if light:
        return light
    return {
        "provider": meta.get("provider"),
        "provider_job_id": meta.get("provider_job_id"),
        "provider_status": meta.get("provider_status"),
        "primary_video_url": meta.get("primary_video_url"),
        "share_url": meta.get("share_url"),
        "progress_pct": meta.get("progress_pct"),
        "error_message": meta.get("error_message"),
    }


async def _build_job_light_view(pool, job: Dict[str, Any]) -> FusionJobLightView:
    light = _extract_light_status(job)
    provider = _extract_provider_view(job) or str(light.get("provider") or "").strip() or None
    provider_job_id = str(light.get("provider_job_id") or "").strip() or None
    provider_status = str(light.get("provider_status") or "").strip() or None
    primary_video_url = str(light.get("primary_video_url") or "").strip() or None
    share_url = str(light.get("share_url") or "").strip() or None
    progress_pct = light.get("progress_pct")
    try:
        progress_pct = float(progress_pct) if progress_pct is not None else None
    except Exception:
        progress_pct = None

    return FusionJobLightView(
        job_id=str(job["id"]),
        status=str(job["status"]),
        provider=provider,
        provider_job_id=provider_job_id,
        provider_status=provider_status,
        primary_video_url=primary_video_url,
        share_url=share_url,
        progress_pct=progress_pct,
        updated_at=job.get("updated_at"),
        error_code=job.get("error_code"),
        error_message=job.get("error_message") or light.get("error_message"),
        pricing=_extract_pricing_view(job),
        pricing_summary=_extract_pricing_summary_view(job),
    )


def _raise_http_for_pricing_error(exc: Exception) -> None:
    msg = str(exc or "")

    if "PRICING_CLIENT_DISABLED" in msg or "pricing client unavailable" in msg.lower():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PRICING_CLIENT_DISABLED",
        )

    if "PRICING_UNKNOWN_OR_INACTIVE_VARIANT" in msg:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="PRICING_UNKNOWN_OR_INACTIVE_VARIANT",
        )

    if "PRICING_VARIANT_ZERO_QTY_LINES" in msg:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="PRICING_VARIANT_ZERO_QTY_LINES",
        )

    if "PRICING_VARIANT_HAS_NO_LINES" in msg:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="PRICING_VARIANT_HAS_NO_LINES",
        )

    if "PRICING_INSUFFICIENT_CREDITS" in msg:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="PRICING_INSUFFICIENT_CREDITS",
        )

    raise HTTPException(
        status_code=status.HTTP_424_FAILED_DEPENDENCY,
        detail="PRICING_RESERVATION_FAILED",
    )


def _req_to_dict(req: FusionJobCreate) -> Dict[str, Any]:
    if hasattr(req, "model_dump"):
        return req.model_dump(exclude_none=True)
    if hasattr(req, "dict"):
        return req.dict(exclude_none=True)
    return {}


def _extract_duration_seconds(req_dict: Dict[str, Any]) -> float:
    video = _coerce_dict(req_dict.get("video"))
    provider_options = _coerce_dict(req_dict.get("provider_options"))
    candidates = [
        req_dict.get("duration_sec"),
        req_dict.get("planned_duration_sec"),
        req_dict.get("audio_duration_sec"),
        video.get("duration_sec"),
        video.get("duration_ms"),
        provider_options.get("duration_sec"),
        provider_options.get("duration_ms"),
    ]
    for c in candidates:
        try:
            if c is None:
                continue
            v = float(c)
            if v > 1000:
                v = v / 1000.0
            if v > 0:
                return v
        except Exception:
            pass
    return 60.0


def _estimated_fusion_minutes(req_dict: Dict[str, Any]) -> str:
    sec = max(1.0, _extract_duration_seconds(req_dict))
    return str(max(1, int(math.ceil(sec / 60.0))))


def _preview_meta(req_dict: Dict[str, Any], orch: FusionOrchestrator) -> Dict[str, Any]:
    estimated_minutes = _estimated_fusion_minutes(req_dict)
    voice_audio = _coerce_dict(req_dict.get("voice_audio"))
    voice_tts = _coerce_dict(req_dict.get("voice_tts"))
    video = _coerce_dict(req_dict.get("video"))
    provider_options = _coerce_dict(req_dict.get("provider_options"))
    reference_image_urls = list(req_dict.get("reference_image_urls") or [])
    reference_image_artifact_ids = list(req_dict.get("reference_image_artifact_ids") or [])

    provider = str(req_dict.get("provider") or orch.default_provider_name or "omnihuman_v15").strip() or "omnihuman_v15"
    aspect_ratio = str(video.get("aspect_ratio") or provider_options.get("aspect_ratio") or "9:16")
    resolution = str(video.get("resolution") or provider_options.get("resolution") or "720p")

    return {
        "mode": "fusion",
        "provider": provider,
        "voice_mode": req_dict.get("voice_mode"),
        "planned_duration_sec": str(int(_extract_duration_seconds(req_dict))),
        "minutes": estimated_minutes,
        "requested_units": estimated_minutes,
        "unit_type": "minute",
        "variant_code": orch._fusion_pricing_variant_code(),
        "leaf_sku_code": orch._fusion_pricing_leaf_sku_code(),
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "delivery_surface": video.get("delivery_surface"),
        "shot_type": video.get("shot_type") or provider_options.get("shot_type"),
        "has_face_artifact_id": bool(req_dict.get("face_artifact_id")),
        "has_audio_artifact_id": bool(voice_audio.get("audio_artifact_id")),
        "has_tts": bool(voice_tts.get("script") and voice_tts.get("voice_id")),
        "reference_image_count": len(reference_image_urls) + len(reference_image_artifact_ids),
        "provider_model_name": provider_options.get("model_name"),
    }


async def _build_job_api_view(pool, job: Dict[str, Any]) -> FusionJobApiView:
    job_id = str(job["id"])
    steps_repo = StepsRepo(pool)
    artifacts_repo = ArtifactsRepo(pool)
    artifact_svc = ArtifactService()

    step_rows = await steps_repo.list_steps(job_id)
    artifact_rows = await artifacts_repo.list_artifacts(job_id)

    provider_job_id = None
    try:
        async with pool.acquire() as conn:
            provider_job_id = await conn.fetchval(
                """
                SELECT provider_job_id::text
                FROM provider_runs
                WHERE job_id = $1::uuid
                  AND provider_job_id IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                job_id,
            )
    except Exception as e:
        logger.debug("provider_job_id_lookup_failed job_id=%s err=%s", job_id, str(e))

    if not provider_job_id:
        for step in step_rows:
            meta = step.get("meta_json")
            if isinstance(meta, dict):
                provider_job_id = meta.get("provider_job_id")
                if provider_job_id:
                    break

    resolved_artifacts: list[ArtifactView] = []
    for a in artifact_rows:
        kind = str(a.get("kind") or "")
        url = str(a.get("url") or "")
        content_type = a.get("content_type")

        try:
            if kind in _SAS_KINDS and _is_azure_blob_url(url):
                url = await artifact_svc.mint_read_sas_for_artifact(dict(a), ttl_hours=2)
        except Exception as e:
            logger.debug(
                "sas_mint_failed job_id=%s kind=%s url=%s err=%s",
                job_id,
                kind,
                url[:120],
                str(e),
            )

        resolved_artifacts.append(ArtifactView(kind=kind, url=url, content_type=content_type))

    return FusionJobApiView(
        job_id=job_id,
        status=str(job["status"]),
        provider=_extract_provider_view(job),
        provider_job_id=provider_job_id,
        error_code=job.get("error_code"),
        error_message=job.get("error_message"),
        pricing=_extract_pricing_view(job),
        pricing_summary=_extract_pricing_summary_view(job),
        steps=[
            StepView(
                step_code=str(r["step_code"]),
                status=str(r["status"]),
                attempt=int(r.get("attempt") or 0),
                error_code=r.get("error_code"),
                error_message=r.get("error_message"),
            )
            for r in step_rows
        ],
        artifacts=resolved_artifacts,
    )


@router.post("/jobs/pricing/preview", dependencies=[RequireFusionEnabled], response_model=FusionPricingPreviewResponse)
async def preview_job_pricing(
    raw_req: Dict[str, Any] = Body(...),
    user_id: str = Depends(get_current_user_id),
) -> FusionPricingPreviewResponse:
    raw_req = dict(raw_req or {})
    is_internal_child = _is_internal_child_pricing_suppressed(raw_req)
    req_dict = _stamp_internal_child_pricing_suppression(raw_req) if is_internal_child else raw_req
    req = _validate_fusion_job_create(req_dict)
    validate_fusion_request(req)

    if is_internal_child:
        pricing = _suppressed_pricing_payload(req_dict)
        summary = _pricing_suppressed_summary(pricing)
        logger.info(
            "fusion.preview_pricing_suppressed user_id=%s parent_job_id=%s segment_id=%s",
            user_id,
            pricing.get("parent_longform_job_id"),
            pricing.get("segment_id"),
        )
        return FusionPricingPreviewResponse(
            quote_id=None,
            preview_fingerprint=None,
            pricing=pricing,
            pricing_summary=summary,
            message=summary.get("display_note"),
        )

    pool = await get_pool()
    orch = FusionOrchestrator(pool)

    try:
        pricing_client = orch.pricing_client
    except Exception as e:
        _raise_http_for_pricing_error(PricingClientError(str(e)))
        raise

    if not bool(getattr(pricing_client, "enabled", False)):
        _raise_http_for_pricing_error(PricingClientError("PRICING_CLIENT_DISABLED"))

    req_dict = _req_to_dict(req)
    meta = _preview_meta(req_dict, orch)
    estimated_units = _estimated_fusion_minutes(req_dict)

    request_hash_src = {
        "studio_type": "fusion",
        "user_id": str(user_id),
        "payload": req_dict,
    }
    request_fingerprint = hashlib.sha256(
        json.dumps(
            request_hash_src,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()

    try:
        spec = PricingPreviewSpec(
            user_id=str(user_id),
            service_name="svc-fusion",
            service_action="fusion.video.generate",
            sku_code=orch._fusion_pricing_variant_code(),
            units=estimated_units,
            idempotency_key=f"svc-fusion:preview:{user_id}:{request_fingerprint}",
            meta=meta,
        )
        resp = await pricing_client.preview(build_preview_request(spec))
        artifact = make_preview_artifact(
            resp,
            service_name="svc-fusion",
            service_action="fusion.video.generate",
            sku_code=orch._fusion_pricing_variant_code(),
            meta=meta,
        )
    except PricingClientError as e:
        _raise_http_for_pricing_error(e)
        raise
    except Exception as e:
        _raise_http_for_pricing_error(PricingClientError(str(e)))
        raise

    pricing = dict(artifact.get("pricing") or {})
    summary = dict(artifact.get("pricing_summary") or {})
    return FusionPricingPreviewResponse(
        quote_id=str(pricing.get("quote_id") or "") or None,
        preview_fingerprint=str(pricing.get("preview_fingerprint") or "") or None,
        pricing=pricing,
        pricing_summary=summary,
        message=str(getattr(resp, "message", "") or "") or None,
    )

@router.post("/jobs", dependencies=[RequireFusionEnabled], response_model=FusionJobApiView)
async def create_job(
    raw_req: Dict[str, Any] = Body(...),
    user_id: str = Depends(get_current_user_id),
) -> FusionJobApiView:
    raw_req = dict(raw_req or {})
    is_internal_child = _is_internal_child_pricing_suppressed(raw_req)
    req_dict = _stamp_internal_child_pricing_suppression(raw_req) if is_internal_child else raw_req
    req = _validate_fusion_job_create(req_dict)
    validate_fusion_request(req)

    pool = await get_pool()
    orch = FusionOrchestrator(pool)
    jobs = FusionJobsRepo(pool)

    if is_internal_child:
        _install_internal_child_pricing_suppression(orch, req_dict)
        suppressed_pricing = _suppressed_pricing_payload(req_dict)
        logger.info(
            "fusion.create_job_pricing_suppressed user_id=%s parent_job_id=%s segment_id=%s provider=%s",
            user_id,
            suppressed_pricing.get("parent_longform_job_id"),
            suppressed_pricing.get("segment_id"),
            req_dict.get("provider") or _coerce_dict(req_dict.get("provider_options")).get("provider_hint"),
        )

    try:
        job_id = await orch.create_job(user_id=user_id, req=req)
    except PricingClientError as e:
        # Internal child jobs must never fail user-facing parent jobs because svc-fusion
        # attempted to reserve credits. If this branch fires, the orchestrator still has
        # a pricing path that must be patched, but we surface a precise diagnostic.
        if is_internal_child and "PRICING_INSUFFICIENT_CREDITS" in str(e):
            logger.error(
                "internal_child_pricing_not_suppressed_in_orchestrator user_id=%s err=%s",
                user_id,
                str(e),
            )
        _raise_http_for_pricing_error(e)
        raise

    job = await jobs.get_job(job_id)
    if not job:
        pricing = _suppressed_pricing_payload(req_dict) if is_internal_child else None
        return FusionJobApiView(
            job_id=job_id,
            status="queued",
            provider=str(getattr(req, "provider", None) or orch.default_provider_name or "omnihuman_v15"),
            pricing=pricing,
            pricing_summary=_pricing_suppressed_summary(pricing) if pricing else None,
        )

    out = FusionJobApiView(
        job_id=str(job["id"]),
        status=str(job["status"]),
        provider=_extract_provider_view(job) or str(getattr(req, "provider", None) or orch.default_provider_name or "omnihuman_v15"),
        error_code=job.get("error_code"),
        error_message=job.get("error_message"),
        pricing=_extract_pricing_view(job),
        pricing_summary=_extract_pricing_summary_view(job),
    )

    if is_internal_child and not out.pricing:
        pricing = _suppressed_pricing_payload(req_dict)
        out.pricing = pricing
        out.pricing_summary = _pricing_suppressed_summary(pricing)

    return out

@router.get("/jobs/{job_id}", dependencies=[RequireFusionEnabled], response_model=FusionJobApiView)
async def get_job(job_id: str) -> FusionJobApiView:
    pool = await get_pool()
    jobs = FusionJobsRepo(pool)

    job = await jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    return await _build_job_api_view(pool, job)


@router.get("/jobs/{job_id}/status-light", dependencies=[RequireFusionEnabled], response_model=FusionJobLightView)
async def get_job_status_light(job_id: str) -> FusionJobLightView:
    pool = await get_pool()
    jobs = FusionJobsRepo(pool)

    job = await jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    return await _build_job_light_view(pool, job)


@router.get("/jobs/{job_id}/status", dependencies=[RequireFusionEnabled], response_model=FusionJobApiView)
async def get_job_status(job_id: str) -> FusionJobApiView:
    return await get_job(job_id)


@router.post("/internal/recovery/sweep", dependencies=[RequireFusionEnabled], response_model=FusionRecoverySweepResponse)
async def sweep_recovery(limit: int = 8) -> FusionRecoverySweepResponse:
    pool = await get_pool()
    orch = FusionOrchestrator(pool)
    job_ids = await orch.claim_stale_processing_job_ids(
        limit=max(1, min(int(limit), 50)),
        stale_seconds=90,
        claim_ttl_seconds=60,
    )
    if job_ids:
        await asyncio.gather(*(orch.recover_job(job_id) for job_id in job_ids), return_exceptions=True)
    return FusionRecoverySweepResponse(recovered=len(job_ids), attempted_job_ids=job_ids)
