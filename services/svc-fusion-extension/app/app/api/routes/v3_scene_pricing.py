from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_current_user_id, get_db_pool_dep as get_db_pool
from app.config import settings
from app.services.sas_service import AzureBlobService
from app.services.stitch_service import probe_duration_seconds
from desifaces_shared.identity import AccountContextNotFound, resolve_account_context
from desifaces_shared.pricing.client import PricingClientError, SvcPricingClient
from desifaces_shared.pricing.orchestration import (
    PricingCommitSpec,
    PricingPreviewSpec,
    PricingReleaseSpec,
    PricingReserveSpec,
    build_commit_request,
    build_preview_request,
    build_pricing_summary,
    build_release_request,
    build_reserve_request,
    make_committed_artifact,
    make_preview_artifact,
    make_released_artifact,
    make_reserved_artifact,
)

router = APIRouter(prefix="/api/longform/v3/scene-pricing", tags=["longform-v3-scene-pricing"])

_SERVICE_NAME = "svc-fusion"
_SERVICE_ACTION = "fusion.video.generate"
_VARIANT_CODE = "FUSION_TALKING_VIDEO"
_LEAF_SKU_CODE = "FUSION_TALK_MIN"
_PROVIDER = "veed_fabric"
_PRICING_KEY = "fusion_parent_pricing"
_MAX_PROBE_CONCURRENCY = 8


class ScenePricingKey(BaseModel):
    project_id: UUID
    workflow_id: UUID
    stage_run_id: UUID


class ScenePricingReserveIn(ScenePricingKey):
    quote_id: str = Field(min_length=1, max_length=300)
    preview_fingerprint: str = Field(min_length=1, max_length=500)


class ScenePricingReleaseIn(ScenePricingKey):
    reason: str = Field(default="fusion_scene_failed", min_length=1, max_length=300)


class ScenePricingOut(BaseModel):
    workflow_id: UUID
    stage_run_id: UUID
    scene_id: UUID
    turn_count: int
    duration_source: str
    total_audio_duration_sec: float
    billable_minutes: int
    audio_lineage_hash: str
    provider: str
    pricing: dict[str, Any] = Field(default_factory=dict)
    pricing_summary: dict[str, Any] = Field(default_factory=dict)
    audio_durations: list[dict[str, Any]] = Field(default_factory=list)


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _billable_minutes(total_seconds: float) -> int:
    return max(1, int(math.ceil(max(float(total_seconds), 0.001) / 60.0)))


def _parse_expiry(value: Any) -> datetime | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pricing_cycle_token(stored: dict[str, Any]) -> str:
    source = "|".join(
        (
            _clean(stored.get("quote_id")),
            _clean(stored.get("preview_fingerprint")),
            _clean(stored.get("quote_expires_at")),
            _clean(stored.get("audio_lineage_hash")),
        )
    )
    if not source.strip("|"):
        raise HTTPException(status_code=409, detail="scene_pricing_preview_cycle_missing")
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]


def _line_unit(pricing: dict[str, Any]) -> str:
    lines = list(_as_dict(pricing.get("meta")).get("lines") or [])
    units = {
        _clean(_as_dict(line).get("unit")).lower()
        for line in lines
        if _clean(_as_dict(line).get("unit"))
    }
    if len(units) == 1:
        return next(iter(units))
    return "composite" if len(units) > 1 else ""


def _provider_hints(pricing: dict[str, Any]) -> set[str]:
    return {
        _clean(_as_dict(line).get("provider_hint")).lower()
        for line in list(_as_dict(pricing.get("meta")).get("lines") or [])
        if _clean(_as_dict(line).get("provider_hint"))
    }


def _storage_location(row: asyncpg.Record) -> tuple[str, str]:
    meta = _as_dict(row["meta_json"])
    container = _clean(meta.get("storage_container"))
    blob_name = _clean(meta.get("storage_path") or meta.get("blob_name"))
    if container and blob_name:
        return container, blob_name.lstrip("/")
    storage_ref = _clean(row["storage_ref"])
    if storage_ref.startswith("azure://"):
        remainder = storage_ref[len("azure://") :]
        container, sep, blob_name = remainder.partition("/")
        if sep and container and blob_name:
            return container, blob_name.lstrip("/")
    raise HTTPException(status_code=409, detail="scene_pricing_audio_storage_lineage_missing")


