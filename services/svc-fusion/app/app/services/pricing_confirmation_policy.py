from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Optional

from desifaces_shared.pricing.orchestration import PricingPreviewSpec, build_preview_request

from app.services.fusion_orchestrator import FusionOrchestrator, PricingClientError


_INSTALLED = False


def _coerce_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump(exclude_none=True)
            return dict(dumped) if isinstance(dumped, dict) else {}
        except Exception:
            return {}
    return {}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _confirmation(req: Any) -> tuple[str, str]:
    value = getattr(req, "pricing_confirmation", None)
    if value is None:
        return "", ""
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    data = _coerce_dict(value)
    return _clean(data.get("quote_id")), _clean(data.get("preview_fingerprint"))


def _request_payload(req: Any) -> Dict[str, Any]:
    if hasattr(req, "model_dump"):
        payload = req.model_dump(exclude_none=True)
    elif hasattr(req, "dict"):
        payload = req.dict(exclude_none=True)
    else:
        payload = {}
    payload = dict(payload or {})
    # Pricing confirmation is evidence about the preview. It must not become part
    # of the payload used to reproduce the preview fingerprint itself.
    payload.pop("pricing_confirmation", None)
    return payload


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
    for candidate in candidates:
        try:
            if candidate is None:
                continue
            value = float(candidate)
            if value > 1000:
                value /= 1000.0
            if value > 0:
                return value
        except Exception:
            continue
    return 60.0


def _estimated_fusion_minutes(req_dict: Dict[str, Any]) -> str:
    seconds = max(1.0, _extract_duration_seconds(req_dict))
    return str(max(1, int(math.ceil(seconds / 60.0))))


def _preview_meta(req_dict: Dict[str, Any], orch: FusionOrchestrator) -> Dict[str, Any]:
    estimated_minutes = _estimated_fusion_minutes(req_dict)
    voice_audio = _coerce_dict(req_dict.get("voice_audio"))
    voice_tts = _coerce_dict(req_dict.get("voice_tts"))
    video = _coerce_dict(req_dict.get("video"))
    provider_options = _coerce_dict(req_dict.get("provider_options"))
    reference_image_urls = list(req_dict.get("reference_image_urls") or [])
    reference_image_artifact_ids = list(req_dict.get("reference_image_artifact_ids") or [])

    provider = _clean(req_dict.get("provider") or orch.default_provider_name or "omnihuman_v15") or "omnihuman_v15"
    aspect_ratio = _clean(video.get("aspect_ratio") or provider_options.get("aspect_ratio") or "9:16")
    resolution = _clean(video.get("resolution") or provider_options.get("resolution") or "720p")

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


async def _fresh_preview_confirmation(
    orch: FusionOrchestrator,
    *,
    user_id: str,
    req: Any,
) -> tuple[str, str]:
    req_dict = _request_payload(req)
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

    spec = PricingPreviewSpec(
        user_id=str(user_id),
        service_name="svc-fusion",
        service_action="fusion.video.generate",
        sku_code=orch._fusion_pricing_variant_code(),
        units=estimated_units,
        idempotency_key=f"svc-fusion:preview:{user_id}:{request_fingerprint}",
        meta=meta,
    )
    resp = await orch.pricing_client.preview(build_preview_request(spec))
    return _clean(getattr(resp, "quote_id", None)), _clean(getattr(resp, "preview_fingerprint", None))


class _ConfirmationPricingClientProxy:
    def __init__(self, client: Any, *, quote_id: str, preview_fingerprint: str) -> None:
        self._client = client
        self.enabled = bool(getattr(client, "enabled", False))
        self._quote_id = quote_id
        self._preview_fingerprint = preview_fingerprint

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def reserve(self, req: Any) -> Any:
        updates = {
            "quote_id": self._quote_id,
            "preview_fingerprint": self._preview_fingerprint,
        }
        if hasattr(req, "model_copy"):
            req = req.model_copy(update=updates)
        else:
            for key, value in updates.items():
                try:
                    setattr(req, key, value)
                except Exception:
                    pass
        return await self._client.reserve(req)


def install_pricing_confirmation_policy() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    original_create_job = FusionOrchestrator.create_job
    original_build_initial = FusionOrchestrator._build_initial_pricing_block
    original_reserve = FusionOrchestrator._reserve_pricing_for_job

    async def create_job(self: FusionOrchestrator, user_id: str, req: Any) -> str:
        payload = _request_payload(req)
        internal_child = bool(self._is_internal_child_request_payload(payload))
        if internal_child or not self._pricing_enabled():
            return await original_create_job(self, user_id, req)

        quote_id, preview_fingerprint = _confirmation(req)
        if not quote_id or not preview_fingerprint:
            raise PricingClientError("PRICING_CONFIRMATION_REQUIRED")

        fresh_quote_id, fresh_preview_fingerprint = await _fresh_preview_confirmation(
            self,
            user_id=str(user_id),
            req=req,
        )
        if not fresh_quote_id or not fresh_preview_fingerprint:
            raise PricingClientError("PRICING_CONFIRMATION_PREVIEW_INCOMPLETE")
        if quote_id != fresh_quote_id or preview_fingerprint != fresh_preview_fingerprint:
            raise PricingClientError("PRICING_CONFIRMATION_MISMATCH")

        return await original_create_job(self, user_id, req)

    def build_initial_pricing_block(self: FusionOrchestrator, req: Any) -> Dict[str, Any]:
        pricing = dict(original_build_initial(self, req) or {})
        quote_id, preview_fingerprint = _confirmation(req)
        if quote_id:
            pricing["quote_id"] = quote_id
        if preview_fingerprint:
            pricing["preview_fingerprint"] = preview_fingerprint
        return pricing

    async def reserve_pricing_for_job(
        self: FusionOrchestrator,
        *,
        job_id: str,
        user_id: str,
        pricing: Dict[str, Any],
    ) -> Dict[str, Any]:
        quote_id = _clean(pricing.get("quote_id"))
        preview_fingerprint = _clean(pricing.get("preview_fingerprint"))
        if not quote_id or not preview_fingerprint:
            return await original_reserve(
                self,
                job_id=job_id,
                user_id=user_id,
                pricing=pricing,
            )

        original_client = self.pricing_client
        self.pricing_client = _ConfirmationPricingClientProxy(
            original_client,
            quote_id=quote_id,
            preview_fingerprint=preview_fingerprint,
        )
        try:
            return await original_reserve(
                self,
                job_id=job_id,
                user_id=user_id,
                pricing=pricing,
            )
        finally:
            self.pricing_client = original_client

    FusionOrchestrator.create_job = create_job
    FusionOrchestrator._build_initial_pricing_block = build_initial_pricing_block
    FusionOrchestrator._reserve_pricing_for_job = reserve_pricing_for_job
    _INSTALLED = True
