from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg

PRICING_IMPORT_ERROR: Optional[str] = None

try:
    from desifaces_shared.pricing.client import PricingClientError, SvcPricingClient
    from desifaces_shared.pricing.models import (
        PricingCommitRequest,
        PricingReleaseRequest,
        PricingReserveRequest,
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
from app.services.providers.heygen.assets import HeyGenAssetsClient
from app.services.providers.heygen.av4_payload import build_av4_payload
from app.services.providers.heygen.client import HeyGenAV4Client, HeyGenApiError

logger = logging.getLogger("fusion_orchestrator")


class _DisabledPricingClient:
    enabled = False

    async def reserve(self, req: PricingReserveRequest):
        raise PricingClientError("pricing client unavailable")

    async def commit(self, req: PricingCommitRequest):
        raise PricingClientError("pricing client unavailable")

    async def release(self, req: PricingReleaseRequest):
        raise PricingClientError("pricing client unavailable")


def _classify_error(e: Exception) -> str:
    msg = str(e).lower()
    if isinstance(e, HeyGenApiError):
        if "voice not found" in msg:
            return "HEYGEN_VOICE_NOT_FOUND"
        if "empty_body" in msg or "invalid_json" in msg:
            return "HEYGEN_TRANSIENT_EMPTY_BODY"
        if "timed out" in msg or "timeout" in msg:
            return "HEYGEN_TIMEOUT"
        if "talking_photo_id" in msg:
            return "HEYGEN_TALKING_PHOTO_REQUIRED"
        return "HEYGEN_API_ERROR"
    if "requires" in msg and ("face" in msg or "audio" in msg):
        return "INVALID_REQUEST"
    if "pricing" in msg:
        return "PRICING_ERROR"
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
    HeyGen AV4 orchestration.

    Key behavior:
      - UI should pass artifact IDs (face_artifact_id, audio_artifact_id)
      - Fusion mints fresh SAS at run time to avoid expired SAS URLs
      - Still supports legacy URLs (face_image_url and voice_audio.audio_url)
      - AV4/create-video path requires a real talking_photo_id for talking-photo flow
      - Provider idempotency is job-scoped to avoid cross-job run reuse
      - Pricing can be enforced with DF_PRICING_REQUIRED=1 to fail fast when disabled

    Pricing note:
      - svc-pricing reserve currently expects request.sku_code to carry the
        pricing_variants.code, not the leaf pricing_skus.code.
      - For Fusion this means reserve must use FUSION_TALKING_VIDEO, while the
        leaf SKU remains FUSION_TALK_MIN through pricing_variant_lines.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.jobs = FusionJobsRepo(pool)
        self.steps = StepsRepo(pool)
        self.runs = ProviderRunsRepo(pool)
        self.artifacts = ArtifactsRepo(pool)
        self.perfs = DigitalPerformancesRepo(pool)

        self.provider = HeyGenAV4Client()
        self.assets = HeyGenAssetsClient()
        self.artifact_service = ArtifactService()

        try:
            self.pricing_client = SvcPricingClient.from_env(service_name="svc-fusion")
        except Exception as e:
            logger.exception(
                "svc_fusion_pricing_client_init_failed",
                extra={"error": str(e)},
            )
            self.pricing_client = _DisabledPricingClient()

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

    def _sas_ttl_hours(self) -> int:
        ttl = getattr(settings, "AZURE_SAS_EXPIRY_HOURS", None)
        try:
            if ttl:
                return max(1, int(ttl))
        except Exception:
            pass
        return 4

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

        # Current Fusion pricing is per-minute and reserve needs qty_param=minutes.
        # Until we wire exact predicted duration, reserve a minimum of 1 minute.
        estimated_units = "1"
        estimated_minutes = "1"

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
                "provider": req.provider,
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
                "has_talking_photo_id": bool(getattr(req, "heygen_talking_photo_id", None)),
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
            return pricing

        pricing = self._coerce_dict(meta.get("pricing"))
        if pricing:
            return pricing

        return {}

    async def _persist_pricing_block(self, job_id: str, pricing: Dict[str, Any]) -> None:
        q = """
        UPDATE public.studio_jobs
        SET
          payload_json = COALESCE(payload_json, '{}'::jsonb) || jsonb_build_object('pricing', $2::jsonb),
          meta_json = COALESCE(meta_json, '{}'::jsonb)
                      || jsonb_build_object(
                           'pricing', $2::jsonb,
                           'pricing_state', COALESCE($3::text, ''),
                           'pricing_enabled', $4::bool,
                           'pricing_billing_mode', NULLIF($5::text, ''),
                           'pricing_settlement_mode', NULLIF($6::text, ''),
                           'pricing_billing_account_id', NULLIF($7::text, '')
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
                    str(pricing.get("state") or ""),
                    bool(pricing.get("enabled", False)),
                    str(pricing.get("billing_mode") or ""),
                    str(pricing.get("settlement_mode") or ""),
                    str(pricing.get("billing_account_id") or ""),
                )
        except Exception:
            logger.exception("fusion_pricing_persist_failed", extra={"job_id": job_id})

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
            await self._persist_pricing_block(job_id, pricing)
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
            resp = await self.pricing_client.commit(
                PricingCommitRequest(
                    user_id=str(user_id),
                    reservation_id=reservation_id,
                    actual_units=str(max(1, int(actual_units))),
                    external_ref_type="studio_job",
                    external_ref_id=str(job_id),
                    idempotency_key=f"svc-fusion:job:{job_id}:commit",
                    meta={
                        "variant_code": variant_code,
                        "sku_code": variant_code,
                        "leaf_sku_code": leaf_sku_code,
                        "minutes": str(max(1, int(actual_units))),
                        "service_action": pricing.get("service_action"),
                        "requested_units": pricing.get("estimated_units"),
                    },
                )
            )
            commit_status = str(self._pricing_resp_get(resp, "status", "committed") or "committed")
            pricing = self._merge_pricing_block(
                pricing,
                state="committed",
                variant_code=self._pricing_resp_get(resp, "variant_code") or variant_code,
                sku_code=self._pricing_resp_get(resp, "variant_code") or variant_code,
                leaf_sku_code=self._pricing_resp_get(resp, "sku_code") or leaf_sku_code,
                actual_units=str(max(1, int(actual_units))),
                commit_status=commit_status,
                reservation_status=commit_status,
                ledger_entry_id=self._pricing_resp_get(resp, "ledger_entry_id"),
                billed_units=self._pricing_resp_get(resp, "billed_units") or str(max(1, int(actual_units))),
                amount=self._pricing_resp_get(resp, "amount"),
                currency=self._pricing_resp_get(resp, "currency"),
                billing_mode=self._pricing_resp_get(resp, "billing_mode") or pricing.get("billing_mode"),
                billing_account_id=self._pricing_resp_get(resp, "billing_account_id") or pricing.get("billing_account_id"),
                settlement_mode=self._pricing_resp_get(resp, "settlement_mode") or pricing.get("settlement_mode"),
                entitlement_source=self._pricing_resp_get(resp, "entitlement_source") or pricing.get("entitlement_source"),
                disabled_reason=None,
            )
            await self._persist_pricing_block(job_id, pricing)
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
            resp = await self.pricing_client.release(
                PricingReleaseRequest(
                    user_id=str(user_id),
                    reservation_id=reservation_id,
                    reason=reason,
                    external_ref_type="studio_job",
                    external_ref_id=str(job_id),
                    idempotency_key=f"svc-fusion:job:{job_id}:release",
                    meta={
                        "variant_code": variant_code,
                        "sku_code": variant_code,
                        "leaf_sku_code": leaf_sku_code,
                        "minutes": str(pricing.get("estimated_units") or "1"),
                        "service_action": pricing.get("service_action"),
                    },
                )
            )
            release_status = str(self._pricing_resp_get(resp, "status", "released") or "released")
            pricing = self._merge_pricing_block(
                pricing,
                state="released",
                variant_code=self._pricing_resp_get(resp, "variant_code") or variant_code,
                sku_code=self._pricing_resp_get(resp, "variant_code") or variant_code,
                leaf_sku_code=self._pricing_resp_get(resp, "sku_code") or leaf_sku_code,
                release_status=release_status,
                reservation_status=release_status,
                released_units=self._pricing_resp_get(resp, "released_units"),
                billing_mode=self._pricing_resp_get(resp, "billing_mode") or pricing.get("billing_mode"),
                billing_account_id=self._pricing_resp_get(resp, "billing_account_id") or pricing.get("billing_account_id"),
                settlement_mode=self._pricing_resp_get(resp, "settlement_mode") or pricing.get("settlement_mode"),
                entitlement_source=self._pricing_resp_get(resp, "entitlement_source") or pricing.get("entitlement_source"),
                disabled_reason=None,
            )
            await self._persist_pricing_block(job_id, pricing)
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
        - Prefer stable IDs (artifact IDs / heygen_talking_photo_id / image_key) for hashing.
        - If a URL is provided (SAS), strip query string for stability.
        - Pricing-enabled jobs start as pricing_pending and only become queued after reserve succeeds.
        - Insufficient credits is a soft product block: return a blocked job with job_id.
        """
        face_artifact_id = getattr(req, "face_artifact_id", None)

        voice_audio = getattr(req, "voice_audio", None)
        audio_artifact_id = getattr(voice_audio, "audio_artifact_id", None) if voice_audio else None

        stable_spec: Dict[str, Any] = {
            "provider": req.provider,
            "voice_mode": req.voice_mode.value,
            "face_artifact_id": str(face_artifact_id) if face_artifact_id else None,
            "heygen_talking_photo_id": (
                req.heygen_talking_photo_id.strip() if req.heygen_talking_photo_id else None
            ),
            "image_key": (req.image_key.strip() if req.image_key else None),
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
            "payload_version": f"{settings.HEYGEN_AV4_PAYLOAD_VERSION}.v2",
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

        pricing_enabled = self._pricing_enabled()
        initial_status = "pricing_pending" if pricing_enabled else "queued"

        job_id = await self.jobs.insert_job(
            user_id=user_id,
            request_hash=req_hash,
            payload=req.model_dump(),
            initial_status=initial_status,
        )

        pricing = self._build_initial_pricing_block(req)
        await self._persist_pricing_block(job_id, pricing)

        if self._pricing_required() and not pricing_enabled:
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

        return job_id



    async def _resolve_face_url(self, job_id: str, req: FusionJobCreate) -> str:
        """
        Resolve face input to a fetchable URL/SAS.

        Priority:
          1) req.face_image_url
          2) req.face_artifact_id -> mint fresh SAS
        """
        if getattr(req, "face_image_url", None):
            return str(req.face_image_url)

        face_artifact_id = getattr(req, "face_artifact_id", None)
        if not face_artifact_id:
            raise ValueError("Provide face_image_url or face_artifact_id")

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

    async def _resolve_audio_url(self, job_id: str, req: FusionJobCreate) -> str:
        """
        Resolve audio input to a fetchable Azure Blob SAS URL.
        """
        if req.voice_audio and req.voice_audio.audio_url:
            return str(req.voice_audio.audio_url)

        audio_artifact_id = None
        if req.voice_audio and getattr(req.voice_audio, "audio_artifact_id", None):
            audio_artifact_id = str(req.voice_audio.audio_artifact_id)

        if not audio_artifact_id:
            raise ValueError("voice_mode=audio requires voice_audio.audio_url or voice_audio.audio_artifact_id")

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

    async def _resolve_talking_photo_id(self, job_id: str, req: FusionJobCreate) -> str:
        """
        Resolve HeyGen talking_photo_id.

        Preferred:
          1) req.heygen_talking_photo_id
          2) req.image_key (back-compat alias only if caller already stores talking_photo_id there)

        Best-effort fallback:
          3) resolve face URL and call existing HeyGenAssetsClient; if it returns a real
             talking_photo_id / avatar_id / photo_avatar_id, use it.
             If it only returns image_key, we record it for diagnostics but fail clearly.
        """
        if getattr(req, "heygen_talking_photo_id", None):
            talking_photo_id = str(req.heygen_talking_photo_id).strip()
            if not talking_photo_id:
                raise ValueError("heygen_talking_photo_id is empty after strip")

            await self.artifacts.add_artifact(
                job_id,
                "heygen_talking_photo_id",
                talking_photo_id,
                content_type="text/plain",
                meta_json={"source": "request_payload"},
            )
            return talking_photo_id

        if getattr(req, "image_key", None):
            talking_photo_id = str(req.image_key).strip()
            if talking_photo_id:
                await self.artifacts.add_artifact(
                    job_id,
                    "heygen_talking_photo_id",
                    talking_photo_id,
                    content_type="text/plain",
                    meta_json={"source": "request_payload.image_key_alias"},
                )
                return talking_photo_id

        face_url = await self._resolve_face_url(job_id, req)
        img_upload = await self.assets.create_talking_photo_from_url(face_url)

        talking_photo_id = _extract_talking_photo_id(img_upload)
        if talking_photo_id:
            await self.artifacts.add_artifact(
                job_id,
                "heygen_talking_photo_id",
                talking_photo_id,
                content_type="text/plain",
                meta_json={"provider": "heygen", "upload": img_upload},
            )
            return talking_photo_id

        image_key = (img_upload.get("data") or {}).get("image_key") or img_upload.get("image_key")
        if image_key:
            await self.artifacts.add_artifact(
                job_id,
                "heygen_image_key",
                str(image_key),
                content_type="text/plain",
                meta_json={"provider": "heygen", "upload": img_upload},
            )

        raise ValueError(
            "heygen_talking_photo_id is required for AV4/talking-photo flow. "
            "Provide req.heygen_talking_photo_id (avatar look id), or upgrade HeyGenAssetsClient "
            "to create/return a Photo Avatar talking_photo_id instead of only image_key."
        )

    # -------------------------------------------------------------------------
    # Job runner
    # -------------------------------------------------------------------------
    async def run_job(self, job_id: str) -> None:
        job = await self.jobs.get_job(job_id)
        if not job:
            logger.warning("job_not_found", extra={"job_id": job_id})
            return

        status = str(job.get("status") or "")
        if status in ("succeeded", "failed", "canceled"):
            logger.info("job_terminal_skip", extra={"job_id": job_id, "status": status})
            return

        payload_json = job["payload_json"]
        if isinstance(payload_json, str):
            payload_json = json.loads(payload_json)
        if not isinstance(payload_json, dict):
            raise ValueError(f"Unexpected payload_json type: {type(payload_json)}")

        req = FusionJobCreate.model_validate(payload_json)

        provider_name = "heygen_av4"
        req_hash = str(job["request_hash"])
        idem = provider_idempotency_key(
            provider_name,
            f"{settings.HEYGEN_AV4_PAYLOAD_VERSION}.v2",
            req_hash,
        )
        provider_idem = f"{idem}:{job_id}"

        run_id: Optional[str] = None
        provider_job_id: Optional[str] = None
        talking_photo_id: Optional[str] = None
        audio_url_to_use: Optional[str] = None
        last_poll = None
        performance_id: Optional[str] = None
        pricing: Dict[str, Any] = {}

        user_id = str(job.get("user_id") or "").strip()

        try:
            if self._pricing_required() and not self._pricing_enabled():
                reason = self._pricing_disabled_reason()
                await self.jobs.set_status(
                    job_id,
                    "failed",
                    error_code="PRICING_CLIENT_DISABLED",
                    error_message=reason,
                )
                return

            pricing = await self._load_latest_pricing(job_id)
            if self._pricing_enabled():
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

            await self.steps.upsert_step(job_id, StepCode.provider_submit.value, "running", attempt=0)

            talking_photo_id = await self._resolve_talking_photo_id(job_id, req)

            if req.voice_mode.value == "audio":
                audio_url_to_use = await self._resolve_audio_url(job_id, req)

                await self.artifacts.add_artifact(
                    job_id,
                    "provider_audio_ref",
                    audio_url_to_use,
                    content_type="text/uri-list",
                    meta_json={"provider": "azure_blob"},
                )

                await self.artifacts.add_artifact(
                    job_id,
                    "heygen_audio_url",
                    audio_url_to_use,
                    content_type="text/uri-list",
                    meta_json={"provider": "azure_blob"},
                )

            video_title = f"desifaces_fusion_{job_id}"
            av4_payload = build_av4_payload(
                req,
                talking_photo_id=talking_photo_id,
                video_title=video_title,
                audio_url_override=audio_url_to_use,
            )

            try:
                await self.steps.upsert_step(
                    job_id,
                    StepCode.provider_submit.value,
                    "running",
                    attempt=0,
                    meta_json={
                        "talking_photo_id": talking_photo_id,
                        "audio_url_present": bool(audio_url_to_use),
                        "idempotency_key": provider_idem,
                    },
                )
            except Exception:
                pass

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
                run_id = await self.runs.create_run(
                    job_id=job_id,
                    provider=provider_name,
                    idempotency_key=provider_idem,
                    request_json=av4_payload,
                )
                submit_res = await self.provider.submit(av4_payload, provider_idem)
                provider_job_id = submit_res.provider_job_id
                if not provider_job_id:
                    raise HeyGenApiError("provider_job_id missing after submit")
                await self.runs.mark_submitted(run_id, provider_job_id, submit_res.raw_response)

            if not provider_job_id:
                raise HeyGenApiError("provider_job_id missing after submit/reuse")

            await self.steps.upsert_step(
                job_id,
                StepCode.provider_submit.value,
                "succeeded",
                attempt=0,
                meta_json={
                    "provider_job_id": provider_job_id,
                    "idempotency_key": provider_idem,
                    "talking_photo_id": talking_photo_id,
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
                    "talking_photo_id": talking_photo_id,
                    "voice_mode": req.voice_mode.value,
                    "payload_version": f"{settings.HEYGEN_AV4_PAYLOAD_VERSION}.v2",
                },
            )
            await self.perfs.upsert_fusion_job_output(job_id, performance_id)

            await self.steps.upsert_step(
                job_id,
                StepCode.provider_poll.value,
                "running",
                attempt=0,
                meta_json={"provider_job_id": provider_job_id},
            )

            started = asyncio.get_running_loop().time()
            last_status: Optional[str] = None
            poll_errors = 0
            provider_timeout_s = _provider_poll_timeout_seconds()
            poll_interval_s = _provider_poll_interval_seconds()

            while True:
                elapsed = asyncio.get_running_loop().time() - started
                if elapsed > provider_timeout_s:
                    raise HeyGenApiError(f"Provider polling timed out after {int(elapsed)}s")

                try:
                    poll = await self.provider.poll(provider_job_id)
                    poll_errors = 0
                except Exception as exc:
                    poll_errors += 1
                    if poll_errors >= 5:
                        raise HeyGenApiError(f"Provider poll failed repeatedly: {exc}") from exc
                    await asyncio.sleep(poll_interval_s)
                    continue

                last_poll = poll

                raw_status = str(getattr(poll, "status", None) or "").strip()
                normalized_status = _normalize_provider_status(raw_status)
                video_url = str(getattr(poll, "video_url", None) or "").strip()

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
                            },
                        )
                    await self.steps.upsert_step(job_id, StepCode.provider_poll.value, "succeeded", attempt=0)
                    break

                if normalized_status == "processing":
                    await asyncio.sleep(poll_interval_s)
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
                            },
                        )
                    raise HeyGenApiError(poll.error_message or f"Provider failed with status={raw_status!r}")

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
                            },
                        )
                    raise HeyGenApiError(f"Provider canceled with status={raw_status!r}")

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
                            },
                        )
                    raise HeyGenApiError("Provider returned succeeded but video_url is missing")

                await asyncio.sleep(poll_interval_s)

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
                    "talking_photo_id": talking_photo_id,
                },
            )

            share_url_val: Optional[str] = None
            try:
                get_share = getattr(self.provider, "get_share_url", None)
                if callable(get_share):
                    share = await get_share(provider_job_id)
                    share_url = (share or {}).get("share_url")
                    if share_url:
                        share_url_val = str(share_url)
                        await self.artifacts.add_artifact(
                            job_id,
                            "share_url",
                            share_url_val,
                            content_type="text/uri-list",
                            meta_json={
                                "provider": provider_name,
                                "provider_job_id": provider_job_id,
                                "raw": (share or {}).get("raw"),
                            },
                        )
            except Exception as e:
                logger.warning(
                    "share_url_failed",
                    extra={"job_id": job_id, "provider_job_id": provider_job_id, "error": str(e)},
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
                actual_units=1,
            )

            await self.steps.upsert_step(job_id, StepCode.finalize.value, "succeeded", attempt=0)
            await self.jobs.set_status(job_id, "succeeded")
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
                    "talking_photo_id": talking_photo_id,
                    "performance_id": performance_id,
                },
            )

            try:
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
                        },
                    )
            except Exception:
                pass

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
                        },
                    )
                    await self.perfs.upsert_fusion_job_output(job_id, performance_id)

                if performance_id:
                    await self.perfs.mark_failed(
                        performance_id,
                        error_code=code,
                        error_message=msg,
                        meta_json={"job_id": job_id, "user_id": str(job["user_id"])},
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