def _sign_audio(container: str, blob_name: str) -> str:
    return AzureBlobService(settings.AZURE_STORAGE_CONNECTION_STRING).sign_read_url(
        container,
        blob_name,
        900,
    )


async def _resolve_account_or_401(conn, user_id: UUID):
    try:
        return await resolve_account_context(conn, user_id)
    except AccountContextNotFound as exc:
        raise HTTPException(status_code=401, detail="account_context_not_found") from exc


async def _load_scene(conn, *, body: ScenePricingKey, account_id: UUID):
    row = await conn.fetchrow(
        """
        select w.workflow_id,w.account_id,w.owner_user_id,w.project_id,w.current_stage,
               s.stage_run_id,s.stage_type,s.scope_type,s.scene_id,s.state,s.metadata_json
        from public.v3_studio_workflows w
        join public.v3_studio_stage_runs s on s.workflow_id=w.workflow_id
        where w.workflow_id=$1 and w.project_id=$2 and w.account_id=$3
          and s.stage_run_id=$4
        """,
        body.workflow_id,
        body.project_id,
        account_id,
        body.stage_run_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="scene_pricing_workflow_stage_not_found")
    if _clean(row["stage_type"]) != "fusion" or _clean(row["scope_type"]) != "scene":
        raise HTTPException(status_code=409, detail="scene_pricing_requires_fusion_scene_stage")
    return row


async def _approved_audio_rows(
    conn,
    *,
    scene_id: UUID,
    workflow_id: UUID,
    account_id: UUID,
    project_id: UUID,
):
    speech_count = int(
        await conn.fetchval(
            "select count(*) from public.v3_dialogue_turns where scene_id=$1 and turn_kind='speech'",
            scene_id,
        )
        or 0
    )
    rows = await conn.fetch(
        """
        select dt.turn_id,dt.sequence_no,ao.media_id,ma.meta_json,ma.storage_ref
        from public.v3_dialogue_turns dt
        join public.v3_studio_stage_runs a
          on a.workflow_id=$1 and a.stage_type='audio' and a.scope_type='dialogue_turn'
         and a.dialogue_turn_id=dt.turn_id and a.state='approved'
        join public.v3_studio_stage_outputs ao
          on ao.stage_run_id=a.stage_run_id and ao.is_active=true
        join public.v3_studio_review_items ar
          on ar.stage_run_id=a.stage_run_id and ar.media_id=ao.media_id and ar.decision='approved'
        join public.media_assets ma
          on ma.id=ao.media_id and ma.account_id=$3 and ma.project_id=$4
         and ma.kind='audio' and ma.lifecycle_state='active'
        where dt.scene_id=$2 and dt.turn_kind='speech'
        order by dt.sequence_no,dt.turn_id
        """,
        workflow_id,
        scene_id,
        account_id,
        project_id,
    )
    if speech_count <= 0:
        raise HTTPException(status_code=409, detail="scene_pricing_requires_speech_turns")
    if len(rows) != speech_count:
        raise HTTPException(
            status_code=409,
            detail=f"scene_pricing_requires_all_audio_approved:{len(rows)}/{speech_count}",
        )
    return rows


