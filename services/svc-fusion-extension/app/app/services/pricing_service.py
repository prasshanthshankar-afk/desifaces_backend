from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

import asyncpg

PRICING_IMPORT_ERROR: Optional[str] = None

try:
    from desifaces_shared.pricing.client import PricingClientError, SvcPricingClient
    from desifaces_shared.pricing.orchestration import (
        PricingReserveSpec,
        PricingCommitSpec,
        PricingReleaseSpec,
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
    logging.getLogger("story_pricing_service").exception(
        "svc_fusion_extension_pricing_import_failed",
        extra={"error": PRICING_IMPORT_ERROR},
    )

    class PricingClientError(Exception):
        pass

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
        external_ref_type: str = "story_job"

    @dataclass
    class PricingCommitSpec:
        user_id: str
        reservation_id: str
        actual_units: str
        external_ref_id: str
        idempotency_key: str
        meta: Dict[str, Any]
        external_ref_type: str = "story_job"

    @dataclass
    class PricingReleaseSpec:
        user_id: str
        reservation_id: str
        reason: str
        external_ref_id: str
        idempotency_key: str
        meta: Dict[str, Any]
        external_ref_type: str = "story_job"

    def build_reserve_request(spec): return spec
    def build_commit_request(spec): return spec
    def build_release_request(spec): return spec
    def build_pricing_summary(pricing): return {}
    def make_reserved_artifact(*args, **kwargs): return {"pricing": {}, "pricing_summary": {}}
    def make_committed_artifact(*args, **kwargs): return {"pricing": {}, "pricing_summary": {}}
    def make_released_artifact(*args, **kwargs): return {"pricing": {}, "pricing_summary": {}}

    class SvcPricingClient:
        enabled = False
        @classmethod
        def from_env(cls, service_name: str):
            return cls()
        async def reserve(self, req): raise PricingClientError("pricing client unavailable")
        async def commit(self, req): raise PricingClientError("pricing client unavailable")
        async def release(self, req): raise PricingClientError("pricing client unavailable")

logger = logging.getLogger("story_pricing_service")


class StoryPricingService:
    """Parent storytelling job pricing owner.

    This intentionally reuses svc-pricing + desifaces_shared.pricing.* instead of
    redesigning pricing logic inside svc-fusion-extension.
    """

    def __init__(self, pool: asyncpg.Pool, table_name: str = "public.longform_jobs"):
        self.pool = pool
        self.table_name = table_name
        try:
            self.pricing_client = SvcPricingClient.from_env(service_name="svc-fusion-extension")
        except Exception:
            logger.exception("story_pricing_client_init_failed")
            self.pricing_client = SvcPricingClient()

    def _story_variant_code(self, profile: Optional[str] = None) -> str:
        if str(profile or "").strip().lower() == "cinematic_video_direction":
            return "CINEMATIC_VIDEO_DIRECTION"
        return "TALKING_VIDEO"

    def _story_leaf_sku_code(self, profile: Optional[str] = None) -> str:
        if str(profile or "").strip().lower() == "cinematic_video_direction":
            return "LONGFORM_CINEMATIC_MIN"
        return "LONGFORM_TALK_MIN"

    def _story_service_action(self, profile: Optional[str] = None) -> str:
        if str(profile or "").strip().lower() == "cinematic_video_direction":
            return "fusion.longform.cinematic_video_direction"
        return "fusion.longform.talking_video"

    def _estimate_units(self, *, audio_duration_sec: float) -> int:
        return max(1, int(math.ceil(max(audio_duration_sec, 1.0) / 60.0)))

    async def _persist_story_pricing(self, *, job_id: str, pricing: Dict[str, Any], pricing_summary: Optional[Dict[str, Any]] = None) -> None:
        query = f"""
        UPDATE {self.table_name}
        SET
          tags = COALESCE(tags, '{{}}'::jsonb)
                 || jsonb_build_object(
                      'pricing', $2::jsonb,
                      'pricing_summary', $3::jsonb
                    ),
          updated_at = now()
        WHERE id = $1::uuid
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                query,
                job_id,
                json.dumps(pricing or {}),
                json.dumps(pricing_summary or build_pricing_summary(pricing or {})),
            )

    async def preview_story_job(self, *, story_input: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
        profile = str((story_input or {}).get("longform_profile") or "talking_video")
        units = self._estimate_units(audio_duration_sec=float(plan.get("audio_duration_sec") or 0.0))
        pricing = {
            "enabled": bool(getattr(self.pricing_client, "enabled", False)),
            "state": "quoted",
            "service_name": "svc-fusion-extension",
            "service_action": self._story_service_action(profile),
            "variant_code": self._story_variant_code(profile),
            "sku_code": self._story_leaf_sku_code(profile),
            "leaf_sku_code": self._story_leaf_sku_code(profile),
            "estimated_units": str(units),
            "unit_type": "minute",
            "meta": {
                "minutes": str(units),
                "story_input": story_input,
                "plan_summary": {
                    "audio_duration_sec": plan.get("audio_duration_sec"),
                    "shot_count": len(plan.get("shot_plan") or []),
                },
            },
        }
        return {"pricing": pricing, "pricing_summary": build_pricing_summary(pricing)}

    async def reserve_story_job(self, *, job_id: str, user_id: str, story_input: Dict[str, Any], plan: Dict[str, Any], current_pricing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        units = self._estimate_units(audio_duration_sec=float(plan.get("audio_duration_sec") or 0.0))
        base = dict(current_pricing or {})
        profile = str((story_input or {}).get("longform_profile") or "talking_video")
        meta = {
            **dict(base.get("meta") or {}),
            "minutes": str(units),
            "variant_code": self._story_variant_code(profile),
            "leaf_sku_code": self._story_leaf_sku_code(profile),
            "story_input": story_input,
            "plan_summary": {
                "audio_duration_sec": plan.get("audio_duration_sec"),
                "shot_count": len(plan.get("shot_plan") or []),
            },
        }
        spec = PricingReserveSpec(
            user_id=str(user_id),
            service_name="svc-fusion-extension",
            service_action=self._story_service_action(profile),
            sku_code=self._story_variant_code(profile),
            units=str(units),
            external_ref_id=str(job_id),
            idempotency_key=f"svc-fusion-extension:job:{job_id}:reserve",
            meta=meta,
            external_ref_type="story_job",
        )
        resp = await self.pricing_client.reserve(build_reserve_request(spec))
        artifact = make_reserved_artifact(resp, base_pricing=base, meta=meta)
        pricing = dict(artifact.get("pricing") or {})
        pricing["enabled"] = True
        pricing["state"] = "reserved"
        pricing["variant_code"] = self._story_variant_code(profile)
        pricing["sku_code"] = self._story_leaf_sku_code(profile)
        pricing["leaf_sku_code"] = self._story_leaf_sku_code(profile)
        pricing_summary = dict(artifact.get("pricing_summary") or build_pricing_summary(pricing))
        await self._persist_story_pricing(job_id=job_id, pricing=pricing, pricing_summary=pricing_summary)
        return {"pricing": pricing, "pricing_summary": pricing_summary, "raw": resp}

    async def commit_story_job(self, *, job_id: str, user_id: str, pricing: Dict[str, Any], final_video_duration_sec: float) -> Dict[str, Any]:
        reservation_id = str(pricing.get("reservation_id") or "").strip()
        if not reservation_id:
            return {"pricing": pricing, "pricing_summary": build_pricing_summary(pricing)}
        units = self._estimate_units(audio_duration_sec=final_video_duration_sec)
        profile = str((dict(pricing.get("meta") or {})).get("story_input", {}).get("longform_profile") or "talking_video")
        meta = {
            **dict(pricing.get("meta") or {}),
            "minutes": str(units),
            "variant_code": self._story_variant_code(profile),
            "leaf_sku_code": self._story_leaf_sku_code(profile),
        }
        spec = PricingCommitSpec(
            user_id=str(user_id),
            reservation_id=reservation_id,
            actual_units=str(units),
            external_ref_id=str(job_id),
            idempotency_key=f"svc-fusion-extension:job:{job_id}:commit",
            meta=meta,
            external_ref_type="story_job",
        )
        resp = await self.pricing_client.commit(build_commit_request(spec))
        artifact = make_committed_artifact(resp, base_pricing=pricing, actual_units=str(units), meta=meta)
        committed = dict(artifact.get("pricing") or {})
        committed["enabled"] = True
        committed["state"] = "committed"
        committed["quote_id"] = committed.get("quote_id") or pricing.get("quote_id")
        committed["preview_fingerprint"] = committed.get("preview_fingerprint") or pricing.get("preview_fingerprint")
        committed["variant_code"] = pricing.get("variant_code")
        committed["sku_code"] = pricing.get("leaf_sku_code") or pricing.get("sku_code")
        committed["leaf_sku_code"] = pricing.get("leaf_sku_code")
        summary = dict(artifact.get("pricing_summary") or build_pricing_summary(committed))
        await self._persist_story_pricing(job_id=job_id, pricing=committed, pricing_summary=summary)
        return {"pricing": committed, "pricing_summary": summary, "raw": resp}

    async def release_story_job(self, *, job_id: str, user_id: str, pricing: Dict[str, Any], reason: str) -> Dict[str, Any]:
        reservation_id = str(pricing.get("reservation_id") or "").strip()
        if not reservation_id:
            return {"pricing": pricing, "pricing_summary": build_pricing_summary(pricing)}
        profile = str((dict(pricing.get("meta") or {})).get("story_input", {}).get("longform_profile") or "talking_video")
        meta = {
            **dict(pricing.get("meta") or {}),
            "variant_code": self._story_variant_code(profile),
            "leaf_sku_code": self._story_leaf_sku_code(profile),
        }
        spec = PricingReleaseSpec(
            user_id=str(user_id),
            reservation_id=reservation_id,
            reason=str(reason),
            external_ref_id=str(job_id),
            idempotency_key=f"svc-fusion-extension:job:{job_id}:release",
            meta=meta,
            external_ref_type="story_job",
        )
        resp = await self.pricing_client.release(build_release_request(spec))
        artifact = make_released_artifact(resp, base_pricing=pricing, meta=meta)
        released = dict(artifact.get("pricing") or {})
        released["enabled"] = True
        released["state"] = "released"
        released["quote_id"] = released.get("quote_id") or pricing.get("quote_id")
        released["preview_fingerprint"] = released.get("preview_fingerprint") or pricing.get("preview_fingerprint")
        released["variant_code"] = pricing.get("variant_code")
        released["sku_code"] = pricing.get("leaf_sku_code") or pricing.get("sku_code")
        released["leaf_sku_code"] = pricing.get("leaf_sku_code")
        summary = dict(artifact.get("pricing_summary") or build_pricing_summary(released))
        await self._persist_story_pricing(job_id=job_id, pricing=released, pricing_summary=summary)
        return {"pricing": released, "pricing_summary": summary, "raw": resp}
