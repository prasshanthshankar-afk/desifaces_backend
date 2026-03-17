from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import asyncpg

PRICING_IMPORT_ERROR: Optional[str] = None

try:
    from desifaces_shared.pricing.client import PricingClientError, SvcPricingClient
    from desifaces_shared.pricing.models import (
        PricingReserveRequest,
        PricingCommitRequest,
        PricingReleaseRequest,
    )
except Exception as pricing_import_error:
    PRICING_IMPORT_ERROR = str(pricing_import_error)
    logging.getLogger("tts_orchestrator").exception(
        "svc_audio_pricing_import_failed",
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


from app.repos.tts_jobs_repo import TTSJobsRepo
from app.services.tts_service import TTSService
from app.services.azure_storage_service import AzureStorageService

logger = logging.getLogger("tts_orchestrator")


class _DisabledPricingClient:
    enabled = False

    async def reserve(self, req: PricingReserveRequest):
        raise PricingClientError("pricing client unavailable")

    async def commit(self, req: PricingCommitRequest):
        raise PricingClientError("pricing client unavailable")

    async def release(self, req: PricingReleaseRequest):
        raise PricingClientError("pricing client unavailable")


def _jsonb_to_dict(val: Any) -> Dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return {}
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    try:
        d = dict(val)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _safe_float(val: Any, default: float) -> float:
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return default
        try:
            return float(s)
        except Exception:
            return default
    try:
        return float(val)
    except Exception:
        return default


def _upload_fields(upload: Any) -> Tuple[str, str, str, int]:
    """
    Returns: (sas_url, storage_path, sha256, bytes)
    Works with either dict or UploadBytesResult-like objects.
    """
    if isinstance(upload, dict):
        sas_url = str(upload.get("sas_url") or upload.get("url") or "").strip()
        storage_path = str(upload.get("storage_path") or upload.get("blob_key") or "").strip()
        sha256 = str(upload.get("sha256") or "").strip()
        b = upload.get("bytes")
        try:
            nbytes = int(b) if b is not None else 0
        except Exception:
            nbytes = 0
        return sas_url, storage_path, sha256, nbytes

    sas_url = str(getattr(upload, "sas_url", None) or getattr(upload, "url", None) or "").strip()
    storage_path = str(
        getattr(upload, "storage_path", None) or getattr(upload, "blob_key", None) or ""
    ).strip()
    sha256 = str(getattr(upload, "sha256", None) or "").strip()
    b = getattr(upload, "bytes", None)
    try:
        nbytes = int(b) if b is not None else 0
    except Exception:
        nbytes = 0
    return sas_url, storage_path, sha256, nbytes


def _chars_1k_units(text: str) -> int:
    """
    Pricing for AUDIO_TTS is typically parameterized by chars_1k.
    Use ceil(chars / 1000), minimum 1 for any non-empty synthesis.
    """
    n = len((text or "").strip())
    if n <= 0:
        return 1
    return max(1, int(math.ceil(n / 1000.0)))


def _classify_error(e: Exception) -> str:
    msg = str(e or "").lower()

    if "insufficient" in msg and "credit" in msg:
        return "PRICING_INSUFFICIENT_CREDITS"
    if "pricing" in msg:
        return "PRICING_ERROR"
    if "voice" in msg and "not found" in msg:
        return "VOICE_NOT_FOUND"
    if "locale" in msg and "not found" in msg:
        return "LOCALE_NOT_SUPPORTED"
    if "translate" in msg:
        return "TRANSLATION_FAILED"
    return "tts_failed"


class TTSOrchestrator:
    STEP_CODE = "tts"
    STUDIO_TYPE = "audio"
    VARIANT_CODE = "AUDIO_TTS"

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.jobs = TTSJobsRepo(pool, studio_type=self.STUDIO_TYPE)
        self.tts = TTSService(pool)
        self.storage = AzureStorageService()

        try:
            self.pricing_client = SvcPricingClient.from_env(service_name="svc-audio")
        except Exception as e:
            logger.exception(
                "svc_audio_pricing_client_init_failed",
                extra={"error": str(e)},
            )
            self.pricing_client = _DisabledPricingClient()

    # -------------------------------------------------------------------------
    # Pricing helpers
    # -------------------------------------------------------------------------
    def _pricing_enabled(self) -> bool:
        try:
            return bool(getattr(self.pricing_client, "enabled", False))
        except Exception:
            return False

    def _pricing_required(self) -> bool:
        v = str(os.getenv("DF_PRICING_REQUIRED", "0")).strip().lower()
        return v in {"1", "true", "yes", "y"}

    def _pricing_disabled_reason(self) -> str:
        if PRICING_IMPORT_ERROR:
            return f"pricing_import_failed: {PRICING_IMPORT_ERROR}"
        return "svc-audio pricing client is disabled or not configured"

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
        payload = _jsonb_to_dict(payload_json)
        meta = _jsonb_to_dict(meta_json)

        pricing = _jsonb_to_dict(payload.get("pricing"))
        if pricing:
            return pricing

        pricing = _jsonb_to_dict(meta.get("pricing"))
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
          AND studio_type = $8::text
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
                    self.STUDIO_TYPE,
                )
        except Exception:
            logger.exception("audio_pricing_persist_failed", extra={"job_id": job_id})

    async def _load_latest_pricing(self, job_id: str) -> Dict[str, Any]:
        q = """
        SELECT payload_json, meta_json
        FROM public.studio_jobs
        WHERE id = $1::uuid
          AND studio_type = $2::text
        LIMIT 1
        """
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(q, job_id, self.STUDIO_TYPE)
            if not row:
                return {}
            return self._pricing_from_payload_meta(
                _jsonb_to_dict(row["payload_json"]),
                _jsonb_to_dict(row["meta_json"]),
            )
        except Exception:
            logger.exception("audio_pricing_load_failed", extra={"job_id": job_id})
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

            if state in {
                "reserved",
                "committed",
                "released",
                "reservation_failed",
                "commit_failed",
                "release_failed",
                "blocked",
            }:
                return last_pricing

            if reservation_id and state in {"pending_reservation", "pricing_pending", ""}:
                out = dict(last_pricing)
                out["state"] = "reserved"
                out["reservation_status"] = out.get("reservation_status") or "reserved"
                return out

            if asyncio.get_running_loop().time() >= deadline:
                return last_pricing

            await asyncio.sleep(poll_s)

    async def _commit_pricing_for_job(
        self,
        *,
        job_id: str,
        user_id: str,
        pricing: Dict[str, Any],
        final_text: str,
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
                error="missing_reservation_id_at_commit",
            )
            await self._persist_pricing_block(job_id, pricing)
            return pricing

        if state not in {"reserved", "commit_failed"}:
            return pricing

        actual_units = _chars_1k_units(final_text)
        variant_code = (
            str(pricing.get("variant_code") or pricing.get("sku_code") or self.VARIANT_CODE).strip()
            or self.VARIANT_CODE
        )
        leaf_sku_code = str(pricing.get("leaf_sku_code") or "").strip() or None

        commit_meta = {
            "variant_code": variant_code,
            "sku_code": variant_code,
            "service_action": str(pricing.get("service_action") or "audio.tts.generate"),
            "requested_units": str(pricing.get("estimated_units") or actual_units),
            "chars_1k": str(actual_units),
            "text_length": len(final_text or ""),
            "target_locale": (
                _jsonb_to_dict(pricing.get("meta")).get("target_locale")
                if isinstance(pricing.get("meta"), dict)
                else None
            ),
        }
        if leaf_sku_code:
            commit_meta["leaf_sku_code"] = leaf_sku_code

        try:
            resp = await self.pricing_client.commit(
                PricingCommitRequest(
                    user_id=str(user_id),
                    reservation_id=reservation_id,
                    actual_units=str(actual_units),
                    external_ref_type="studio_job",
                    external_ref_id=str(job_id),
                    idempotency_key=f"svc-audio:job:{job_id}:commit",
                    meta=commit_meta,
                )
            )
            commit_status = str(self._pricing_resp_get(resp, "status", "committed") or "committed")
            pricing = self._merge_pricing_block(
                pricing,
                state="committed",
                variant_code=self._pricing_resp_get(resp, "variant_code") or variant_code,
                sku_code=self._pricing_resp_get(resp, "variant_code") or variant_code,
                leaf_sku_code=self._pricing_resp_get(resp, "sku_code") or leaf_sku_code,
                actual_units=str(actual_units),
                billed_units=self._pricing_resp_get(resp, "billed_units") or str(actual_units),
                commit_status=commit_status,
                reservation_status=commit_status,
                ledger_entry_id=self._pricing_resp_get(resp, "ledger_entry_id"),
                amount=self._pricing_resp_get(resp, "amount"),
                currency=self._pricing_resp_get(resp, "currency"),
                billing_mode=self._pricing_resp_get(resp, "billing_mode") or pricing.get("billing_mode"),
                billing_account_id=self._pricing_resp_get(resp, "billing_account_id")
                or pricing.get("billing_account_id"),
                settlement_mode=self._pricing_resp_get(resp, "settlement_mode")
                or pricing.get("settlement_mode"),
                entitlement_source=self._pricing_resp_get(resp, "entitlement_source")
                or pricing.get("entitlement_source"),
                disabled_reason=None,
            )
            await self._persist_pricing_block(job_id, pricing)
            return pricing
        except Exception as e:
            logger.exception(
                "audio_pricing_commit_failed",
                extra={"job_id": job_id, "reservation_id": reservation_id, "user_id": user_id},
            )
            pricing = self._merge_pricing_block(
                pricing,
                state="commit_failed",
                actual_units=str(actual_units),
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

        if (not reservation_id) or (state not in {"reserved", "release_failed", "commit_failed"}):
            awaited = await self._await_reserved_pricing(job_id)
            if awaited:
                pricing = awaited
                reservation_id = str(pricing.get("reservation_id") or "").strip()
                state = str(pricing.get("state") or "").strip().lower()

        if not reservation_id:
            return pricing

        if state not in {"reserved", "release_failed", "commit_failed"}:
            return pricing

        variant_code = (
            str(pricing.get("variant_code") or pricing.get("sku_code") or self.VARIANT_CODE).strip()
            or self.VARIANT_CODE
        )
        leaf_sku_code = str(pricing.get("leaf_sku_code") or "").strip() or None

        release_meta = {
            "variant_code": variant_code,
            "sku_code": variant_code,
            "service_action": str(pricing.get("service_action") or "audio.tts.generate"),
            "chars_1k": str(pricing.get("estimated_units") or "1"),
        }
        if leaf_sku_code:
            release_meta["leaf_sku_code"] = leaf_sku_code

        try:
            resp = await self.pricing_client.release(
                PricingReleaseRequest(
                    user_id=str(user_id),
                    reservation_id=reservation_id,
                    reason=reason,
                    external_ref_type="studio_job",
                    external_ref_id=str(job_id),
                    idempotency_key=f"svc-audio:job:{job_id}:release",
                    meta=release_meta,
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
                billing_account_id=self._pricing_resp_get(resp, "billing_account_id")
                or pricing.get("billing_account_id"),
                settlement_mode=self._pricing_resp_get(resp, "settlement_mode")
                or pricing.get("settlement_mode"),
                entitlement_source=self._pricing_resp_get(resp, "entitlement_source")
                or pricing.get("entitlement_source"),
                disabled_reason=None,
            )
            await self._persist_pricing_block(job_id, pricing)
            return pricing
        except Exception as e:
            logger.exception(
                "audio_pricing_release_failed",
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
    # Create job + reserve pricing
    # -------------------------------------------------------------------------
    async def create_job(self, *, user_id: str, payload: Dict[str, Any]) -> str:
        payload = dict(payload or {})

        text = str(payload.get("text") or "").strip()
        target_locale = str(payload.get("target_locale") or "").strip()
        input_language = str(payload.get("input_language") or payload.get("source_language") or "en")
        output_format = str(payload.get("output_format") or "mp3")

        if not user_id:
            raise ValueError("user_id is required")
        if not text:
            raise ValueError("payload.text is required")
        if not target_locale:
            raise ValueError("payload.target_locale is required")

        request_hash_src = {
            "studio_type": self.STUDIO_TYPE,
            "user_id": str(user_id),
            "payload": payload,
        }
        request_hash = hashlib.sha256(
            json.dumps(
                request_hash_src,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

        pricing_enabled = self._pricing_enabled()
        if self._pricing_required() and not pricing_enabled:
            raise PricingClientError(f"PRICING_CLIENT_DISABLED: {self._pricing_disabled_reason()}")

        estimated_units = _chars_1k_units(text)

        pricing: Dict[str, Any] = {
            "enabled": pricing_enabled,
            "state": "pricing_pending" if pricing_enabled else "not_enabled",
            "service_name": "svc-audio",
            "service_action": "audio.tts.generate",
            "variant_code": self.VARIANT_CODE,
            "sku_code": self.VARIANT_CODE,
            "estimated_units": str(estimated_units),
            "units_kind": "chars_1k",
            "meta": {
                "target_locale": target_locale,
                "input_language": input_language,
                "output_format": output_format,
                "text_length": len(text),
            },
        }

        initial_status = "pricing_pending" if pricing_enabled else "queued"
        meta = {"pricing": pricing} if pricing_enabled else {}

        job_id = await self.jobs.insert_job(
            user_id=str(user_id),
            request_hash=request_hash,
            payload=payload,
            initial_status=initial_status,
            meta=meta,
        )

        if not pricing_enabled:
            return job_id

        existing = await self.jobs.get_job(job_id)
        if existing:
            existing_pricing = self._pricing_from_payload_meta(
                _jsonb_to_dict(existing.get("payload_json")),
                _jsonb_to_dict(existing.get("meta_json")),
            )
            existing_status = str(existing.get("status") or "").strip().lower()
            existing_reservation_id = str(existing_pricing.get("reservation_id") or "").strip()
            existing_state = str(existing_pricing.get("state") or "").strip().lower()

            if existing_reservation_id or existing_state in {
                "reserved",
                "committed",
                "released",
                "reservation_failed",
                "commit_failed",
                "release_failed",
                "blocked",
            }:
                return job_id

            if existing_status in {"queued", "running", "succeeded", "failed", "blocked"}:
                return job_id

        await self._persist_pricing_block(job_id, pricing)

        try:
            resp = await self.pricing_client.reserve(
                PricingReserveRequest(
                    user_id=str(user_id),
                    service_name="svc-audio",
                    service_action="audio.tts.generate",
                    sku_code=self.VARIANT_CODE,
                    units=str(estimated_units),
                    external_ref_type="studio_job",
                    external_ref_id=str(job_id),
                    idempotency_key=f"svc-audio:job:{job_id}:reserve",
                    meta={
                        "variant_code": self.VARIANT_CODE,
                        "sku_code": self.VARIANT_CODE,
                        "chars_1k": str(estimated_units),
                        "text_length": len(text),
                        "target_locale": target_locale,
                        "input_language": input_language,
                        "output_format": output_format,
                    },
                )
            )

            reserve_status = str(self._pricing_resp_get(resp, "status", "reserved") or "reserved")
            pricing = self._merge_pricing_block(
                pricing,
                state="reserved",
                reservation_status=reserve_status,
                reservation_id=self._pricing_resp_get(resp, "reservation_id"),
                variant_code=self._pricing_resp_get(resp, "variant_code") or self.VARIANT_CODE,
                sku_code=self._pricing_resp_get(resp, "variant_code") or self.VARIANT_CODE,
                leaf_sku_code=self._pricing_resp_get(resp, "sku_code"),
                billing_mode=self._pricing_resp_get(resp, "billing_mode"),
                billing_account_id=self._pricing_resp_get(resp, "billing_account_id"),
                settlement_mode=self._pricing_resp_get(resp, "settlement_mode"),
                entitlement_source=self._pricing_resp_get(resp, "entitlement_source"),
                amount=self._pricing_resp_get(resp, "amount"),
                currency=self._pricing_resp_get(resp, "currency"),
                disabled_reason=None,
                error=None,
                error_code=None,
            )
            await self._persist_pricing_block(job_id, pricing)
            await self.jobs.set_status(job_id, "queued")
            return job_id

        except Exception as e:
            code = _classify_error(e)
            failed_status = "blocked" if code == "PRICING_INSUFFICIENT_CREDITS" else "failed"

            pricing = self._merge_pricing_block(
                pricing,
                state="reservation_failed",
                error=str(e),
                error_code=code,
            )
            await self._persist_pricing_block(job_id, pricing)
            await self.jobs.set_status(
                job_id,
                failed_status,
                error_code=code,
                error_message=str(e),
            )

            if isinstance(e, PricingClientError):
                raise
            raise PricingClientError(str(e))

    # -------------------------------------------------------------------------
    # Main job execution
    # -------------------------------------------------------------------------
    async def process_job(self, job_id: str) -> None:
        """
        Flow:
          1) claim phase (tx): validate, mark running, upsert step running, increment attempt
          2) pricing gate: if pricing is enabled, require a completed reserve before execution
          3) execution: synthesize + upload (no DB conn held)
          4) finalize (tx): insert artifact, mark succeeded
          5) commit pricing
          6) on error: release pricing, mark failed + step failed
        """

        user_id: Optional[str] = None
        payload: Dict[str, Any] = {}
        text = ""
        target_locale = ""
        attempt_i = 0
        pricing: Dict[str, Any] = {}

        # ---------------------------
        # Claim phase
        # ---------------------------
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                job = await conn.fetchrow(
                    """
                    SELECT id::text,
                           user_id::text,
                           studio_type,
                           status,
                           payload_json,
                           meta_json,
                           attempt_count
                      FROM studio_jobs
                     WHERE id=$1::uuid
                    """,
                    job_id,
                )
                if not job:
                    return

                studio_type = (job["studio_type"] or "").strip()
                if studio_type != self.STUDIO_TYPE:
                    await self._fail_job_and_step(
                        conn,
                        job_id,
                        "wrong_studio_type",
                        f"expected studio_type={self.STUDIO_TYPE}, got={studio_type}",
                        attempt=0,
                    )
                    return

                status = (job["status"] or "").lower()
                if status in ("succeeded", "failed", "cancelled", "canceled", "blocked"):
                    return

                payload = _jsonb_to_dict(job["payload_json"])
                user_id = (job["user_id"] or "").strip()
                pricing = self._pricing_from_payload_meta(
                    _jsonb_to_dict(job["payload_json"]),
                    _jsonb_to_dict(job["meta_json"]),
                )

                text = (payload.get("text") or "").strip()
                target_locale = (payload.get("target_locale") or "").strip()

                if not user_id:
                    await self._fail_job_and_step(
                        conn,
                        job_id,
                        "missing_user_id",
                        "job.user_id is required",
                        attempt=0,
                    )
                    return
                if not text:
                    await self._fail_job_and_step(
                        conn,
                        job_id,
                        "missing_text",
                        "payload.text is required",
                        attempt=0,
                    )
                    return
                if not target_locale:
                    await self._fail_job_and_step(
                        conn,
                        job_id,
                        "missing_target_locale",
                        "payload.target_locale is required",
                        attempt=0,
                    )
                    return

                attempt_i = int(
                    await conn.fetchval(
                        """
                        UPDATE studio_jobs
                           SET status='running',
                               updated_at=now(),
                               attempt_count=attempt_count+1
                         WHERE id=$1::uuid
                         RETURNING attempt_count
                        """,
                        job_id,
                    )
                    or 0
                )

                await conn.execute(
                    """
                    INSERT INTO studio_job_steps(job_id, step_code, status, attempt, error_code, error_message, meta_json)
                    VALUES($1::uuid, $2::text, 'running', $3::int, NULL, NULL, '{}'::jsonb)
                    ON CONFLICT (job_id, step_code)
                    DO UPDATE SET
                      status='running',
                      attempt=EXCLUDED.attempt,
                      error_code=NULL,
                      error_message=NULL,
                      updated_at=now()
                    """,
                    job_id,
                    self.STEP_CODE,
                    attempt_i,
                )

        # ---------------------------
        # Pricing gate
        # ---------------------------
        if self._pricing_required() and not self._pricing_enabled():
            reason = self._pricing_disabled_reason()
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await self._fail_job_and_step(
                        conn,
                        job_id,
                        "PRICING_CLIENT_DISABLED",
                        reason,
                        attempt=attempt_i,
                    )
            return

        if self._pricing_enabled():
            latest_pricing = await self._await_reserved_pricing(job_id)
            if latest_pricing:
                pricing = latest_pricing

            if self._pricing_required() and not pricing:
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        await self._fail_job_and_step(
                            conn,
                            job_id,
                            "PRICING_NOT_RESERVED",
                            "Pricing block missing for pricing-enabled audio job",
                            attempt=attempt_i,
                        )
                return

            if pricing.get("enabled"):
                pricing_state = str(pricing.get("state") or "").strip().lower()
                reservation_id = str(pricing.get("reservation_id") or "").strip()
                pricing_error = str(pricing.get("error") or pricing.get("error_message") or "").strip()

                if pricing_state == "blocked" or (
                    str(pricing.get("error_code") or "").strip() == "PRICING_INSUFFICIENT_CREDITS"
                ):
                    async with self.pool.acquire() as conn:
                        async with conn.transaction():
                            await self._block_job_and_step(
                                conn,
                                job_id,
                                "PRICING_INSUFFICIENT_CREDITS",
                                pricing_error or "Insufficient credits to generate audio",
                                attempt=attempt_i,
                            )
                    return

                if pricing_state == "reservation_failed":
                    code = "PRICING_RESERVATION_FAILED"
                    if "PRICING_INSUFFICIENT_CREDITS" in pricing_error:
                        code = "PRICING_INSUFFICIENT_CREDITS"
                    async with self.pool.acquire() as conn:
                        async with conn.transaction():
                            if code == "PRICING_INSUFFICIENT_CREDITS":
                                await self._block_job_and_step(
                                    conn,
                                    job_id,
                                    code,
                                    pricing_error or "Insufficient credits to generate audio",
                                    attempt=attempt_i,
                                )
                            else:
                                await self._fail_job_and_step(
                                    conn,
                                    job_id,
                                    code,
                                    pricing_error or "Pricing reservation failed",
                                    attempt=attempt_i,
                                )
                    return

                if not reservation_id:
                    async with self.pool.acquire() as conn:
                        async with conn.transaction():
                            await self._fail_job_and_step(
                                conn,
                                job_id,
                                "PRICING_NOT_RESERVED",
                                "Pricing reservation did not complete before audio execution",
                                attempt=attempt_i,
                            )
                    return

        # ---------------------------
        # Execution phase
        # ---------------------------
        try:
            input_language = (payload.get("input_language") or payload.get("source_language") or "en")
            output_format = (payload.get("output_format") or "mp3")

            rate = _safe_float(payload.get("speed") or payload.get("rate"), 1.0)
            pitch = _safe_float(payload.get("pitch"), 0.0)

            audio_bytes, final_text, chosen_voice, content_type, ext, meta = await self.tts.synthesize(
                text=text,
                input_language=input_language,
                target_locale=target_locale,
                voice=payload.get("voice"),
                style=payload.get("style"),
                emotion=payload.get("emotion"),
                rate=rate,
                pitch=pitch,
                translate=bool(payload.get("translate", True)),
                output_format=output_format,
            )

            upload = await self.storage.upload_bytes(
                data=audio_bytes,
                user_id=user_id,
                job_id=job_id,
                variant=1,
                ext=ext,
                content_type=content_type,
            )

            sas_url, storage_path, sha256, nbytes = _upload_fields(upload)

            if not sas_url:
                raise RuntimeError(f"upload_missing_sas_url: type={type(upload)} upload={upload!r}")

            translated_text = None
            if isinstance(meta, dict):
                translated_text = meta.get("translated_text") or meta.get("translation") or None

            payload_updates: Dict[str, Any] = {
                "voice": chosen_voice,
                "final_synthesis_text": final_text,
            }
            if translated_text:
                payload_updates["translated_text"] = translated_text
            if isinstance(meta, dict) and meta:
                payload_updates["tts_meta"] = meta

            payload_merged = dict(payload)
            payload_merged.update(payload_updates)

            artifact_meta = {
                "variant": 1,
                "ext": ext,
                "storage_path": storage_path,
                "voice": chosen_voice,
                "final_text": final_text,
                "target_locale": target_locale,
                "attempt": attempt_i,
            }
            if isinstance(meta, dict) and meta:
                artifact_meta.update(meta)

            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE studio_jobs
                           SET payload_json=$2::jsonb,
                               updated_at=now()
                         WHERE id=$1::uuid
                        """,
                        job_id,
                        json.dumps(payload_merged),
                    )

                    await conn.execute(
                        """
                        INSERT INTO artifacts(job_id, kind, url, content_type, sha256, bytes, meta_json)
                        VALUES($1::uuid, 'audio', $2::text, $3::text, $4::text, $5::bigint, $6::jsonb)
                        """,
                        job_id,
                        sas_url,
                        content_type,
                        sha256,
                        nbytes,
                        json.dumps(artifact_meta),
                    )

                    await conn.execute(
                        """
                        UPDATE studio_jobs
                           SET status='succeeded',
                               updated_at=now(),
                               error_code=NULL,
                               error_message=NULL
                         WHERE id=$1::uuid
                        """,
                        job_id,
                    )

                    await conn.execute(
                        """
                        UPDATE studio_job_steps
                           SET status='succeeded',
                               error_code=NULL,
                               error_message=NULL,
                               updated_at=now()
                         WHERE job_id=$1::uuid AND step_code=$2::text
                        """,
                        job_id,
                        self.STEP_CODE,
                    )

            pricing = await self._commit_pricing_for_job(
                job_id=job_id,
                user_id=str(user_id),
                pricing=pricing,
                final_text=final_text,
            )

        except Exception as e:
            msg = str(e)
            code = _classify_error(e)
            logger.exception("TTS job failed job_id=%s err=%s", job_id, msg)

            try:
                pricing = await self._release_pricing_for_job(
                    job_id=job_id,
                    user_id=str(user_id or ""),
                    pricing=pricing,
                    reason=code.lower(),
                )
            except Exception:
                logger.exception("audio_pricing_release_in_except_failed", extra={"job_id": job_id})

            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE studio_jobs
                           SET status='failed',
                               updated_at=now(),
                               error_code=$2::text,
                               error_message=$3::text
                         WHERE id=$1::uuid
                        """,
                        job_id,
                        code,
                        msg,
                    )

                    await conn.execute(
                        """
                        INSERT INTO studio_job_steps(job_id, step_code, status, attempt, error_code, error_message, meta_json)
                        VALUES($1::uuid, $2::text, 'failed', $3::int, $4::text, $5::text,
                               jsonb_build_object('error', $5::text))
                        ON CONFLICT (job_id, step_code)
                        DO UPDATE SET
                          status='failed',
                          attempt=EXCLUDED.attempt,
                          error_code=EXCLUDED.error_code,
                          error_message=EXCLUDED.error_message,
                          meta_json=studio_job_steps.meta_json || EXCLUDED.meta_json,
                          updated_at=now()
                        """,
                        job_id,
                        self.STEP_CODE,
                        attempt_i,
                        code,
                        msg,
                    )
            raise

    async def _block_job_and_step(
        self,
        conn: asyncpg.Connection,
        job_id: str,
        code: str,
        message: str,
        *,
        attempt: int,
    ) -> None:
        await conn.execute(
            """
            UPDATE studio_jobs
               SET status='blocked',
                   updated_at=now(),
                   error_code=$2::text,
                   error_message=$3::text
             WHERE id=$1::uuid
            """,
            job_id,
            code,
            message,
        )

        await conn.execute(
            """
            INSERT INTO studio_job_steps(job_id, step_code, status, attempt, error_code, error_message, meta_json)
            VALUES($1::uuid, $2::text, 'blocked', $3::int, $4::text, $5::text,
                   jsonb_build_object('error', $5::text))
            ON CONFLICT (job_id, step_code)
            DO UPDATE SET
              status='blocked',
              attempt=EXCLUDED.attempt,
              error_code=EXCLUDED.error_code,
              error_message=EXCLUDED.error_message,
              meta_json=studio_job_steps.meta_json || EXCLUDED.meta_json,
              updated_at=now()
            """,
            job_id,
            self.STEP_CODE,
            int(attempt),
            code,
            message,
        )

    async def _fail_job_and_step(
        self,
        conn: asyncpg.Connection,
        job_id: str,
        code: str,
        message: str,
        *,
        attempt: int,
    ) -> None:
        await conn.execute(
            """
            UPDATE studio_jobs
               SET status='failed',
                   updated_at=now(),
                   error_code=$2::text,
                   error_message=$3::text
             WHERE id=$1::uuid
            """,
            job_id,
            code,
            message,
        )

        await conn.execute(
            """
            INSERT INTO studio_job_steps(job_id, step_code, status, attempt, error_code, error_message, meta_json)
            VALUES($1::uuid, $2::text, 'failed', $3::int, $4::text, $5::text,
                   jsonb_build_object('error', $5::text))
            ON CONFLICT (job_id, step_code)
            DO UPDATE SET
              status='failed',
              attempt=EXCLUDED.attempt,
              error_code=EXCLUDED.error_code,
              error_message=EXCLUDED.error_message,
              meta_json=studio_job_steps.meta_json || EXCLUDED.meta_json,
              updated_at=now()
            """,
            job_id,
            self.STEP_CODE,
            int(attempt),
            code,
            message,
        )