async def _measure_audio_rows(rows) -> tuple[list[dict[str, Any]], float, str]:
    semaphore = asyncio.Semaphore(_MAX_PROBE_CONCURRENCY)

    async def measure(row) -> dict[str, Any]:
        container, blob_name = _storage_location(row)
        async with semaphore:
            duration = await asyncio.to_thread(
                probe_duration_seconds,
                _sign_audio(container, blob_name),
            )
        if duration is None or float(duration) <= 0:
            raise HTTPException(
                status_code=409,
                detail=f"scene_pricing_audio_duration_probe_failed:{row['media_id']}",
            )
        return {
            "dialogue_turn_id": str(row["turn_id"]),
            "sequence_no": int(row["sequence_no"]),
            "media_id": str(row["media_id"]),
            "duration_sec": round(float(duration), 3),
        }

    measured = await asyncio.gather(*(measure(row) for row in rows))
    measured.sort(key=lambda item: (int(item["sequence_no"]), item["dialogue_turn_id"]))
    total = round(sum(float(item["duration_sec"]) for item in measured), 3)
    lineage = [
        {
            "dialogue_turn_id": item["dialogue_turn_id"],
            "media_id": item["media_id"],
            "duration_ms": int(round(float(item["duration_sec"]) * 1000.0)),
        }
        for item in measured
    ]
    digest = hashlib.sha256(
        json.dumps(lineage, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return measured, total, digest


async def _scene_measurement(conn, *, body: ScenePricingKey, account_id: UUID):
    scene = await _load_scene(conn, body=body, account_id=account_id)
    rows = await _approved_audio_rows(
        conn,
        scene_id=UUID(str(scene["scene_id"])),
        workflow_id=body.workflow_id,
        account_id=account_id,
        project_id=body.project_id,
    )
    measured, total, lineage_hash = await _measure_audio_rows(rows)
    return scene, measured, total, _billable_minutes(total), lineage_hash


def _pricing_meta(
    *,
    body: ScenePricingKey,
    scene_id: UUID,
    measured: list[dict[str, Any]],
    total: float,
    minutes: int,
    lineage_hash: str,
) -> dict[str, Any]:
    return {
        "mode": "fusion_scene_parent",
        "provider": _PROVIDER,
        "provider_hint": _PROVIDER,
        "workflow_id": str(body.workflow_id),
        "scene_id": str(scene_id),
        "stage_run_id": str(body.stage_run_id),
        "project_id": str(body.project_id),
        "audio_duration_source": "approved_audio_ffprobe",
        "actual_audio_duration_sec": str(total),
        "minutes": str(minutes),
        "requested_units": str(minutes),
        "unit_type": "minute",
        "variant_code": _VARIANT_CODE,
        "leaf_sku_code": _LEAF_SKU_CODE,
        "child_count": len(measured),
        "audio_lineage_hash": lineage_hash,
        "channel": "service",
    }


def _normalize_parent_pricing(
    pricing: dict[str, Any],
    *,
    minutes: int,
    meta: dict[str, Any],
) -> dict[str, Any]:
    out = dict(pricing or {})
    out.update(
        {
            "enabled": True,
            "service_name": _SERVICE_NAME,
            "service_action": _SERVICE_ACTION,
            "sku_code": _VARIANT_CODE,
            "variant_code": _VARIANT_CODE,
            "leaf_sku_code": _LEAF_SKU_CODE,
            "unit_type": "minute",
            "estimated_units": str(minutes),
            "provider": _PROVIDER,
            "meta": {**_as_dict(out.get("meta")), **meta},
        }
    )
    return out


def _validate_pricebook_contract(pricing: dict[str, Any]) -> None:
    unit = _line_unit(pricing)
    if unit != "minute":
        raise HTTPException(
            status_code=409,
            detail=f"scene_pricing_unit_contract_invalid:{unit or 'missing'}",
        )
    stale = {
        hint
        for hint in _provider_hints(pricing)
        if hint not in {_PROVIDER, "provider-neutral", "neutral"}
    }
    if stale:
        raise HTTPException(
            status_code=409,
            detail="scene_pricing_stale_provider_hint:" + ",".join(sorted(stale)),
        )


def _preview_record(
    *,
    pricing: dict[str, Any],
    pricing_summary: dict[str, Any],
    measured: list[dict[str, Any]],
    total: float,
    minutes: int,
    lineage_hash: str,
) -> dict[str, Any]:
    return {
        "state": "quoted",
        "quote_id": _clean(pricing.get("quote_id")),
        "preview_fingerprint": _clean(pricing.get("preview_fingerprint")),
        "quote_expires_at": _clean(pricing.get("quote_expires_at")),
        "reservation_id": None,
        "estimated_units": str(minutes),
        "unit_type": "minute",
        "provider": _PROVIDER,
        "turn_count": len(measured),
        "total_audio_duration_sec": total,
        "audio_lineage_hash": lineage_hash,
        "pricing": pricing,
        "pricing_summary": pricing_summary,
    }


async def _persist_parent_pricing(conn, *, stage_run_id: UUID, record: dict[str, Any]) -> None:
    await conn.execute(
        """
        update public.v3_studio_stage_runs
        set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
        where stage_run_id=$1
        """,
        stage_run_id,
        json.dumps({_PRICING_KEY: record}),
    )


def _stored_parent_pricing(scene) -> dict[str, Any]:
    return _as_dict(_as_dict(scene["metadata_json"]).get(_PRICING_KEY))


def _assert_preview_confirmation(
    stored: dict[str, Any],
    *,
    body: ScenePricingReserveIn,
    minutes: int,
    lineage_hash: str,
    turn_count: int,
) -> None:
    if _clean(stored.get("quote_id")) != _clean(body.quote_id):
        raise HTTPException(status_code=409, detail="scene_pricing_quote_id_mismatch")
    if _clean(stored.get("preview_fingerprint")) != _clean(body.preview_fingerprint):
        raise HTTPException(status_code=409, detail="scene_pricing_preview_fingerprint_mismatch")
    expiry = _parse_expiry(stored.get("quote_expires_at"))
    if expiry is None or expiry <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="scene_pricing_quote_expired")
    if int(stored.get("estimated_units") or 0) != int(minutes):
        raise HTTPException(status_code=409, detail="scene_pricing_units_changed_since_preview")
    if _clean(stored.get("audio_lineage_hash")) != lineage_hash:
        raise HTTPException(status_code=409, detail="scene_pricing_audio_lineage_changed_since_preview")
    if int(stored.get("turn_count") or 0) != int(turn_count):
        raise HTTPException(status_code=409, detail="scene_pricing_turn_count_changed_since_preview")


def _pricing_client() -> SvcPricingClient:
    client = SvcPricingClient.from_env(service_name="svc-fusion-extension")
    if not client.enabled:
        raise HTTPException(status_code=503, detail="scene_pricing_client_disabled")
    return client


def _out(
    *,
    scene_id: UUID,
    body: ScenePricingKey,
    measured: list[dict[str, Any]],
    total: float,
    minutes: int,
    lineage_hash: str,
    pricing: dict[str, Any],
    pricing_summary: dict[str, Any],
) -> ScenePricingOut:
    return ScenePricingOut(
        workflow_id=body.workflow_id,
        stage_run_id=body.stage_run_id,
        scene_id=scene_id,
        turn_count=len(measured),
        duration_source="approved_audio_ffprobe",
        total_audio_duration_sec=total,
        billable_minutes=minutes,
        audio_lineage_hash=lineage_hash,
        provider=_PROVIDER,
        pricing=pricing,
        pricing_summary=pricing_summary,
        audio_durations=measured,
    )


@router.post("/preview", response_model=ScenePricingOut)
async def preview_scene_pricing(
    body: ScenePricingKey,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> ScenePricingOut:
    canonical_user_id = UUID(str(user_id))
    async with pool.acquire() as conn:
        account = await _resolve_account_or_401(conn, canonical_user_id)
        scene, measured, total, minutes, lineage_hash = await _scene_measurement(
            conn,
            body=body,
            account_id=account.account_id,
        )
        if _clean(scene["current_stage"]) != "fusion":
            raise HTTPException(status_code=409, detail="scene_pricing_workflow_not_at_fusion")
        meta = _pricing_meta(
            body=body,
            scene_id=UUID(str(scene["scene_id"])),
            measured=measured,
            total=total,
            minutes=minutes,
            lineage_hash=lineage_hash,
        )
        request_fingerprint = hashlib.sha256(
            json.dumps(meta, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        spec = PricingPreviewSpec(
            user_id=str(canonical_user_id),
            service_name=_SERVICE_NAME,
            service_action=_SERVICE_ACTION,
            sku_code=_VARIANT_CODE,
            units=str(minutes),
            external_ref_type="v3_scene_stage_preview",
            external_ref_id=str(body.stage_run_id),
            idempotency_key=(
                f"svc-fusion-extension:v3-scene:{body.stage_run_id}:"
                f"preview:{request_fingerprint}"
            ),
            meta=meta,
        )
        try:
            response = await _pricing_client().preview(build_preview_request(spec))
        except PricingClientError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"scene_pricing_preview_failed:{str(exc)[:1200]}",
            ) from exc
        artifact = make_preview_artifact(
            response,
            service_name=_SERVICE_NAME,
            service_action=_SERVICE_ACTION,
            sku_code=_VARIANT_CODE,
            meta=meta,
        )
        pricing = _normalize_parent_pricing(
            dict(artifact.get("pricing") or {}),
            minutes=minutes,
            meta=meta,
        )
        _validate_pricebook_contract(pricing)
        if not _clean(pricing.get("quote_id")) or not _clean(pricing.get("preview_fingerprint")):
            raise HTTPException(
                status_code=409,
                detail="scene_pricing_preview_missing_confirmation_contract",
            )
        pricing_summary = build_pricing_summary(pricing)
        record = _preview_record(
            pricing=pricing,
            pricing_summary=pricing_summary,
            measured=measured,
            total=total,
            minutes=minutes,
            lineage_hash=lineage_hash,
        )
        await _persist_parent_pricing(conn, stage_run_id=body.stage_run_id, record=record)
        return _out(
            scene_id=UUID(str(scene["scene_id"])),
            body=body,
            measured=measured,
            total=total,
            minutes=minutes,
            lineage_hash=lineage_hash,
            pricing=pricing,
            pricing_summary=pricing_summary,
        )


@router.post("/reserve", response_model=ScenePricingOut)
async def reserve_scene_pricing(
    body: ScenePricingReserveIn,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> ScenePricingOut:
    canonical_user_id = UUID(str(user_id))
    async with pool.acquire() as conn:
        account = await _resolve_account_or_401(conn, canonical_user_id)
        scene, measured, total, minutes, lineage_hash = await _scene_measurement(
            conn,
            body=body,
            account_id=account.account_id,
        )
        stored = _stored_parent_pricing(scene)
        if (
            _clean(stored.get("state")) in {"reserved", "committed"}
            and _clean(stored.get("quote_id")) == _clean(body.quote_id)
        ):
            return _out(
                scene_id=UUID(str(scene["scene_id"])),
                body=body,
                measured=measured,
                total=total,
                minutes=minutes,
                lineage_hash=lineage_hash,
                pricing=_as_dict(stored.get("pricing")),
                pricing_summary=_as_dict(stored.get("pricing_summary")),
            )

        _assert_preview_confirmation(
            stored,
            body=body,
            minutes=minutes,
            lineage_hash=lineage_hash,
            turn_count=len(measured),
        )
        base_pricing = _as_dict(stored.get("pricing"))
        meta = {
            **_as_dict(base_pricing.get("meta")),
            "reservation_owner": "v3_scene_parent",
        }
        cycle_token = _pricing_cycle_token(stored)
        spec = PricingReserveSpec(
            user_id=str(canonical_user_id),
            service_name=_SERVICE_NAME,
            service_action=_SERVICE_ACTION,
            sku_code=_VARIANT_CODE,
            units=str(minutes),
            external_ref_type="v3_scene_stage",
            external_ref_id=str(body.stage_run_id),
            idempotency_key=(
                f"svc-fusion-extension:v3-scene:{body.stage_run_id}:"
                f"reserve:{cycle_token}"
            ),
            meta=meta,
            quote_id=body.quote_id,
            preview_fingerprint=body.preview_fingerprint,
        )
        try:
            response = await _pricing_client().reserve(build_reserve_request(spec))
        except PricingClientError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"scene_pricing_reserve_failed:{str(exc)[:1200]}",
            ) from exc
        artifact = make_reserved_artifact(
            response,
            service_name=_SERVICE_NAME,
            service_action=_SERVICE_ACTION,
            sku_code=_VARIANT_CODE,
            estimated_units=str(minutes),
            estimated_amount=_clean(base_pricing.get("estimated_amount")) or None,
            currency=_clean(base_pricing.get("currency")) or None,
            unit_type="minute",
            meta=meta,
        )
        pricing = _normalize_parent_pricing(
            dict(artifact.get("pricing") or {}),
            minutes=minutes,
            meta=meta,
        )
        pricing["state"] = "reserved"
        pricing["quote_id"] = body.quote_id
        pricing["preview_fingerprint"] = body.preview_fingerprint
        pricing["quote_expires_at"] = _clean(stored.get("quote_expires_at")) or None
        reservation_id = _clean(pricing.get("reservation_id"))
        if not reservation_id:
            raise HTTPException(
                status_code=409,
                detail="scene_pricing_reserve_missing_reservation_id",
            )
        pricing_summary = build_pricing_summary(pricing)
        record = {
            **stored,
            "state": "reserved",
            "reservation_id": reservation_id,
            "pricing": pricing,
            "pricing_summary": pricing_summary,
        }
        await _persist_parent_pricing(conn, stage_run_id=body.stage_run_id, record=record)
        return _out(
            scene_id=UUID(str(scene["scene_id"])),
            body=body,
            measured=measured,
            total=total,
            minutes=minutes,
            lineage_hash=lineage_hash,
            pricing=pricing,
            pricing_summary=pricing_summary,
        )


@router.post("/commit", response_model=ScenePricingOut)
async def commit_scene_pricing(
    body: ScenePricingKey,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> ScenePricingOut:
    canonical_user_id = UUID(str(user_id))
    async with pool.acquire() as conn:
        account = await _resolve_account_or_401(conn, canonical_user_id)
        scene, measured, total, minutes, lineage_hash = await _scene_measurement(
            conn,
            body=body,
            account_id=account.account_id,
        )
        stored = _stored_parent_pricing(scene)
        if _clean(stored.get("state")) == "committed":
            return _out(
                scene_id=UUID(str(scene["scene_id"])),
                body=body,
                measured=measured,
                total=total,
                minutes=minutes,
                lineage_hash=lineage_hash,
                pricing=_as_dict(stored.get("pricing")),
                pricing_summary=_as_dict(stored.get("pricing_summary")),
            )
        reservation_id = _clean(
            stored.get("reservation_id")
            or _as_dict(stored.get("pricing")).get("reservation_id")
        )
        if not reservation_id:
            raise HTTPException(
                status_code=409,
                detail="scene_pricing_commit_requires_reservation",
            )
        if (
            int(stored.get("estimated_units") or 0) != minutes
            or _clean(stored.get("audio_lineage_hash")) != lineage_hash
        ):
            raise HTTPException(status_code=409, detail="scene_pricing_commit_input_changed")

        base_pricing = _as_dict(stored.get("pricing"))
        meta = {**_as_dict(base_pricing.get("meta")), "commit_owner": "v3_scene_parent"}
        spec = PricingCommitSpec(
            user_id=str(canonical_user_id),
            reservation_id=reservation_id,
            actual_units=str(minutes),
            external_ref_type="v3_scene_stage",
            external_ref_id=str(body.stage_run_id),
            idempotency_key=(
                f"svc-fusion-extension:v3-scene:{body.stage_run_id}:"
                f"commit:{reservation_id}"
            ),
            meta=meta,
        )
        try:
            response = await _pricing_client().commit(build_commit_request(spec))
        except PricingClientError as exc:
            pending = {
                **stored,
                "state": "commit_pending",
                "last_error": str(exc)[:1200],
            }
            await _persist_parent_pricing(
                conn,
                stage_run_id=body.stage_run_id,
                record=pending,
            )
            raise HTTPException(
                status_code=409,
                detail=f"scene_pricing_commit_failed:{str(exc)[:1200]}",
            ) from exc

        artifact = make_committed_artifact(
            response,
            base_pricing=base_pricing,
            actual_units=str(minutes),
            meta=meta,
        )
        pricing = _normalize_parent_pricing(
            dict(artifact.get("pricing") or {}),
            minutes=minutes,
            meta=meta,
        )
        pricing["state"] = "committed"
        pricing_summary = build_pricing_summary(pricing)
        record = {
            **stored,
            "state": "committed",
            "pricing": pricing,
            "pricing_summary": pricing_summary,
            "last_error": None,
        }
        await _persist_parent_pricing(conn, stage_run_id=body.stage_run_id, record=record)
        return _out(
            scene_id=UUID(str(scene["scene_id"])),
            body=body,
            measured=measured,
            total=total,
            minutes=minutes,
            lineage_hash=lineage_hash,
            pricing=pricing,
            pricing_summary=pricing_summary,
        )


@router.post("/release", response_model=ScenePricingOut)
async def release_scene_pricing(
    body: ScenePricingReleaseIn,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> ScenePricingOut:
    canonical_user_id = UUID(str(user_id))
    async with pool.acquire() as conn:
        account = await _resolve_account_or_401(conn, canonical_user_id)
        scene, measured, total, minutes, lineage_hash = await _scene_measurement(
            conn,
            body=body,
            account_id=account.account_id,
        )
        stored = _stored_parent_pricing(scene)
        if _clean(stored.get("state")) == "committed":
            raise HTTPException(
                status_code=409,
                detail="scene_pricing_committed_cannot_release",
            )
        if _clean(stored.get("state")) == "released":
            return _out(
                scene_id=UUID(str(scene["scene_id"])),
                body=body,
                measured=measured,
                total=total,
                minutes=minutes,
                lineage_hash=lineage_hash,
                pricing=_as_dict(stored.get("pricing")),
                pricing_summary=_as_dict(stored.get("pricing_summary")),
            )

        reservation_id = _clean(
            stored.get("reservation_id")
            or _as_dict(stored.get("pricing")).get("reservation_id")
        )
        if not reservation_id:
            pricing = _as_dict(stored.get("pricing"))
            pricing["state"] = "released"
            summary = build_pricing_summary(pricing)
            await _persist_parent_pricing(
                conn,
                stage_run_id=body.stage_run_id,
                record={
                    **stored,
                    "state": "released",
                    "pricing": pricing,
                    "pricing_summary": summary,
                    "release_reason": body.reason,
                },
            )
            return _out(
                scene_id=UUID(str(scene["scene_id"])),
                body=body,
                measured=measured,
                total=total,
                minutes=minutes,
                lineage_hash=lineage_hash,
                pricing=pricing,
                pricing_summary=summary,
            )

        base_pricing = _as_dict(stored.get("pricing"))
        meta = {**_as_dict(base_pricing.get("meta")), "release_owner": "v3_scene_parent"}
        spec = PricingReleaseSpec(
            user_id=str(canonical_user_id),
            reservation_id=reservation_id,
            reason=body.reason,
            external_ref_type="v3_scene_stage",
            external_ref_id=str(body.stage_run_id),
            idempotency_key=(
                f"svc-fusion-extension:v3-scene:{body.stage_run_id}:"
                f"release:{reservation_id}"
            ),
            meta=meta,
        )
        try:
            response = await _pricing_client().release(build_release_request(spec))
        except PricingClientError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"scene_pricing_release_failed:{str(exc)[:1200]}",
            ) from exc
        artifact = make_released_artifact(
            response,
            base_pricing=base_pricing,
            meta=meta,
        )
        pricing = _normalize_parent_pricing(
            dict(artifact.get("pricing") or {}),
            minutes=minutes,
            meta=meta,
        )
        pricing["state"] = "released"
        pricing_summary = build_pricing_summary(pricing)
        record = {
            **stored,
            "state": "released",
            "pricing": pricing,
            "pricing_summary": pricing_summary,
            "release_reason": body.reason,
        }
        await _persist_parent_pricing(conn, stage_run_id=body.stage_run_id, record=record)
        return _out(
            scene_id=UUID(str(scene["scene_id"])),
            body=body,
            measured=measured,
            total=total,
            minutes=minutes,
            lineage_hash=lineage_hash,
            pricing=pricing,
            pricing_summary=pricing_summary,
        )


__all__ = [
    "ScenePricingKey",
    "ScenePricingOut",
    "_billable_minutes",
    "_line_unit",
    "_provider_hints",
    "_pricing_cycle_token",
    "_assert_preview_confirmation",
    "router",
]
