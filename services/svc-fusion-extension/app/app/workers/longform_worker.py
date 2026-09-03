from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request

from tenacity import RetryError
from azure.storage.blob import BlobServiceClient, ContentSettings

from app.config import settings
from app.db import get_db_pool
from app.domain.enums import RenderRoute
from app.http_clients.audio_client import create_tts_audio_blocking
from app.http_clients.fusion_client import create_fusion_job, get_fusion_job, _pick_video_url
from app.repos.longform_segments_repo import LongformSegmentsRepo
from app.repos.longform_jobs_repo import LongformJobsRepo
from app.services.sas_service import AzureBlobService, parse_blob_path_from_sas_url
from app.services.longform_orchestrator import release_longform_pricing_for_job, stitch_if_ready
from app.services.stitch_service import (
    attach_audio_track,
    download_to_local,
    probe_duration_seconds,
    render_mixed_montage_segment,
    render_text_card,
    upload_final_mp4,
    compose_presenter_with_motion_background,
)

logger = logging.getLogger("svc_fusion_extension.longform_worker")
segs_repo = LongformSegmentsRepo()
jobs_repo = LongformJobsRepo()


def _preview_url(url: Optional[str], keep: int = 96) -> Optional[str]:
    if not url:
        return None
    s = str(url).strip()
    if len(s) <= keep:
        return s
    return s[:keep] + "..."



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


async def _set_segment_runtime_status(
    conn,
    *,
    seg_id: str,
    status: str,
    fusion_job_id: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    await conn.execute(
        """
        update public.longform_segments
        set
          status = $2::text,
          fusion_job_id = coalesce($3::uuid, fusion_job_id),
          error_message = case when nullif($4::text, '') is null then error_message else $4::text end
        where id = $1::uuid
        """,
        seg_id,
        status,
        fusion_job_id,
        error_message or '',
    )


def _talking_provider_candidates(segment_plan: Dict[str, Any], tags: Dict[str, Any]) -> List[str]:
    provider_options = _as_dict_loose(segment_plan.get('provider_options'))
    quality_tier = _quality_tier_from_tags(tags, segment_plan)
    provider_hint = _provider_hint_from_tags(tags, segment_plan)

    if quality_tier == 'economy':
        preferred = (
            _safe_str(provider_options.get('presenter_provider'))
            or provider_hint
            or _safe_str(tags.get('presenter_provider'))
            or _safe_str(os.getenv('LONGFORM_TALKING_ECONOMY_PRIMARY_PROVIDER'))
            or 'veed_fabric'
        )
        fallback = _safe_str(os.getenv('LONGFORM_TALKING_ECONOMY_FALLBACK_PROVIDER'))
        default_candidates = ['veed_fabric']
    else:
        preferred = (
            _safe_str(provider_options.get('presenter_provider'))
            or provider_hint
            or _safe_str(tags.get('presenter_provider'))
            or _safe_str(os.getenv('LONGFORM_TALKING_PREMIUM_PRIMARY_PROVIDER'))
            or _safe_str(os.getenv('LONGFORM_TALKING_PRIMARY_PROVIDER'))
            or 'kling'
        )
        fallback = _safe_str(os.getenv('LONGFORM_TALKING_PREMIUM_FALLBACK_PROVIDER'))
        default_candidates = ['kling']

    candidates: List[str] = []
    for item in (preferred, fallback, *default_candidates):
        s = _safe_str(item)
        if not s:
            continue
        if s not in candidates:
            candidates.append(s)
    return candidates or default_candidates


def _talking_segment_tags(
    *,
    base_tags: Dict[str, Any],
    provider_name: str,
    preferred_provider: str,
    fallback_from: Optional[str],
    attempt_index: int,
) -> Dict[str, Any]:
    out = dict(base_tags or {})
    out['provider_preference'] = preferred_provider
    out['selected_provider'] = provider_name
    out['provider_attempt_index'] = attempt_index
    if fallback_from:
        out['fallback_from_provider'] = fallback_from
        out['fallback_active'] = True
    return out


async def _run_talking_fusion_child_with_fallback(
    *,
    seg: Dict[str, Any],
    segment_plan: Dict[str, Any],
    tags: Dict[str, Any],
    seg_id: str,
    longform_job_id: str,
    user_id: str,
    face_image_url: Optional[str],
    face_artifact_id: Optional[str],
    audio_res: Dict[str, Any],
    duration_sec: int,
    aspect_ratio: str,
    provider_options: Dict[str, Any],
    reference_image_urls: List[str],
    base_tags: Dict[str, Any],
    token_or_header: str,
    pool,
) -> Tuple[Dict[str, Any], str]:
    del token_or_header
    candidates = _talking_provider_candidates(segment_plan, tags)
    preferred = candidates[0]
    last_exc: Optional[Exception] = None

    for idx, provider_name in enumerate(candidates):
        fallback_from = candidates[idx - 1] if idx > 0 else None
        try:
            async with pool.acquire() as conn:
                await _set_segment_runtime_status(
                    conn,
                    seg_id=seg_id,
                    status=('switching_to_fallback' if idx > 0 else 'provider_running'),
                    error_message=(f'switching_to_fallback:{fallback_from}->{provider_name}' if idx > 0 else None),
                )

            child_duration_sec = int(duration_sec)
            provider_cap_sec = _provider_duration_cap_seconds(provider_name, tags, segment_plan)
            if provider_cap_sec is not None and child_duration_sec > provider_cap_sec:
                logger.info(
                    'capping talking segment duration for provider seg_id=%s provider=%s requested_duration=%s capped_duration=%s',
                    seg_id, provider_name, child_duration_sec, provider_cap_sec,
                )
                child_duration_sec = provider_cap_sec
            child_provider_options = dict(provider_options or {})
            if provider_name in {'veed', 'veed_fabric', 'veed/fabric-1.0'}:
                child_provider_options.setdefault('quality_tier', 'economy')
                child_provider_options.setdefault('provider_hint', 'veed_fabric')
                child_provider_options.setdefault('presenter_provider', 'veed_fabric')
                child_provider_options.setdefault('resolution', _safe_str(child_provider_options.get('resolution')) or '480p')

            child_profile = _longform_profile(tags)
            child_provider_options = _apply_child_pricing_suppression(
                child_provider_options,
                longform_job_id=longform_job_id,
                profile=child_profile,
                segment_id=seg_id,
                role='presenter_child',
            )
            logger.info(
                'talking child pricing suppressed seg_id=%s parent_job_id=%s provider=%s role=%s',
                seg_id,
                longform_job_id,
                provider_name,
                child_provider_options.get('billing_context', {}).get('child_role'),
            )

            child_audio_url = audio_res.get('audio_url')
            child_audio_artifact_id = audio_res.get('audio_artifact_id')
            if audio_res.get('segmented_audio'):
                child_audio_artifact_id = None

            created = await _call_with_auth_retry(
                seg,
                create_fusion_job,
                actor_user_id=user_id,
                provider=provider_name,
                face_image_url=face_image_url,
                face_artifact_id=face_artifact_id,
                audio_url=child_audio_url,
                audio_artifact_id=child_audio_artifact_id,
                duration_sec=child_duration_sec,
                aspect_ratio=aspect_ratio,
                provider_options=child_provider_options,
                reference_image_urls=reference_image_urls,
                tags=_apply_child_pricing_suppression(
                    _talking_segment_tags(
                        base_tags=base_tags,
                        provider_name=provider_name,
                        preferred_provider=preferred,
                        fallback_from=fallback_from,
                        attempt_index=idx,
                    ),
                    longform_job_id=longform_job_id,
                    profile=child_profile,
                    segment_id=seg_id,
                    role='presenter_child',
                ),
            )
            fusion_job_id = created['job_id']
            logger.info(
                'talking segment child job created seg_id=%s fusion_job_id=%s provider=%s fallback_from=%s',
                seg_id, fusion_job_id, provider_name, fallback_from,
            )
            async with pool.acquire() as conn:
                await segs_repo.save_fusion_job(conn, seg_id, fusion_job_id)
                await _set_segment_runtime_status(
                    conn,
                    seg_id=seg_id,
                    fusion_job_id=fusion_job_id,
                    status=('fallback_running' if idx > 0 else 'video_running'),
                )

            status_payload = await _poll_fusion_until_done(
                seg,
                fusion_job_id,
                actor_user_id=user_id,
                timeout_seconds=_fusion_timeout_for_segment(
                    provider_name,
                    tags,
                    segment_plan,
                    child_duration_sec,
                ),
            )
            return status_payload, provider_name
        except Exception as exc:
            last_exc = exc
            msg = _safe_errmsg(exc)
            is_retryable = _is_provider_degraded_message(msg) or isinstance(_unwrap_retry_error(exc), TimeoutError)
            if idx < len(candidates) - 1 and is_retryable:
                logger.warning(
                    'talking segment provider degraded seg_id=%s provider=%s next_provider=%s err=%s',
                    seg_id, provider_name, candidates[idx + 1], msg,
                )
                async with pool.acquire() as conn:
                    await _set_segment_runtime_status(
                        conn,
                        seg_id=seg_id,
                        status='provider_degraded_retrying',
                        error_message=msg,
                    )
                continue
            raise

    raise last_exc or RuntimeError('talking provider fallback exhausted')


def _segment_requires_talking_canary(seg_row: Dict[str, Any]) -> bool:
    tags = _job_tags(seg_row)
    if _longform_profile(tags) != 'talking_video':
        return False
    segment_plan = _segment_plan_from_tags(seg_row, tags)
    if _quality_tier_from_tags(tags, segment_plan) == 'premium' and _provider_hint_from_tags(tags, segment_plan) == 'kling':
        return False
    route = _segment_render_route(segment_plan)
    if route not in {RenderRoute.fusion.value, RenderRoute.legacy_segment_pipeline.value}:
        return False
    return _is_presenter_speaking_shot(_segment_shot_type(segment_plan), segment_plan, tags)



def _longform_profile(tags: Dict[str, Any]) -> str:
    profile = _safe_str(tags.get("longform_profile")) or _safe_str(_as_dict_loose(tags.get("intent")).get("longform_profile")) or "talking_video"
    return profile if profile in {"talking_video", "cinematic_video_direction"} else "talking_video"


def _normalize_quality_tier(value: Any) -> str:
    s = (_safe_str(value) or '').strip().lower()
    if s in {'economy', 'eco', 'fast', 'budget', 'veed', 'veed_fabric'}:
        return 'economy'
    return 'premium'


def _quality_tier_from_tags(tags: Dict[str, Any], segment_plan: Optional[Dict[str, Any]] = None) -> str:
    segment_plan = segment_plan or {}
    intent = _as_dict_loose(tags.get('intent'))
    provider_options = _as_dict_loose(segment_plan.get('provider_options'))
    for value in (
        segment_plan.get('quality_tier'),
        provider_options.get('quality_tier'),
        tags.get('quality_tier'),
        intent.get('quality_tier'),
    ):
        if value is not None:
            return _normalize_quality_tier(value)
    return 'premium'


def _provider_hint_from_tags(tags: Dict[str, Any], segment_plan: Optional[Dict[str, Any]] = None) -> Optional[str]:
    segment_plan = segment_plan or {}
    intent = _as_dict_loose(tags.get('intent'))
    provider_options = _as_dict_loose(segment_plan.get('provider_options'))
    for value in (
        provider_options.get('provider_hint'),
        provider_options.get('presenter_provider'),
        segment_plan.get('provider_hint'),
        tags.get('provider_hint'),
        tags.get('selected_provider'),
        intent.get('provider_hint'),
    ):
        s = _safe_str(value)
        if s:
            return s.strip().lower()
    return None

def _provider_duration_cap_seconds(provider_name: Optional[str], tags: Dict[str, Any], segment_plan: Optional[Dict[str, Any]] = None) -> Optional[int]:
    provider = (_safe_str(provider_name) or '').strip().lower()
    segment_plan = segment_plan or {}
    quality_tier = _quality_tier_from_tags(tags, segment_plan)
    provider_hint = _provider_hint_from_tags(tags, segment_plan)
    if provider == 'kling' or (quality_tier == 'premium' and provider_hint == 'kling'):
        for value in (
            _as_dict_loose(segment_plan.get('provider_options')).get('segment_provider_cap_sec'),
            _as_dict_loose(segment_plan.get('provider_options')).get('provider_cap_seconds'),
            segment_plan.get('provider_cap_seconds'),
            tags.get('segment_provider_cap_sec'),
            tags.get('provider_cap_seconds'),
            os.getenv('FAL_KLING_AVATAR_MAX_DURATION_SEC'),
        ):
            v = _safe_int(value, 0)
            if v > 0:
                return max(10, min(120, v))
        return 30
    if provider in {'veed', 'veed_fabric', 'veed/fabric-1.0'} or quality_tier == 'economy' or provider_hint in {'veed', 'veed_fabric'}:
        for value in (
            _as_dict_loose(segment_plan.get('provider_options')).get('provider_cap_seconds'),
            segment_plan.get('provider_cap_seconds'),
            tags.get('provider_cap_seconds'),
            os.getenv('DF_VEED_FABRIC_MAX_DURATION_SEC'),
            os.getenv('LONGFORM_ECONOMY_PROVIDER_CAP_SECONDS'),
        ):
            v = _safe_int(value, 0)
            if v > 0:
                return max(10, min(60, v))
        return 30
    return None


def _fusion_timeout_for_segment(
    provider_name: Optional[str],
    tags: Dict[str, Any],
    segment_plan: Optional[Dict[str, Any]],
    duration_sec: int,
) -> int:
    base = _safe_int(getattr(settings, "FUSION_TIMEOUT_SECONDS", 0), 600) or 600
    provider = (_safe_str(provider_name) or _provider_hint_from_tags(tags, segment_plan or {}) or "").strip().lower()
    quality = _quality_tier_from_tags(tags, segment_plan or {})

    # KLING Premium runs are slower than VEED and should not be killed while still running.
    if provider == "kling" or (quality == "premium" and provider == "kling"):
        env_override = _safe_int(os.getenv("LONGFORM_KLING_TIMEOUT_SECONDS"), 0)
        if env_override > 0:
            return max(base, env_override)
        return max(base, 900, 180 + int(duration_sec or 0) * 20)

    env_override = _safe_int(os.getenv("LONGFORM_FUSION_TIMEOUT_SECONDS"), 0)
    if env_override > 0:
        return max(base, env_override)
    return base


def _camera_defaults_for_profile(profile: str) -> Dict[str, Optional[str]]:
    if profile == "cinematic_video_direction":
        return {"camera_angle": "eye_level", "camera_framing": "medium", "camera_motion_style": "gentle_push_in"}
    return {"camera_angle": "eye_level", "camera_framing": "medium_close_up", "camera_motion_style": "static"}


def _child_billing_context(longform_job_id: str, profile: str, *, segment_id: Optional[str] = None, role: Optional[str] = None) -> Dict[str, Any]:
    """Billing context for downstream child render jobs.

    svc-fusion-extension is the public, billable product boundary for longform/talking-video.
    Child svc-fusion jobs are implementation details. They must not reserve user credits again,
    otherwise parent pricing succeeds and hidden child pricing can still block with
    PRICING_INSUFFICIENT_CREDITS.

    The suppression markers are intentionally duplicated across nested and top-level shapes
    because svc-fusion has had multiple request schemas over time.
    """
    ctx: Dict[str, Any] = {
        "mode": "internal_child",
        "pricing_suppressed": True,
        "suppress_pricing": True,
        "internal_job": True,
        "child_job": True,
        "bill_to_parent": True,
        "parent_service": "svc-fusion-extension",
        "parent_story_job_id": str(longform_job_id),
        "parent_longform_job_id": str(longform_job_id),
        "billing_parent_job_id": str(longform_job_id),
        "longform_profile": profile,
        "reason": "child_job_of_billable_longform_parent",
    }
    if segment_id:
        ctx["segment_id"] = str(segment_id)
    if role:
        ctx["child_role"] = str(role)
    return ctx


def _child_pricing_state(longform_job_id: str, profile: str, *, segment_id: Optional[str] = None, role: Optional[str] = None) -> Dict[str, Any]:
    ctx = _child_billing_context(longform_job_id, profile, segment_id=segment_id, role=role)
    return {
        "enabled": False,
        "state": "suppressed",
        "suppressed": True,
        "suppress_pricing": True,
        "pricing_suppressed": True,
        "billing_mode": "internal_child",
        "settlement_mode": "internal_child",
        "pricing_mode": "internal_child",
        "unit_type": "internal",
        "estimated_units": "0",
        "reserved_units": None,
        "actual_units": None,
        "billed_units": None,
        "released_units": None,
        "reservation_id": None,
        "reservation_status": None,
        "reason": ctx["reason"],
        "parent_service": ctx["parent_service"],
        "parent_job_id": ctx["parent_longform_job_id"],
        "parent_longform_job_id": ctx["parent_longform_job_id"],
        "billing_parent_job_id": ctx["billing_parent_job_id"],
        "segment_id": ctx.get("segment_id"),
        "child_role": ctx.get("child_role"),
        "longform_profile": profile,
    }


def _apply_child_pricing_suppression(
    data: Optional[Dict[str, Any]],
    *,
    longform_job_id: str,
    profile: str,
    segment_id: Optional[str] = None,
    role: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a copy with child-pricing suppression markers.

    This is safe for both provider_options and tags. It preserves existing values
    while adding explicit top-level and nested pricing markers.
    """
    out = dict(data or {})
    ctx = _child_billing_context(longform_job_id, profile, segment_id=segment_id, role=role)
    pricing = out.get("pricing") if isinstance(out.get("pricing"), dict) else {}
    pricing = {**pricing, **_child_pricing_state(longform_job_id, profile, segment_id=segment_id, role=role)}

    out.update(
        {
            # Multiple equivalent markers are deliberate. The downstream svc-fusion
            # route/orchestrator has had several request shapes over time; this
            # keeps internal child jobs no-charge even if one layer strips extras.
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
            "parent_service": "svc-fusion-extension",
            "parent_job_id": str(longform_job_id),
            "parent_story_job_id": str(longform_job_id),
            "parent_longform_job_id": str(longform_job_id),
            "billing_parent_job_id": str(longform_job_id),
            "pricing_confirmation": {
                **ctx,
                "pricing_suppressed": True,
                "suppress_pricing": True,
                "pricing_mode": "internal_child",
                "state": "suppressed",
                "enabled": False,
            },
            "pricing": pricing,
            "pricing_context": ctx,
            "billing_context": ctx,
            "billing": {
                **ctx,
                "enabled": False,
                "state": "suppressed",
                "billing_mode": "internal_child",
                "settlement_mode": "internal_child",
                "pricing_mode": "internal_child",
            },
        }
    )
    return out


def _apply_camera_to_provider_options(provider_options: Dict[str, Any], segment_plan: Dict[str, Any], tags: Dict[str, Any], profile: str) -> Dict[str, Any]:
    out = dict(provider_options or {})
    defaults = _camera_defaults_for_profile(profile)
    out.setdefault("camera_angle", _safe_str(segment_plan.get("camera_angle")) or _safe_str(tags.get("camera_angle")) or defaults["camera_angle"])
    out.setdefault("camera_framing", _safe_str(segment_plan.get("camera_framing")) or _safe_str(tags.get("camera_framing")) or defaults["camera_framing"])
    out.setdefault("camera_motion_style", _safe_str(segment_plan.get("camera_motion_style")) or _safe_str(tags.get("camera_motion_style")) or defaults["camera_motion_style"])
    return out


# -----------------------------
# Helpers: auth + retry + json
# -----------------------------
def _normalize_bearer(token_or_header: str) -> str:
    t = (token_or_header or "").strip()
    if not t:
        return ""
    if t.lower().startswith("bearer "):
        return t
    return f"Bearer {t}"


_AUTH_CACHE: Dict[str, Any] = {
    "token": "",
    "expires_at": 0.0,
    "source": "",
}


def _settings_or_env(*names: str) -> str:
    for name in names:
        if hasattr(settings, name):
            value = getattr(settings, name)
            if value:
                s = str(value).strip()
                if s:
                    return s
        value = os.getenv(name, "")
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _jwt_exp_epoch(token_or_header: str) -> Optional[float]:
    raw = (token_or_header or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()

    parts = raw.split(".")
    if len(parts) != 3:
        return None

    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        parsed = json.loads(decoded.decode("utf-8"))
    except Exception:
        return None

    exp = parsed.get("exp")
    try:
        return float(exp) if exp is not None else None
    except Exception:
        return None


def _cache_auth_token(token_or_header: str, *, source: str) -> str:
    tok = _normalize_bearer(token_or_header)
    if not tok:
        return ""

    _AUTH_CACHE["token"] = tok
    _AUTH_CACHE["expires_at"] = _jwt_exp_epoch(tok) or 0.0
    _AUTH_CACHE["source"] = source
    return tok


def _cached_auth_token_is_valid() -> bool:
    tok = str(_AUTH_CACHE.get("token") or "").strip()
    if not tok:
        return False

    exp = float(_AUTH_CACHE.get("expires_at") or 0.0)
    if exp <= 0:
        return True

    return (time.time() + 60.0) < exp


def _static_service_bearer() -> str:
    return _normalize_bearer(
        _settings_or_env(
            "SVC_TO_SVC_BEARER",
            "SVC_FUSION_EXTENSION_BEARER",
            "DF_INTERNAL_SERVICE_BEARER",
        )
    )


def _service_login_url() -> str:
    base = _settings_or_env(
        "SVC_CORE_URL",
        "DF_CORE_URL",
        "CORE_URL",
        "AUTH_BASE_URL",
    ).rstrip("/")
    if not base:
        return ""
    if base.endswith("/api/auth/login"):
        return base
    if base.endswith("/api"):
        return f"{base}/auth/login"
    return f"{base}/api/auth/login"


def _service_login_credentials() -> Tuple[str, str]:
    email = _settings_or_env(
        "DF_SERVICE_EMAIL",
        "SVC_FUSION_EXTENSION_SERVICE_EMAIL",
    )
    password = _settings_or_env(
        "DF_SERVICE_PASSWORD",
        "SVC_FUSION_EXTENSION_SERVICE_PASSWORD",
    )
    return email, password


def _login_service_account_blocking() -> str:
    login_url = _service_login_url()
    email, password = _service_login_credentials()
    if not (login_url and email and password):
        return ""

    body = json.dumps({"email": email, "password": password}).encode("utf-8")
    req = urllib_request.Request(
        login_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except urllib_error.HTTPError as ex:
        detail = ""
        try:
            detail = ex.read().decode("utf-8")
        except Exception:
            detail = str(ex)
        raise RuntimeError(f"service_account_login_failed [{ex.code}]: {detail}") from ex
    except Exception as ex:
        raise RuntimeError(f"service_account_login_failed: {ex}") from ex

    try:
        payload = json.loads(raw or "{}")
    except Exception as ex:
        raise RuntimeError(f"service_account_login_invalid_json: {ex}") from ex

    access_token = _normalize_bearer(payload.get("access_token") or "")
    if not access_token:
        raise RuntimeError("service_account_login_missing_access_token")
    return access_token


async def _get_downstream_auth_token(seg_row: Dict[str, Any], *, force_refresh: bool = False) -> str:
    static_bearer = _static_service_bearer()
    if static_bearer:
        return _cache_auth_token(static_bearer, source="service_bearer")

    if not force_refresh and _cached_auth_token_is_valid():
        return str(_AUTH_CACHE.get("token") or "")

    email, password = _service_login_credentials()
    login_url = _service_login_url()
    if login_url and email and password:
        fresh = await asyncio.to_thread(_login_service_account_blocking)
        return _cache_auth_token(fresh, source="service_account_login")

    user_tok = _normalize_bearer(seg_row.get("auth_token") or "")
    if user_tok:
        return _cache_auth_token(user_tok, source="segment_auth_token")

    return ""


def _resolve_auth_token(seg_row: Dict[str, Any]) -> str:
    # Backward-compatible wrapper. New code should prefer _get_downstream_auth_token(...).
    static_bearer = _static_service_bearer()
    if static_bearer:
        return _cache_auth_token(static_bearer, source="service_bearer")

    user_tok = _normalize_bearer(seg_row.get("auth_token") or "")
    if user_tok:
        return _cache_auth_token(user_tok, source="segment_auth_token")

    return ""


def _is_auth_error(e: Exception) -> bool:
    root = _unwrap_retry_error(e)
    msg = f"{type(root).__name__}: {root}".lower()
    needles = (
        "401",
        "unauthorized",
        "invalid token",
        "signature has expired",
        "token expired",
        "expired signature",
        "not authenticated",
        "authentication failed",
    )
    return any(n in msg for n in needles)


async def _call_with_auth_retry(
    seg_row: Dict[str, Any],
    fn,
    *args,
    actor_user_id: Optional[str],
    **kwargs,
):
    token = await _get_downstream_auth_token(seg_row, force_refresh=False)
    if not token:
        raise RuntimeError(
            "Missing downstream auth. Configure SVC_TO_SVC_BEARER or "
            "DF_SERVICE_EMAIL/DF_SERVICE_PASSWORD (+ CORE_URL/SVC_CORE_URL), "
            "or provide seg_row.auth_token as last-resort fallback."
        )

    try:
        return await fn(token, *args, actor_user_id=actor_user_id, **kwargs)
    except Exception as ex:
        if not _is_auth_error(ex):
            raise

        logger.warning(
            "downstream auth failed; retrying once with refreshed auth fn=%s err=%s",
            getattr(fn, "__name__", "<callable>"),
            _safe_errmsg(ex),
        )
        _AUTH_CACHE["token"] = ""
        _AUTH_CACHE["expires_at"] = 0.0
        _AUTH_CACHE["source"] = ""

        fresh = await _get_downstream_auth_token(seg_row, force_refresh=True)
        if not fresh:
            raise
        return await fn(fresh, *args, actor_user_id=actor_user_id, **kwargs)




def _unwrap_retry_error(e: Exception) -> Exception:
    if isinstance(e, RetryError):
        try:
            last = e.last_attempt.exception()
            return last or e
        except Exception:
            return e
    return e


def _safe_errmsg(e: Exception) -> str:
    root = _unwrap_retry_error(e)
    return f"{type(root).__name__}: {root}"


def _exc_info_tuple(e: Exception) -> Tuple[type, BaseException, Any]:
    root = _unwrap_retry_error(e)
    return (type(root), root, root.__traceback__)


def _as_dict(val: Any, *, field: str) -> Dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
        except Exception as ex:
            raise RuntimeError(f"{field}_invalid_json: {ex}") from ex
        return parsed if isinstance(parsed, dict) else {}
    raise RuntimeError(f"{field}_wrong_type: {type(val).__name__}")


def _as_dict_loose(val: Any) -> Dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _as_list_loose(val: Any) -> List[Any]:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, tuple):
        return list(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            return [s]
    return []


def _safe_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(float(v))
    except Exception:
        return default


# -----------------------------
# Directed metadata resolution
# Stored in longform_jobs.tags because current longform_segments has no meta_json.
# -----------------------------
def _job_tags(seg: Dict[str, Any]) -> Dict[str, Any]:
    return _as_dict_loose(seg.get("tags"))


def _segment_plan_from_tags(seg: Dict[str, Any], tags: Dict[str, Any]) -> Dict[str, Any]:
    idx = int(seg.get("segment_index") or 0)

    directed = _as_dict_loose(tags.get("directed_plan"))
    timeline = _as_dict_loose(directed.get("timeline")) or _as_dict_loose(tags.get("timeline"))

    for mapping in (
        _as_dict_loose(directed.get("segments_by_index")),
        _as_dict_loose(timeline.get("segments_by_index")),
        _as_dict_loose(tags.get("segments_by_index")),
    ):
        if mapping:
            item = _as_dict_loose(mapping.get(str(idx)))
            if item:
                return item

    for items in (
        directed.get("segments"),
        timeline.get("segments"),
        tags.get("segments"),
    ):
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and int(item.get("segment_index") or -1) == idx:
                    return item

    for items in (
        directed.get("shots"),
        timeline.get("shots"),
        tags.get("shots"),
    ):
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and int(item.get("segment_index") or item.get("shot_index") or -1) == idx:
                    return item

    return {}


def _segment_script_text(seg: Dict[str, Any], segment_plan: Dict[str, Any]) -> Optional[str]:
    script = _as_dict_loose(segment_plan.get("script"))
    for value in (
        seg.get("text_chunk"),
        seg.get("script_text"),
        segment_plan.get("script_text"),
        script.get("spoken_text"),
        script.get("voiceover_text"),
        script.get("subtitle_text"),
        seg.get("job_script_text"),
    ):
        s = _safe_str(value)
        if s:
            return s

    onscreen = script.get("onscreen_text") or segment_plan.get("onscreen_text")
    if isinstance(onscreen, list):
        joined = " ".join(str(x).strip() for x in onscreen if str(x).strip())
        return joined or None

    return None


def _segment_render_route(segment_plan: Dict[str, Any]) -> str:
    return (
        _safe_str(segment_plan.get("render_route"))
        or _safe_str(segment_plan.get("route"))
        or RenderRoute.legacy_segment_pipeline.value
    )


def _segment_shot_type(segment_plan: Dict[str, Any]) -> Optional[str]:
    return _safe_str(segment_plan.get("shot_type"))


def _segment_mode(segment_plan: Dict[str, Any], tags: Dict[str, Any]) -> Optional[str]:
    return (
        _safe_str(segment_plan.get("mode"))
        or _safe_str(_as_dict_loose(tags.get("intent")).get("mode"))
        or _safe_str(tags.get("mode"))
    )


def _segment_scenario(segment_plan: Dict[str, Any], tags: Dict[str, Any]) -> Optional[str]:
    return (
        _safe_str(segment_plan.get("scenario_type"))
        or _safe_str(_as_dict_loose(tags.get("scenario")).get("scenario_type"))
        or _safe_str(tags.get("scenario_type"))
    )


def _segment_prebuilt_video_url(seg: Dict[str, Any], segment_plan: Dict[str, Any]) -> Optional[str]:
    for value in (
        seg.get("segment_video_url"),
        segment_plan.get("segment_video_url"),
        segment_plan.get("video_url"),
        segment_plan.get("prebuilt_video_url"),
        segment_plan.get("source_video_url"),
        _as_dict_loose(segment_plan.get("resolved_assets")).get("source_video_url"),
    ):
        s = _safe_str(value)
        if s:
            return s
    return None


def _segment_prebuilt_storage_path(seg: Dict[str, Any], segment_plan: Dict[str, Any]) -> Optional[str]:
    for value in (
        seg.get("segment_storage_path"),
        segment_plan.get("segment_storage_path"),
        segment_plan.get("video_storage_path"),
        segment_plan.get("storage_path"),
        _as_dict_loose(segment_plan.get("resolved_assets")).get("storage_path"),
    ):
        s = _safe_str(value)
        if s:
            return s
    return None


def _push_face_url_candidate(candidates: List[str], value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str):
        s = value.strip()
        if s.startswith(("http://", "https://")):
            candidates.append(s)
        return
    if isinstance(value, dict):
        for key in (
            "face_image_url",
            "face_url",
            "storage_ref",
            "url",
            "signed_url",
            "sas_url",
            "blob_url",
            "preview_url",
        ):
            s = _safe_str(value.get(key))
            if s and s.startswith(("http://", "https://")):
                candidates.append(s)
                return
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _push_face_url_candidate(candidates, item)


def _refresh_read_sas_url(url: Optional[str]) -> Optional[str]:
    s = _safe_str(url)
    if not s:
        return None
    try:
        container_name, blob_path = parse_blob_path_from_sas_url(s)
        sas = AzureBlobService(settings.AZURE_STORAGE_CONNECTION_STRING)
        ttl = int(getattr(settings, "FINAL_SAS_TTL_SECONDS", 86400))
        return sas.sign_read_url(container_name, blob_path, ttl)
    except Exception:
        return s

def _artifact_headers(token_or_header: Optional[str], user_id: Optional[str]) -> Dict[str, str]:
    headers = {"Accept": "application/json"}
    tok = _normalize_bearer(token_or_header or "")
    if tok:
        headers["Authorization"] = tok
    if user_id:
        headers["X-User-Id"] = str(user_id)
    return headers


def _artifact_resolver_candidates(kind: str, artifact_id: str) -> List[str]:
    artifact_id = str(artifact_id or "").strip()
    if not artifact_id:
        return []
    if artifact_id.startswith(("http://", "https://")):
        return [artifact_id]

    urls: List[str] = []
    template_names = [
        f"DF_{kind.upper()}_ARTIFACT_RESOLVE_URL_TEMPLATE",
        f"{kind.upper()}_ARTIFACT_RESOLVE_URL_TEMPLATE",
        f"SVC_{kind.upper()}_ARTIFACT_RESOLVE_URL_TEMPLATE",
    ]
    for name in template_names:
        template = _settings_or_env(name)
        if template and "{artifact_id}" in template:
            urls.append(template.replace("{artifact_id}", artifact_id))

    if kind == "audio":
        base = _settings_or_env("SVC_AUDIO_URL", "DF_AUDIO_URL", "AUDIO_URL")
        paths = [
            "/api/audio/artifacts/{artifact_id}",
            "/api/audio/assets/{artifact_id}",
            "/api/audio/media/{artifact_id}",
            "/api/audio/artifact/{artifact_id}",
        ]
    else:
        base = _settings_or_env("SVC_FACE_URL", "DF_FACE_URL", "FACE_URL")
        paths = [
            "/api/face/artifacts/{artifact_id}",
            "/api/face/assets/{artifact_id}",
            "/api/face/media/{artifact_id}",
            "/api/face/artifact/{artifact_id}",
        ]
    if base:
        base = base.rstrip("/")
        for path in paths:
            urls.append(base + path.replace("{artifact_id}", artifact_id))

    deduped: List[str] = []
    seen = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def _deep_first_url(obj: Any, preferred_keys: Tuple[str, ...]) -> Optional[str]:
    if isinstance(obj, dict):
        for key in preferred_keys:
            value = _safe_str(obj.get(key))
            if value and value.startswith(("http://", "https://")):
                return value
        for value in obj.values():
            found = _deep_first_url(value, preferred_keys)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found = _deep_first_url(item, preferred_keys)
            if found:
                return found
    return None


def _deep_first_int(obj: Any, preferred_keys: Tuple[str, ...]) -> int:
    if isinstance(obj, dict):
        for key in preferred_keys:
            try:
                value = obj.get(key)
                if value is not None and int(float(value)) > 0:
                    return int(float(value))
            except Exception:
                pass
        for value in obj.values():
            found = _deep_first_int(value, preferred_keys)
            if found > 0:
                return found
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found = _deep_first_int(item, preferred_keys)
            if found > 0:
                return found
    return 0


def _resolve_audio_artifact_metadata_blocking(artifact_id: str, *, token_or_header: Optional[str], user_id: Optional[str]) -> Dict[str, Any]:
    urls = _artifact_resolver_candidates("audio", artifact_id)
    if not urls:
        return {}
    headers = _artifact_headers(token_or_header, user_id)
    for url in urls:
        req = urllib_request.Request(url, headers=headers, method="GET")
        try:
            with urllib_request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
        except urllib_error.HTTPError as ex:
            if int(getattr(ex, 'code', 0) or 0) in (401, 403, 404, 405):
                continue
            logger.warning("audio artifact resolver http error artifact_id=%s url=%s status=%s", artifact_id, url, getattr(ex, 'code', None))
            continue
        except Exception:
            logger.exception("audio artifact resolver request failed artifact_id=%s url=%s", artifact_id, url)
            continue
        try:
            data = json.loads(raw or "{}")
        except Exception:
            data = {}
        if isinstance(data, dict) and data:
            audio_url = _deep_first_url(data, ("audio_url", "voice_audio_url", "signed_url", "sas_url", "download_url", "url"))
            duration_sec = _deep_first_int(data, ("duration_sec", "audio_duration_sec", "track_duration_sec", "voice_audio_duration_sec", "duration_ms"))
            if audio_url or duration_sec > 0:
                return {"audio_url": audio_url, "audio_artifact_id": artifact_id, "duration_sec": duration_sec}
    return {}


def _resolve_face_selector(seg: Dict[str, Any], segment_plan: Dict[str, Any], tags: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], Optional[str]]:
    tags = tags or {}
    resolved_assets = _as_dict_loose(segment_plan.get("resolved_assets"))
    asset_requirements = _as_dict_loose(segment_plan.get("asset_requirements"))
    seg_face_meta = _as_dict_loose(seg.get("face_meta_json"))
    face_selection = _as_dict_loose(tags.get("face_selection"))
    selected_face = _as_dict_loose(tags.get("selected_face"))

    face_artifact_id = (
        _safe_str(seg.get("face_artifact_id"))
        or _safe_str(resolved_assets.get("face_artifact_id"))
        or _safe_str(asset_requirements.get("face_artifact_id"))
        or _safe_str(segment_plan.get("face_artifact_id"))
        or _safe_str(tags.get("face_artifact_id"))
        or _safe_str(face_selection.get("face_artifact_id"))
        or _safe_str(selected_face.get("face_artifact_id"))
    )

    candidates: List[str] = []
    for value in (
        seg.get("face_image_url"),
        seg.get("selected_face_image_url"),
        seg.get("face_url"),
        seg.get("storage_ref"),
        resolved_assets.get("face_image_url"),
        resolved_assets.get("face_url"),
        resolved_assets.get("storage_ref"),
        asset_requirements.get("face_image_url"),
        asset_requirements.get("face_url"),
        segment_plan.get("face_image_url"),
        segment_plan.get("face_url"),
        segment_plan.get("storage_ref"),
        tags.get("face_image_url"),
        tags.get("selected_face_image_url"),
        tags.get("face_url"),
        tags.get("storage_ref"),
        face_selection,
        selected_face,
        seg_face_meta,
    ):
        _push_face_url_candidate(candidates, value)

    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        refreshed = _refresh_read_sas_url(url)
        if refreshed:
            return refreshed, None

    if face_artifact_id:
        return None, face_artifact_id
    return None, None


def _normalize_output_profile(seg: Dict[str, Any], segment_plan: Dict[str, Any]) -> str:
    return (
        _safe_str(segment_plan.get("output_profile"))
        or _safe_str(getattr(settings, "DEFAULT_OUTPUT_PROFILE", None))
        or "1080p"
    )


async def _mark_segment_succeeded_and_progress(
    conn,
    *,
    seg_id: str,
    longform_job_id: str,
    video_url: str,
    storage_path: Optional[str],
    provider_job_id: Optional[str] = None,
) -> None:
    await segs_repo.mark_succeeded(
        conn,
        seg_id,
        segment_video_url=video_url,
        segment_storage_path=storage_path,
        provider_job_id=provider_job_id,
    )

    await conn.execute(
        """
        update public.longform_jobs
        set
          completed_segments = completed_segments + 1,
          status = case
            when completed_segments + 1 >= total_segments then 'stitching'
            else status
          end
        where id = $1::uuid
        """,
        longform_job_id,
    )
    # DEDICATED_STITCH_HANDOFF_V1: when completed_segments reaches total_segments
    # the SQL above sets parent status='stitching'. The dedicated stitch worker
    # owns canonical finalization so segment workers immediately return to rendering.


# -----------------------------
# Helpers: voice gender + voice
# -----------------------------
def _normalize_gender(val: Any) -> Optional[str]:
    s = ("" if val is None else str(val)).strip().lower()
    if not s:
        return None
    if s in ("m", "male", "man", "boy"):
        return "male"
    if s in ("f", "female", "woman", "girl"):
        return "female"
    return None


def _default_voice_for(gender: str, locale: str) -> str:
    loc = (locale or "en-US").strip() or "en-US"

    female = getattr(settings, "DEFAULT_TTS_VOICE_FEMALE", None) or os.getenv("DEFAULT_TTS_VOICE_FEMALE", "")
    male = getattr(settings, "DEFAULT_TTS_VOICE_MALE", None) or os.getenv("DEFAULT_TTS_VOICE_MALE", "")

    if (gender or "").lower() == "male":
        return male.strip() or (f"{loc}-GuyNeural" if loc.lower().startswith("en-") else "en-US-GuyNeural")
    return female.strip() or (f"{loc}-JennyNeural" if loc.lower().startswith("en-") else "en-US-JennyNeural")


def _resolve_gender_mode_and_manual(seg: Dict[str, Any], voice_cfg: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    mode = seg.get("voice_gender_mode") or voice_cfg.get("voice_gender_mode") or "auto"
    mode = str(mode).strip().lower()
    if mode not in ("auto", "manual"):
        mode = "auto"

    manual = seg.get("voice_gender") or voice_cfg.get("voice_gender") or voice_cfg.get("gender")
    return mode, _normalize_gender(manual)


def _infer_gender_from_seg(seg: Dict[str, Any]) -> Optional[str]:
    meta = _as_dict(seg.get("face_meta_json"), field="face_meta_json")

    g = _normalize_gender(meta.get("gender"))
    if g:
        return g

    for k in ("sex", "voice_gender", "gender_hint"):
        g = _normalize_gender(meta.get(k))
        if g:
            return g

    return None


def _apply_voice_selection(seg: Dict[str, Any], voice_cfg: Dict[str, Any]) -> Dict[str, Any]:
    voice_cfg = dict(voice_cfg or {})

    if voice_cfg.get("voice") or voice_cfg.get("voice_id"):
        return voice_cfg

    mode, manual_gender = _resolve_gender_mode_and_manual(seg, voice_cfg)

    if mode == "manual":
        if not manual_gender:
            raise RuntimeError("voice_gender_missing_for_manual_mode")
        resolved_gender = manual_gender
    else:
        resolved_gender = _infer_gender_from_seg(seg) or "female"

    locale = voice_cfg.get("locale") or voice_cfg.get("target_locale") or "en-US"
    voice_cfg["voice"] = _default_voice_for(resolved_gender, str(locale))
    voice_cfg["voice_gender_resolved"] = resolved_gender
    return voice_cfg


# -----------------------------
# Fusion polling
# -----------------------------
async def _poll_fusion_until_done(
    seg_row: Dict[str, Any],
    job_id: str,
    *,
    actor_user_id: Optional[str],
    timeout_seconds: int,
) -> Dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + float(timeout_seconds)
    started = asyncio.get_running_loop().time()
    last_status = None
    last_provider_job_id = None
    last_error_message = None
    while True:
        st = await _call_with_auth_retry(
            seg_row,
            get_fusion_job,
            job_id,
            actor_user_id=actor_user_id,
        )
        status = (st.get("status") or "").lower()
        provider_job_id = _safe_str(st.get("provider_job_id"))
        error_message = _safe_str(st.get("error_message"))

        if status != last_status or provider_job_id != last_provider_job_id or error_message != last_error_message:
            logger.info(
                "fusion child status job_id=%s provider_job_id=%s status=%s error_message=%s elapsed_s=%s",
                job_id,
                provider_job_id,
                status or "<unset>",
                error_message,
                int(asyncio.get_running_loop().time() - started),
            )
            last_status = status
            last_provider_job_id = provider_job_id
            last_error_message = error_message

        if status in ("succeeded", "success", "done"):
            logger.info("fusion child succeeded job_id=%s provider_job_id=%s elapsed_s=%s", job_id, provider_job_id, int(asyncio.get_running_loop().time() - started))
            return st
        if status in ("failed", "error"):
            raise RuntimeError(st.get("error_message") or str(st))
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(
                "svc-fusion job %s timed out last_status=%s provider_job_id=%s error_message=%s"
                % (job_id, status or "<unset>", provider_job_id or "<unset>", error_message or "<none>")
            )

        poll_seconds = max(2.0, float(getattr(settings, "FUSION_POLL_SECONDS", 5)))
        if status in {"queued", "pending"}:
            poll_seconds = max(poll_seconds, 4.0)
        await asyncio.sleep(poll_seconds)


# -----------------------------
# Internal cinematic helpers
# -----------------------------
def _segment_headline(segment_plan: Dict[str, Any], shot_type: Optional[str]) -> Optional[str]:
    script = _as_dict_loose(segment_plan.get("script"))
    for value in (
        segment_plan.get("headline"),
        segment_plan.get("title"),
        segment_plan.get("card_title"),
        script.get("title"),
        script.get("headline"),
        shot_type,
    ):
        s = _safe_str(value)
        if s:
            return s.replace("_", " ").title() if s == shot_type else s
    return None


def _segment_subheadline(segment_plan: Dict[str, Any]) -> Optional[str]:
    script = _as_dict_loose(segment_plan.get("script"))
    for value in (
        segment_plan.get("subtitle"),
        segment_plan.get("subheadline"),
        segment_plan.get("card_subtitle"),
        script.get("subtitle"),
        script.get("subheadline"),
        script.get("summary"),
    ):
        s = _safe_str(value)
        if s:
            return s
    return None


def _segment_footer(segment_plan: Dict[str, Any]) -> Optional[str]:
    script = _as_dict_loose(segment_plan.get("script"))
    cta = script.get("cta") or segment_plan.get("cta") or segment_plan.get("footer")
    s = _safe_str(cta)
    if s:
        return s
    return _safe_str(getattr(settings, "LONGFORM_DEFAULT_FOOTER", None)) or None


def _push_image_url(items: List[str], value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str):
        s = value.strip()
        if s and s.startswith(("http://", "https://")):
            items.append(s)
        return
    if isinstance(value, dict):
        for key in (
            "url",
            "image_url",
            "source_url",
            "signed_url",
            "sas_url",
            "blob_url",
            "preview_url",
            "storage_ref",
        ):
            s = _safe_str(value.get(key))
            if s and s.startswith(("http://", "https://")):
                items.append(s)
                return
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _push_image_url(items, item)


def _push_video_url(items: List[str], value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str):
        s = value.strip()
        if s and s.startswith(("http://", "https://")):
            items.append(s)
        return
    if isinstance(value, dict):
        for key in (
            "video_url",
            "url",
            "source_url",
            "signed_url",
            "sas_url",
            "blob_url",
            "preview_url",
            "storage_ref",
        ):
            s = _safe_str(value.get(key))
            if s and s.startswith(("http://", "https://")):
                items.append(s)
                return
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _push_video_url(items, item)


def _dedupe_urls(urls: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for url in urls:
        s = _safe_str(url)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _collect_media_items(seg: Dict[str, Any], segment_plan: Dict[str, Any], tags: Dict[str, Any]) -> List[Dict[str, str]]:
    resolved_assets = _as_dict_loose(segment_plan.get("resolved_assets"))
    script = _as_dict_loose(segment_plan.get("script"))

    video_candidates: List[str] = []
    screenshot_candidates: List[str] = []

    for value in (
        segment_plan.get("video_urls"),
        segment_plan.get("videos"),
        segment_plan.get("broll_video_urls"),
        segment_plan.get("video_assets"),
        segment_plan.get("supporting_videos"),
        resolved_assets.get("video_urls"),
        resolved_assets.get("videos"),
        resolved_assets.get("broll_video_urls"),
        resolved_assets.get("video_assets"),
        resolved_assets.get("supporting_videos"),
        script.get("video_urls"),
        script.get("supporting_videos"),
        tags.get("video_urls"),
        tags.get("supporting_videos"),
        tags.get("broll_videos"),
        seg.get("reference_videos"),
    ):
        _push_video_url(video_candidates, value)

    for value in (
        segment_plan.get("screenshot_urls"),
        segment_plan.get("screenshots"),
        resolved_assets.get("screenshot_urls"),
        resolved_assets.get("screenshots"),
        script.get("screenshot_urls"),
        tags.get("screenshot_urls"),
        tags.get("screenshots"),
    ):
        _push_image_url(screenshot_candidates, value)

    video_urls = _dedupe_urls(video_candidates)
    screenshot_urls = _dedupe_urls(screenshot_candidates)
    image_urls = _dedupe_urls(_collect_image_urls(seg, segment_plan, tags))

    items: List[Dict[str, str]] = []
    for url in video_urls:
        items.append({"kind": "video", "url": url})
    for url in screenshot_urls:
        items.append({"kind": "image", "url": url})
    for url in image_urls:
        items.append({"kind": "image", "url": url})

    seen = set()
    deduped: List[Dict[str, str]] = []
    for item in items:
        url = _safe_str(item.get("url"))
        kind = _safe_str(item.get("kind")) or "image"
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append({"kind": kind, "url": url})
    return deduped


def _refresh_and_filter_media_items(
    media_items: List[Dict[str, str]],
    *,
    seg_id: str,
    longform_job_id: str,
    render_route: str,
    shot_type: Optional[str],
    limit: int,
) -> List[Dict[str, str]]:
    refreshed: List[Dict[str, str]] = []
    seen = set()

    for item in media_items:
        kind = (_safe_str(item.get("kind")) or "image").lower()
        original = _safe_str(item.get("url"))
        if not original:
            continue

        candidate = _refresh_read_sas_url(original) or original
        if not candidate or candidate in seen:
            continue

        if _probe_media_url_accessible(candidate):
            seen.add(candidate)
            refreshed.append({"kind": kind, "url": candidate})
            if len(refreshed) >= limit:
                break
            continue

        logger.warning(
            "skipping inaccessible montage media seg_id=%s job_id=%s route=%s shot_type=%s kind=%s media_url=%s",
            seg_id,
            longform_job_id,
            render_route,
            shot_type or "<unset>",
            kind,
            original,
        )

    return refreshed


def _collect_image_urls(seg: Dict[str, Any], segment_plan: Dict[str, Any], tags: Dict[str, Any]) -> List[str]:
    resolved_assets = _as_dict_loose(segment_plan.get("resolved_assets"))
    script = _as_dict_loose(segment_plan.get("script"))
    candidates: List[str] = []

    for value in (
        segment_plan.get("image_urls"),
        segment_plan.get("images"),
        segment_plan.get("broll_image_urls"),
        segment_plan.get("broll_assets"),
        segment_plan.get("supporting_images"),
        segment_plan.get("reference_images"),
        resolved_assets.get("image_urls"),
        resolved_assets.get("images"),
        resolved_assets.get("broll_image_urls"),
        resolved_assets.get("broll_assets"),
        resolved_assets.get("supporting_images"),
        script.get("image_urls"),
        script.get("supporting_images"),
        tags.get("image_urls"),
        tags.get("supporting_images"),
        tags.get("broll_images"),
        seg.get("reference_images"),
    ):
        _push_image_url(candidates, value)

    face_image_url = _safe_str(seg.get("face_image_url")) or _safe_str(resolved_assets.get("face_image_url"))
    if face_image_url:
        candidates.append(face_image_url)

    seen = set()
    deduped: List[str] = []
    for url in candidates:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def _probe_media_url_accessible(url: str, *, timeout_seconds: int = 10) -> bool:
    req = urllib_request.Request(
        url,
        headers={
            "User-Agent": "svc-fusion-extension/1.0",
        },
        method="HEAD",
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout_seconds) as resp:
            code = getattr(resp, "status", None) or getattr(resp, "code", None) or 200
            return 200 <= int(code) < 400
    except urllib_error.HTTPError as ex:
        # Some blob/CDN endpoints reject HEAD while still allowing GET.
        if int(getattr(ex, "code", 0) or 0) in (400, 403, 404):
            return False
        if int(getattr(ex, "code", 0) or 0) in (405, 501):
            return True
        return False
    except Exception:
        return False


def _refresh_and_filter_image_urls(
    image_urls: List[str],
    *,
    seg_id: str,
    longform_job_id: str,
    render_route: str,
    shot_type: Optional[str],
    limit: int,
) -> List[str]:
    refreshed: List[str] = []
    seen = set()

    for original in image_urls:
        candidate = _refresh_read_sas_url(original) or original
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)

        if _probe_media_url_accessible(candidate):
            refreshed.append(candidate)
            if len(refreshed) >= limit:
                break
            continue

        logger.warning(
            "skipping inaccessible montage image seg_id=%s job_id=%s route=%s shot_type=%s image_url=%s",
            seg_id,
            longform_job_id,
            render_route,
            shot_type or "<unset>",
            original,
        )

    return refreshed


def _segment_storage_path(longform_job_id: str, seg_id: str, render_route: str) -> str:
    prefix = _safe_str(getattr(settings, "AZURE_VIDEO_OUTPUT_PREFIX", None)) or "longform"
    return f"{prefix.rstrip('/')}/segments/{longform_job_id}/{seg_id}_{render_route}.mp4"


def _is_presenter_with_motion_bg(shot_type: Optional[str], segment_plan: Dict[str, Any], tags: Dict[str, Any]) -> bool:
    if _quality_tier_from_tags(tags, segment_plan) == 'premium' and _provider_hint_from_tags(tags, segment_plan) == 'kling':
        return False
    st = (_safe_str(shot_type) or '').lower()
    if st in {"presenter_with_motion_bg", "presenter-motion-bg", "presenter_motion_bg"}:
        return True
    provider_options = _as_dict_loose(segment_plan.get("provider_options"))
    composition = _as_dict_loose(provider_options.get("presenter_with_motion_bg"))
    background_mode = (_safe_str(segment_plan.get("background_mode")) or _safe_str(tags.get("background_mode")) or _safe_str(provider_options.get("background_mode")) or "").lower()
    return bool(composition.get("enabled") or tags.get("presenter_with_motion_bg") or background_mode in {"movement_based", "dynamic", "animated", "motion"})


def _is_presenter_speaking_shot(shot_type: Optional[str], segment_plan: Dict[str, Any], tags: Dict[str, Any]) -> bool:
    st = (_safe_str(shot_type) or '').lower()
    if st in {
        "presenter_anchor",
        "presenter_with_motion_bg",
        "talking_head",
        "direct_address",
        "hook_open",
        "hook_close",
        "presenter_open",
        "presenter_close",
        "host_intro",
        "host_outro",
        "spokesperson",
    }:
        return True

    provider_options = _as_dict_loose(segment_plan.get("provider_options"))
    speaking_cfg = _as_dict_loose(provider_options.get("presenter"))
    if bool(speaking_cfg.get("enabled")):
        return True

    if (_safe_str(tags.get("presenter_mode")) or '').lower() in {"anchor", "speaking", "talking"}:
        return True

    return False


def _select_motion_provider(shot_type: Optional[str], segment_plan: Dict[str, Any], tags: Dict[str, Any]) -> str:
    provider_options = _as_dict_loose(segment_plan.get("provider_options"))
    preferred = _safe_str(provider_options.get("background_provider") or tags.get("background_provider"))
    if preferred in {"kling", "luma", "runway"}:
        return preferred

    st = (_safe_str(shot_type) or '').lower()
    realistic = {
        "presenter_with_motion_bg",
        "ambient_bg",
        "traffic_bg",
        "street_bg",
        "market_bg",
        "workplace_bg",
        "nature_bg",
        "crowd_bg",
        "establishing",
        "documentary_broll",
    }
    cinematic = {
        "transition",
        "stylized_transition",
        "camera_move",
        "hero_motion",
        "product_beauty",
        "glamour_broll",
    }
    if st in cinematic:
        return "kling"
    if st in realistic:
        return "luma"
    if str(tags.get("motion_intent") or "").strip().lower() in {"transition", "stylized", "hero"}:
        return "kling"
    return "luma"


def _build_motion_prompt(segment_plan: Dict[str, Any], tags: Dict[str, Any], shot_type: Optional[str], text_chunk: Optional[str]) -> str:
    script = _as_dict_loose(segment_plan.get("script"))
    provider_options = _as_dict_loose(segment_plan.get("provider_options"))
    for value in (
        provider_options.get("background_prompt"),
        provider_options.get("motion_prompt"),
        segment_plan.get("background_prompt"),
        segment_plan.get("motion_prompt"),
        script.get("background_prompt"),
        tags.get("background_prompt"),
        tags.get("prompt_preview"),
    ):
        s = _safe_str(value)
        if s:
            return s
    base = _safe_str(text_chunk) or "A premium cinematic storytelling scene"
    return (
        f"{base}. Keep the presenter foreground readable while the background shows natural environmental motion: "
        "cars moving, trees swaying, people walking or working, subtle camera drift, premium realism, no glitches, no warped faces."
    )


def _build_motion_provider_options(
    *,
    provider_name: str,
    segment_plan: Dict[str, Any],
    tags: Dict[str, Any],
    shot_type: Optional[str],
    text_chunk: Optional[str],
    aspect_ratio: str,
    duration_sec: int,
    reference_image_urls: List[str],
) -> Dict[str, Any]:
    provider_options = _as_dict_loose(segment_plan.get("provider_options"))
    options = dict(provider_options)
    options["prompt"] = _build_motion_prompt(segment_plan, tags, shot_type, text_chunk)
    options.setdefault("aspect_ratio", aspect_ratio)
    options.setdefault("duration_sec", int(duration_sec))
    options.setdefault("resolution", _safe_str(tags.get("resolution")) or "720p")
    options.setdefault("apply_film", True)
    options.setdefault("apply_upscaler", True)
    options.setdefault("motion_intent", _safe_str(tags.get("motion_intent")) or _safe_str(shot_type) or "ambient_realism")
    motion_reference_video_url = (
        _safe_str(provider_options.get("motion_reference_video_url"))
        or _safe_str(segment_plan.get("motion_reference_video_url"))
        or _safe_str(tags.get("motion_reference_video_url"))
    )
    if motion_reference_video_url:
        options.setdefault("motion_reference_video_url", motion_reference_video_url)
        options.setdefault("use_video_as_prompt", True)
        options.setdefault("video_description", _safe_str(tags.get("motion_reference_description")) or "reference motion video")
    options.setdefault("reference_image_urls", reference_image_urls)
    if provider_name == "kling":
        options.setdefault("model_name", _safe_str(os.getenv("FAL_KLING_I2V_MODEL")) or "fal-ai/kling-video/v3/standard/image-to-video")
        options.setdefault("generate_audio", False)
        options.setdefault("shot_type", _safe_str(tags.get("kling_shot_type")) or "customize")
    elif provider_name == "luma":
        options.setdefault("model_name", _safe_str(os.getenv("FAL_LUMA_I2V_MODEL")) or "fal-ai/luma-dream-machine/ray-2/image-to-video")
    elif provider_name == "runway":
        options.setdefault("model_name", _safe_str(os.getenv("FAL_RUNWAY_MODEL_ID")))
    return options




def _build_presenter_prompt(
    segment_plan: Dict[str, Any],
    tags: Dict[str, Any],
    shot_type: Optional[str],
    text_chunk: Optional[str],
) -> str:
    script = _as_dict_loose(segment_plan.get("script"))
    provider_options = _as_dict_loose(segment_plan.get("provider_options"))
    candidates = (
        provider_options.get("prompt"),
        provider_options.get("user_prompt"),
        segment_plan.get("prompt"),
        segment_plan.get("user_prompt"),
        segment_plan.get("performance_prompt"),
        script.get("performance_prompt"),
        tags.get("prompt_preview"),
        tags.get("user_prompt"),
        tags.get("prompt"),
        text_chunk,
        script.get("spoken_text"),
        script.get("voiceover_text"),
    )
    for value in candidates:
        s = _safe_str(value)
        if s:
            return (
                f"{s} Community-driven authenticity, premium realism, "
                f"natural expressions and body language, not corporate avatar aesthetics."
            )
    shot_hint = (_safe_str(shot_type) or "presenter_anchor").replace("_", " ")
    return (
        f"Static or gently pushing medium shot. {shot_hint}. "
        f"The person speaks naturally to camera with expressive eyes, realistic pauses, "
        f"subtle head movement, and grounded human body language. "
        f"Community-driven authenticity, premium realism, not corporate avatar aesthetics."
    )


def _build_presenter_provider_options(
    segment_plan: Dict[str, Any],
    tags: Dict[str, Any],
    shot_type: Optional[str],
    text_chunk: Optional[str],
    output_profile: Optional[str],
    profile: str,
) -> Dict[str, Any]:
    provider_options = dict(_as_dict_loose(segment_plan.get("provider_options")))
    quality_tier = _quality_tier_from_tags(tags, segment_plan)
    provider_hint = _provider_hint_from_tags(tags, segment_plan)
    provider_options["prompt"] = _build_presenter_prompt(segment_plan, tags, shot_type, text_chunk)
    if quality_tier == 'economy' or provider_hint in {'veed', 'veed_fabric'}:
        provider_options.setdefault("resolution", _safe_str(provider_options.get("resolution")) or "480p")
        provider_options.setdefault("presenter_provider", "veed_fabric")
        provider_options.setdefault("model_name", _safe_str(os.getenv("DF_VEED_FABRIC_MODEL_ID")) or "veed/fabric-1.0")
        provider_options.setdefault("quality_tier", "economy")
        provider_options.setdefault("provider_hint", "veed_fabric")
    elif output_profile:
        provider_options.setdefault("resolution", _safe_str(output_profile))
    if tags.get("turbo_mode") is not None:
        provider_options.setdefault("turbo_mode", bool(tags.get("turbo_mode")))
    return _apply_camera_to_provider_options(provider_options, segment_plan, tags, profile)


async def _render_presenter_with_motion_bg_segment(
    *,
    seg: Dict[str, Any],
    segment_plan: Dict[str, Any],
    tags: Dict[str, Any],
    seg_id: str,
    longform_job_id: str,
    user_id: str,
    selected_face_image_url: Optional[str],
    selected_face_artifact_id: Optional[str],
    aspect_ratio: str,
    duration_sec: int,
    text_chunk: str,
    voice_cfg: Dict[str, Any],
    pool,
    profile: str,
) -> Tuple[str, str, Optional[str]]:
    logger.info("presenter_with_motion_bg start seg_id=%s job_id=%s aspect_ratio=%s duration_sec=%s has_face_url=%s has_face_artifact=%s", seg_id, longform_job_id, aspect_ratio, duration_sec, bool(selected_face_image_url), bool(selected_face_artifact_id))
    audio_res = await _resolve_segment_audio(
        seg=seg,
        segment_plan=segment_plan,
        tags=tags,
        seg_id=seg_id,
        longform_job_id=longform_job_id,
        user_id=user_id,
        text_chunk=text_chunk,
        voice_cfg=voice_cfg,
        token_or_header=await _get_downstream_auth_token(seg, force_refresh=False),
        pool=pool,
        required=True,
        require_url=False,
    )

    logger.info("presenter_with_motion_bg audio ready seg_id=%s audio_url=%s audio_artifact_id=%s source=%s", seg_id, _preview_url(audio_res.get("audio_url")), audio_res.get("audio_artifact_id"), audio_res.get("source"))

    presenter_provider_options = _build_presenter_provider_options(
        segment_plan,
        tags,
        "presenter_with_motion_bg",
        text_chunk,
        "1080p" if duration_sec <= 30 else "720p",
        profile,
    )

    reference_image_urls = [
        item["url"] for item in _collect_media_items(seg, segment_plan, tags)
        if item.get("kind") == "image"
    ][:4]
    motion_provider = _select_motion_provider("presenter_with_motion_bg", segment_plan, tags)
    motion_provider_options = _build_motion_provider_options(
        provider_name=motion_provider,
        segment_plan=segment_plan,
        tags=tags,
        shot_type="presenter_with_motion_bg",
        text_chunk=text_chunk,
        aspect_ratio=aspect_ratio,
        duration_sec=duration_sec,
        reference_image_urls=reference_image_urls,
    )

    presenter_base_tags = _apply_child_pricing_suppression(
        {
            "source": "svc-fusion-extension",
            "longform_job_id": longform_job_id,
            "segment_id": seg_id,
            "render_route": "fusion",
            "shot_type": "presenter_anchor",
            "composition_role": "presenter",
            "quality_tier": _quality_tier_from_tags(tags, segment_plan),
            "provider_hint": _provider_hint_from_tags(tags, segment_plan),
        },
        longform_job_id=longform_job_id,
        profile=profile,
        segment_id=seg_id,
        role="presenter_child",
    )

    async def _run_presenter_child():
        return await _run_talking_fusion_child_with_fallback(
            seg=seg,
            segment_plan=segment_plan,
            tags=tags,
            seg_id=seg_id,
            longform_job_id=longform_job_id,
            user_id=user_id,
            face_image_url=selected_face_image_url,
            face_artifact_id=selected_face_artifact_id,
            audio_res=audio_res,
            duration_sec=duration_sec,
            aspect_ratio=aspect_ratio,
            provider_options=presenter_provider_options,
            reference_image_urls=reference_image_urls,
            base_tags=presenter_base_tags,
            token_or_header=await _get_downstream_auth_token(seg, force_refresh=False),
            pool=pool,
        )

    async def _run_background_child():
        bg_job = await _call_with_auth_retry(
            seg,
            create_fusion_job,
            actor_user_id=user_id,
            provider=motion_provider,
            face_image_url=selected_face_image_url,
            face_artifact_id=selected_face_artifact_id,
            duration_sec=duration_sec,
            aspect_ratio=aspect_ratio,
            provider_options=_apply_child_pricing_suppression(
                motion_provider_options,
                longform_job_id=longform_job_id,
                profile=profile,
                segment_id=seg_id,
                role="background_motion_child",
            ),
            reference_image_urls=reference_image_urls,
            tags=_apply_child_pricing_suppression(
                {
                    "source": "svc-fusion-extension",
                    "longform_job_id": longform_job_id,
                    "segment_id": seg_id,
                    "render_route": "fusion",
                    "shot_type": "background_motion_plate",
                    "composition_role": "background",
                    "motion_provider": motion_provider,
                    "apply_film": True,
                    "apply_upscaler": True,
                },
                longform_job_id=longform_job_id,
                profile=profile,
                segment_id=seg_id,
                role="background_motion_child",
            ),
        )
        bg_job_id = bg_job["job_id"]
        bg_status = await _poll_fusion_until_done(
            seg,
            bg_job_id,
            actor_user_id=user_id,
            timeout_seconds=_fusion_timeout_for_segment(
                motion_provider,
                tags,
                segment_plan,
                duration_sec,
            ),
        )
        return bg_job_id, bg_status

    (presenter_status, presenter_provider), (bg_job_id, bg_status) = await asyncio.gather(
        _run_presenter_child(),
        _run_background_child(),
    )
    logger.info("presenter_with_motion_bg child jobs completed seg_id=%s bg_job_id=%s bg_provider=%s presenter_provider=%s reference_image_count=%s", seg_id, bg_job_id, motion_provider, presenter_provider, len(reference_image_urls))

    presenter_video_url = _pick_video_url(presenter_status)
    bg_video_url = _pick_video_url(bg_status)
    logger.info("presenter_with_motion_bg child outputs seg_id=%s presenter_video_url=%s bg_video_url=%s", seg_id, _preview_url(presenter_video_url), _preview_url(bg_video_url))
    if not presenter_video_url:
        raise RuntimeError("presenter_with_motion_bg presenter clip missing video output")
    if not bg_video_url:
        raise RuntimeError("presenter_with_motion_bg background clip missing video output")

    with tempfile.TemporaryDirectory(prefix="df_presenter_bg_comp_") as td:
        local_final = os.path.join(td, "presenter_with_motion_bg.mp4")
        compose_presenter_with_motion_background(
            presenter_video_url=presenter_video_url,
            background_video_url=bg_video_url,
            out_mp4=local_final,
            aspect_ratio=aspect_ratio,
        )
        storage_path = _segment_storage_path(longform_job_id, seg_id, "presenter_with_motion_bg")
        saved_storage_path, signed_url = upload_final_mp4(local_final, storage_path=storage_path)
        logger.info("presenter_with_motion_bg composed seg_id=%s storage_path=%s signed_url=%s", seg_id, saved_storage_path, _preview_url(signed_url))

    return (
        signed_url,
        saved_storage_path,
        presenter_status.get("provider_job_id")
        or presenter_status.get("job_id")
        or presenter_status.get("id"),
    )


async def _maybe_create_tts_audio(
    *,
    seg: Dict[str, Any],
    seg_id: str,
    user_id: str,
    text_chunk: Optional[str],
    voice_cfg: Dict[str, Any],
    token_or_header: str,
    pool,
    required: bool,
) -> Optional[Dict[str, Any]]:
    if not text_chunk:
        return None
    if not token_or_header:
        if required:
            raise RuntimeError("missing_auth_for_internal_tts")
        return None

    try:
        selected_voice_cfg = _apply_voice_selection(seg, voice_cfg)
        audio_res = await _call_with_auth_retry(
            seg,
            create_tts_audio_blocking,
            actor_user_id=user_id,
            text=text_chunk,
            voice_cfg=selected_voice_cfg,
            poll_seconds=settings.AUDIO_POLL_SECONDS,
            timeout_seconds=settings.AUDIO_TIMEOUT_SECONDS,
        )
        async with pool.acquire() as conn:
            await segs_repo.save_audio_result(
                conn,
                seg_id,
                tts_job_id=audio_res["job_id"],
                audio_url=audio_res["audio_url"],
                audio_artifact_id=audio_res.get("audio_artifact_id"),
            )
        audio_res["voice_cfg"] = selected_voice_cfg
        return audio_res
    except Exception:
        if required:
            raise
        logger.warning("internal tts skipped seg_id=%s", seg_id, exc_info=True)
        return None

def _extract_voice_audio_source(seg: Dict[str, Any], segment_plan: Dict[str, Any], tags: Dict[str, Any]) -> Dict[str, Any]:
    resolved_assets = _as_dict_loose(segment_plan.get("resolved_assets"))
    asset_requirements = _as_dict_loose(segment_plan.get("asset_requirements"))

    def _pick_url(*values: Any) -> Optional[str]:
        for value in values:
            s = _safe_str(value)
            if s and s.startswith(("http://", "https://")):
                return s
        return None

    def _pick_id(*values: Any) -> Optional[str]:
        for value in values:
            s = _safe_str(value)
            if s:
                return s
        return None

    audio_url = _pick_url(
        segment_plan.get("segment_audio_url"),
        resolved_assets.get("segment_audio_url"),
        seg.get("audio_url"),
        segment_plan.get("voice_audio_url"),
        resolved_assets.get("voice_audio_url"),
        asset_requirements.get("voice_audio_url"),
        tags.get("voice_audio_url"),
    )
    audio_artifact_id = _pick_id(
        segment_plan.get("segment_audio_artifact_id"),
        resolved_assets.get("segment_audio_artifact_id"),
        segment_plan.get("voice_audio_artifact_id"),
        resolved_assets.get("voice_audio_artifact_id"),
        asset_requirements.get("voice_audio_artifact_id"),
        seg.get("audio_artifact_id"),
        tags.get("voice_audio_artifact_id"),
    )
    audio_start_sec = _safe_int(
        segment_plan.get("audio_start_sec")
        if segment_plan.get("audio_start_sec") is not None
        else resolved_assets.get("audio_start_sec"),
        0,
    )
    audio_end_sec = _safe_int(
        segment_plan.get("audio_end_sec")
        if segment_plan.get("audio_end_sec") is not None
        else resolved_assets.get("audio_end_sec"),
        0,
    )
    duration_sec = _safe_int(
        segment_plan.get("voice_audio_duration_sec")
        if segment_plan.get("voice_audio_duration_sec") is not None
        else resolved_assets.get("voice_audio_duration_sec"),
        0,
    )
    return {
        "audio_url": audio_url,
        "audio_artifact_id": audio_artifact_id,
        "audio_start_sec": audio_start_sec,
        "audio_end_sec": audio_end_sec,
        "duration_sec": duration_sec,
    }


def _upload_audio_clip(local_path: str, *, longform_job_id: str, seg_id: str) -> Dict[str, Any]:
    if not Path(local_path).exists() or Path(local_path).stat().st_size <= 0:
        raise RuntimeError(f"audio clip missing or empty: {local_path}")
    container_name = settings.AZURE_VIDEO_OUTPUT_CONTAINER
    prefix = _safe_str(getattr(settings, "AZURE_VIDEO_OUTPUT_PREFIX", None)) or "longform"
    extension = Path(local_path).suffix or ".mp3"
    blob_path = f"{prefix.rstrip('/')}/audio_segments/{longform_job_id}/{seg_id}_{uuid.uuid4().hex}{extension}"
    blob_service = BlobServiceClient.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)
    container = blob_service.get_container_client(container_name)
    with open(local_path, "rb") as f:
        container.upload_blob(name=blob_path, data=f, overwrite=True, content_settings=ContentSettings(content_type="audio/mpeg"))
    sas = AzureBlobService(settings.AZURE_STORAGE_CONNECTION_STRING)
    signed_url = sas.sign_read_url(container_name, blob_path, int(getattr(settings, "FINAL_SAS_TTL_SECONDS", 86400)))
    return {"audio_url": signed_url, "audio_storage_path": blob_path}


def _slice_audio_clip(source_url: str, *, start_sec: int, end_sec: int, out_path: str) -> None:
    if end_sec <= start_sec:
        raise RuntimeError(f"invalid audio segment window start={start_sec} end={end_sec}")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="df_longform_audio_src_") as td:
        local_src = os.path.join(td, "source_audio.bin")
        download_to_local(source_url, local_src)
        cmd = [
            "ffmpeg",
            "-y",
            "-ss", str(max(0, start_sec)),
            "-i", local_src,
            "-t", str(max(1, end_sec - start_sec)),
            "-vn",
            "-acodec", "mp3",
            out_path,
        ]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"ffmpeg audio slice failed rc={p.returncode}: {p.stderr}")
        if not Path(out_path).exists() or Path(out_path).stat().st_size <= 0:
            raise RuntimeError("ffmpeg audio slice produced empty file")


async def _persist_selected_audio_result(
    *,
    seg_id: str,
    pool,
    audio_url: Optional[str],
    audio_artifact_id: Optional[str],
) -> None:
    if not audio_url:
        return
    async with pool.acquire() as conn:
        await segs_repo.save_audio_result(
            conn,
            seg_id,
            tts_job_id=None,
            audio_url=audio_url,
            audio_artifact_id=audio_artifact_id,
        )


async def _resolve_segment_audio(
    *,
    seg: Dict[str, Any],
    segment_plan: Dict[str, Any],
    tags: Dict[str, Any],
    seg_id: str,
    longform_job_id: str,
    user_id: str,
    text_chunk: Optional[str],
    voice_cfg: Dict[str, Any],
    token_or_header: str,
    pool,
    required: bool,
    require_url: bool = False,
) -> Optional[Dict[str, Any]]:
    source_audio = _extract_voice_audio_source(seg, segment_plan, tags)
    audio_url = _safe_str(source_audio.get("audio_url"))
    audio_artifact_id = _safe_str(source_audio.get("audio_artifact_id"))
    audio_start_sec = _safe_int(source_audio.get("audio_start_sec"), 0)
    audio_end_sec = _safe_int(source_audio.get("audio_end_sec"), 0)

    if not audio_url and audio_artifact_id:
        resolved = await asyncio.to_thread(
            _resolve_audio_artifact_metadata_blocking,
            audio_artifact_id,
            token_or_header=token_or_header,
            user_id=user_id,
        )
        if _safe_str(resolved.get("audio_url")):
            audio_url = _safe_str(resolved.get("audio_url"))

    is_kling_premium = _quality_tier_from_tags(tags, segment_plan) == 'premium' and _provider_hint_from_tags(tags, segment_plan) == 'kling'
    requires_segment_window = is_kling_premium and (_safe_int(seg.get('duration_sec'), 0) > 0) and (_safe_int(tags.get('segment_count'), 0) > 1 or _safe_int(_as_dict_loose(tags.get('directed_plan')).get('shot_count'), 0) > 1)

    if audio_url:
        if audio_end_sec > audio_start_sec:
            with tempfile.TemporaryDirectory(prefix="df_longform_audio_seg_") as td:
                local_clip = os.path.join(td, "segment.mp3")
                _slice_audio_clip(audio_url, start_sec=audio_start_sec, end_sec=audio_end_sec, out_path=local_clip)
                uploaded = _upload_audio_clip(local_clip, longform_job_id=longform_job_id, seg_id=seg_id)
            await _persist_selected_audio_result(seg_id=seg_id, pool=pool, audio_url=uploaded.get("audio_url"), audio_artifact_id=None)
            return {"audio_url": uploaded.get("audio_url"), "audio_artifact_id": None, "source": "original_voice_audio_segment", "segmented_audio": True}

        if requires_segment_window:
            raise RuntimeError(f"SEGMENT_AUDIO_WINDOW_MISSING seg_id={seg_id} start={audio_start_sec} end={audio_end_sec}")

        await _persist_selected_audio_result(seg_id=seg_id, pool=pool, audio_url=audio_url, audio_artifact_id=audio_artifact_id)
        return {"audio_url": audio_url, "audio_artifact_id": audio_artifact_id, "source": "original_voice_audio", "segmented_audio": False}

    if audio_artifact_id and not require_url and audio_end_sec <= audio_start_sec:
        if requires_segment_window:
            raise RuntimeError(f"SEGMENT_AUDIO_URL_MISSING_FOR_WINDOWED_SEGMENT seg_id={seg_id} audio_artifact_id={audio_artifact_id}")
        return {"audio_url": None, "audio_artifact_id": audio_artifact_id, "source": "original_voice_audio_artifact", "segmented_audio": False}

    return await _maybe_create_tts_audio(
        seg=seg,
        seg_id=seg_id,
        user_id=user_id,
        text_chunk=text_chunk,
        voice_cfg=voice_cfg,
        token_or_header=token_or_header,
        pool=pool,
        required=required,
    )


async def _render_internal_card_segment(
    *,
    seg: Dict[str, Any],
    segment_plan: Dict[str, Any],
    tags: Dict[str, Any],
    seg_id: str,
    longform_job_id: str,
    user_id: str,
    aspect_ratio: str,
    duration_sec: int,
    text_chunk: Optional[str],
    voice_cfg: Dict[str, Any],
    token_or_header: str,
    render_route: str,
    shot_type: Optional[str],
    mode: str,
    scenario_type: Optional[str],
    pool,
) -> Tuple[str, str, Optional[str]]:
    title = _segment_headline(segment_plan, shot_type) or "DesiFaces"
    subtitle = _segment_subheadline(segment_plan)
    footer = _segment_footer(segment_plan)
    body = None
    if not subtitle and text_chunk:
        body = text_chunk[:180]

    silent_duration = max(2, min(duration_sec or 3, 8))

    with tempfile.TemporaryDirectory(prefix="df_longform_card_") as td:
        silent_mp4 = os.path.join(td, "card_silent.mp4")
        final_mp4 = os.path.join(td, "card_final.mp4")

        render_text_card(
            silent_mp4,
            duration_sec=silent_duration,
            aspect_ratio=aspect_ratio,
            title=title,
            subtitle=subtitle,
            body=body,
            footer=footer,
        )

        audio_res = await _resolve_segment_audio(
            seg=seg,
            segment_plan=segment_plan,
            tags=tags,
            seg_id=seg_id,
            longform_job_id=longform_job_id,
            user_id=user_id,
            text_chunk=text_chunk,
            voice_cfg=voice_cfg,
            token_or_header=token_or_header,
            pool=pool,
            required=False,
            require_url=True,
        )

        output_path = silent_mp4
        if audio_res and audio_res.get("audio_url"):
            local_audio = os.path.join(td, "card_audio.bin")
            download_to_local(audio_res["audio_url"], local_audio)
            audio_duration = probe_duration_seconds(local_audio) or 0.0
            target_duration = max(float(silent_duration), float(audio_duration) + 0.25)
            if target_duration > float(silent_duration) + 0.05:
                render_text_card(
                    silent_mp4,
                    duration_sec=target_duration,
                    aspect_ratio=aspect_ratio,
                    title=title,
                    subtitle=subtitle,
                    body=body,
                    footer=footer,
                )
            attach_audio_track(silent_mp4, local_audio, final_mp4)
            output_path = final_mp4

        storage_path = _segment_storage_path(longform_job_id, seg_id, render_route)
        saved_storage_path, signed_url = upload_final_mp4(output_path, storage_path=storage_path)
        logger.info(
            "internal card rendered seg_id=%s job_id=%s route=%s shot_type=%s mode=%s scenario=%s",
            seg_id,
            longform_job_id,
            render_route,
            shot_type or "<unset>",
            mode,
            scenario_type or "<unset>",
        )
        return signed_url, saved_storage_path, None


async def _render_internal_montage_or_broll_segment(
    *,
    seg: Dict[str, Any],
    segment_plan: Dict[str, Any],
    tags: Dict[str, Any],
    seg_id: str,
    longform_job_id: str,
    user_id: str,
    aspect_ratio: str,
    duration_sec: int,
    text_chunk: Optional[str],
    voice_cfg: Dict[str, Any],
    token_or_header: str,
    render_route: str,
    shot_type: Optional[str],
    mode: str,
    scenario_type: Optional[str],
    pool,
) -> Tuple[str, str, Optional[str]]:
    media_items = _collect_media_items(seg, segment_plan, tags)
    title = _segment_headline(segment_plan, shot_type)
    subtitle = _segment_subheadline(segment_plan)
    footer = _segment_footer(segment_plan)

    require_narration = render_route == RenderRoute.audio_broll.value
    audio_res = await _resolve_segment_audio(
        seg=seg,
        segment_plan=segment_plan,
        tags=tags,
        seg_id=seg_id,
        longform_job_id=longform_job_id,
        user_id=user_id,
        text_chunk=text_chunk,
        voice_cfg=voice_cfg,
        token_or_header=token_or_header,
        pool=pool,
        required=require_narration,
        require_url=True,
    )

    with tempfile.TemporaryDirectory(prefix="df_longform_montage_") as td:
        montage_silent = os.path.join(td, "montage_silent.mp4")
        final_mp4 = os.path.join(td, "montage_final.mp4")

        local_audio_path: Optional[str] = None
        audio_duration = 0.0
        if audio_res and audio_res.get("audio_url"):
            local_audio_path = os.path.join(td, "narration.bin")
            download_to_local(audio_res["audio_url"], local_audio_path)
            audio_duration = probe_duration_seconds(local_audio_path) or 0.0

        target_duration = max(float(duration_sec or 4), float(audio_duration) + 0.25)
        target_duration = max(2.0, min(target_duration, float(getattr(settings, "MAX_SEGMENT_SECONDS", 120))))

        max_media_items = max(
            1,
            _safe_int(getattr(settings, "LONGFORM_MAX_MONTAGE_MEDIA_ITEMS", None), 6) or 6,
        )
        usable_media_items = _refresh_and_filter_media_items(
            media_items,
            seg_id=seg_id,
            longform_job_id=longform_job_id,
            render_route=render_route,
            shot_type=shot_type,
            limit=max_media_items,
        )

        if usable_media_items:
            render_mixed_montage_segment(
                usable_media_items,
                montage_silent,
                duration_sec=target_duration,
                aspect_ratio=aspect_ratio,
                title=title,
                subtitle=subtitle,
                footer=footer,
            )
        else:
            if media_items:
                logger.warning(
                    "no accessible montage media remained after SAS refresh seg_id=%s job_id=%s route=%s shot_type=%s requested_media=%s",
                    seg_id,
                    longform_job_id,
                    render_route,
                    shot_type or "<unset>",
                    len(media_items),
                )
            fallback_title = title or ("Narrated Visual" if render_route == RenderRoute.audio_broll.value else "Story Beat")
            fallback_body = text_chunk[:180] if text_chunk else ""
            render_text_card(
                montage_silent,
                duration_sec=target_duration,
                aspect_ratio=aspect_ratio,
                title=fallback_title,
                subtitle=subtitle,
                body=fallback_body,
                footer=footer,
            )

        output_path = montage_silent
        if local_audio_path:
            attach_audio_track(montage_silent, local_audio_path, final_mp4)
            output_path = final_mp4

        storage_path = _segment_storage_path(longform_job_id, seg_id, render_route)
        saved_storage_path, signed_url = upload_final_mp4(output_path, storage_path=storage_path)
        logger.info(
            "internal montage rendered seg_id=%s job_id=%s route=%s shot_type=%s mode=%s scenario=%s media_count=%s",
            seg_id,
            longform_job_id,
            render_route,
            shot_type or "<unset>",
            mode,
            scenario_type or "<unset>",
            len(usable_media_items),
        )
        return signed_url, saved_storage_path, None


# -----------------------------
# Main segment execution
# -----------------------------
async def _process_segment(seg: Dict[str, Any], pool) -> None:
    seg_id = str(seg["id"])
    longform_job_id = str(seg["job_id"])
    user_id = str(seg["user_id"])

    tags = _job_tags(seg)
    segment_plan = _segment_plan_from_tags(seg, tags)

    aspect_ratio = segment_plan.get("aspect_ratio") or seg["aspect_ratio"]
    duration_sec = min(int(getattr(settings, "MAX_SEGMENT_SECONDS", 120)), int(seg["duration_sec"]))

    voice_cfg = _as_dict(seg.get("voice_cfg"), field="voice_cfg")
    render_route = _segment_render_route(segment_plan)
    shot_type = _segment_shot_type(segment_plan)
    mode = _segment_mode(segment_plan, tags) or "legacy"
    scenario_type = _segment_scenario(segment_plan, tags)
    profile = _longform_profile(tags)

    try:
        logger.info("segment start seg_id=%s job_id=%s user_id=%s route=%s shot_type=%s mode=%s scenario=%s profile=%s aspect_ratio=%s duration_sec=%s", seg_id, longform_job_id, user_id, render_route, shot_type or "<unset>", mode, scenario_type or "<unset>", profile, aspect_ratio, duration_sec)
        token_or_header = await _get_downstream_auth_token(seg, force_refresh=False)

        selected_face_image_url, selected_face_artifact_id = _resolve_face_selector(seg, segment_plan, tags)
        text_chunk = _segment_script_text(seg, segment_plan)
        output_profile = _normalize_output_profile(seg, segment_plan)
        prebuilt_video_url = _segment_prebuilt_video_url(seg, segment_plan)
        prebuilt_storage_path = _segment_prebuilt_storage_path(seg, segment_plan)

        logger.info("segment inputs seg_id=%s has_token=%s has_face_url=%s has_face_artifact=%s text_len=%s output_profile=%s prebuilt_video_url=%s", seg_id, bool(token_or_header), bool(selected_face_image_url), bool(selected_face_artifact_id), len(text_chunk or ""), output_profile, _preview_url(prebuilt_video_url))

        if render_route == RenderRoute.imported_asset.value:
            if not prebuilt_video_url:
                raise RuntimeError("imported_asset_missing_prebuilt_video_url")

            parsed_storage_path = prebuilt_storage_path
            if not parsed_storage_path:
                try:
                    _, parsed_storage_path = parse_blob_path_from_sas_url(prebuilt_video_url)
                except Exception:
                    parsed_storage_path = None

            async with pool.acquire() as conn:
                await _mark_segment_succeeded_and_progress(
                    conn,
                    seg_id=seg_id,
                    longform_job_id=longform_job_id,
                    video_url=prebuilt_video_url,
                    storage_path=parsed_storage_path,
                    provider_job_id=None,
                )

            logger.info(
                "segment succeeded via imported/prebuilt asset seg_id=%s job_id=%s route=%s shot_type=%s mode=%s scenario=%s",
                seg_id,
                longform_job_id,
                render_route,
                shot_type or "<unset>",
                mode,
                scenario_type or "<unset>",
            )
            return

        if render_route == RenderRoute.internal_card.value:
            video_url, storage_path, provider_job_id = await _render_internal_card_segment(
                seg=seg,
                segment_plan=segment_plan,
                tags=tags,
                seg_id=seg_id,
                longform_job_id=longform_job_id,
                user_id=user_id,
                aspect_ratio=aspect_ratio,
                duration_sec=duration_sec,
                text_chunk=text_chunk,
                voice_cfg=voice_cfg,
                token_or_header=token_or_header,
                render_route=render_route,
                shot_type=shot_type,
                mode=mode,
                scenario_type=scenario_type,
                pool=pool,
            )
            async with pool.acquire() as conn:
                await _mark_segment_succeeded_and_progress(
                    conn,
                    seg_id=seg_id,
                    longform_job_id=longform_job_id,
                    video_url=video_url,
                    storage_path=storage_path,
                    provider_job_id=provider_job_id,
                )
            return

        if render_route in {RenderRoute.internal_montage.value, RenderRoute.audio_broll.value}:
            video_url, storage_path, provider_job_id = await _render_internal_montage_or_broll_segment(
                seg=seg,
                segment_plan=segment_plan,
                tags=tags,
                seg_id=seg_id,
                longform_job_id=longform_job_id,
                user_id=user_id,
                aspect_ratio=aspect_ratio,
                duration_sec=duration_sec,
                text_chunk=text_chunk,
                voice_cfg=voice_cfg,
                token_or_header=token_or_header,
                render_route=render_route,
                shot_type=shot_type,
                mode=mode,
                scenario_type=scenario_type,
                pool=pool,
            )
            async with pool.acquire() as conn:
                await _mark_segment_succeeded_and_progress(
                    conn,
                    seg_id=seg_id,
                    longform_job_id=longform_job_id,
                    video_url=video_url,
                    storage_path=storage_path,
                    provider_job_id=provider_job_id,
                )
            return

        if render_route in {RenderRoute.fusion.value, RenderRoute.legacy_segment_pipeline.value}:
            if not token_or_header:
                raise RuntimeError(
                    "Missing downstream auth. Configure SVC_TO_SVC_BEARER or "
                    "DF_SERVICE_EMAIL/DF_SERVICE_PASSWORD (+ DF_CORE_URL/SVC_CORE_URL), "
                    "or provide seg.auth_token as a last-resort fallback."
                )

            if not (selected_face_image_url or selected_face_artifact_id):
                raise RuntimeError(
                    f"Missing face selector for render_route={render_route}: "
                    "neither face_image_url nor face_artifact_id present"
                )

            if not text_chunk:
                raise RuntimeError(f"Missing text_chunk/script_text for render_route={render_route}")

            voice_cfg = _apply_voice_selection(seg, voice_cfg)

            logger.info(
                "fusion segment start seg_id=%s job_id=%s user_id=%s route=%s shot_type=%s mode=%s scenario=%s "
                "aspect=%s dur=%s has_face_url=%s has_face_artifact=%s voice=%s resolved_gender=%s",
                seg_id,
                longform_job_id,
                user_id,
                render_route,
                shot_type or "<unset>",
                mode,
                scenario_type or "<unset>",
                aspect_ratio,
                duration_sec,
                bool(selected_face_image_url),
                bool(selected_face_artifact_id),
                voice_cfg.get("voice") or voice_cfg.get("voice_id") or "<none>",
                voice_cfg.get("voice_gender_resolved") or "<unset>",
            )

            is_premium_kling = (
                _quality_tier_from_tags(tags, segment_plan) == 'premium'
                and _provider_hint_from_tags(tags, segment_plan) == 'kling'
            )

            if _is_presenter_with_motion_bg(shot_type, segment_plan, tags) and not is_premium_kling:
                video_url, storage_path, provider_job_id = await _render_presenter_with_motion_bg_segment(
                    seg=seg,
                    segment_plan=segment_plan,
                    tags=tags,
                    seg_id=seg_id,
                    longform_job_id=longform_job_id,
                    user_id=user_id,
                    selected_face_image_url=selected_face_image_url,
                    selected_face_artifact_id=selected_face_artifact_id,
                    aspect_ratio=aspect_ratio,
                    duration_sec=duration_sec,
                    text_chunk=text_chunk,
                    voice_cfg=voice_cfg,
                    pool=pool,
                    profile=profile,
                )
                async with pool.acquire() as conn:
                    await _mark_segment_succeeded_and_progress(
                        conn,
                        seg_id=seg_id,
                        longform_job_id=longform_job_id,
                        video_url=video_url,
                        storage_path=storage_path,
                        provider_job_id=provider_job_id,
                    )
                return

            reference_image_urls = [
                item["url"] for item in _collect_media_items(seg, segment_plan, tags)
                if item.get("kind") == "image"
            ][:4]

            if not _is_presenter_speaking_shot(shot_type, segment_plan, tags):
                fallback_route = (
                    RenderRoute.audio_broll.value
                    if text_chunk
                    else RenderRoute.internal_montage.value
                )
                logger.info(
                    "rerouting non-presenter fusion shot to internal renderer seg_id=%s job_id=%s route=%s "
                    "fallback_route=%s shot_type=%s mode=%s scenario=%s",
                    seg_id,
                    longform_job_id,
                    render_route,
                    fallback_route,
                    shot_type or "<unset>",
                    mode,
                    scenario_type or "<unset>",
                )
                video_url, storage_path, provider_job_id = await _render_internal_montage_or_broll_segment(
                    seg=seg,
                    segment_plan=segment_plan,
                    tags=tags,
                    seg_id=seg_id,
                    longform_job_id=longform_job_id,
                    user_id=user_id,
                    aspect_ratio=aspect_ratio,
                    duration_sec=duration_sec,
                    text_chunk=text_chunk,
                    voice_cfg=voice_cfg,
                    token_or_header=token_or_header,
                    render_route=fallback_route,
                    shot_type=shot_type,
                    mode=mode,
                    scenario_type=scenario_type,
                    pool=pool,
                )
                async with pool.acquire() as conn:
                    await _mark_segment_succeeded_and_progress(
                        conn,
                        seg_id=seg_id,
                        longform_job_id=longform_job_id,
                        video_url=video_url,
                        storage_path=storage_path,
                        provider_job_id=provider_job_id,
                    )
                return

            audio_res = await _resolve_segment_audio(
                seg=seg,
                segment_plan=segment_plan,
                tags=tags,
                seg_id=seg_id,
                longform_job_id=longform_job_id,
                user_id=user_id,
                text_chunk=text_chunk,
                voice_cfg=voice_cfg,
                token_or_header=token_or_header,
                pool=pool,
                required=True,
                require_url=False,
            )

            presenter_provider_options = _build_presenter_provider_options(
                segment_plan,
                tags,
                shot_type,
                text_chunk,
                output_profile,
                profile,
            )

            base_child_tags = _apply_child_pricing_suppression(
                {
                    "source": "svc-fusion-extension",
                    "longform_job_id": longform_job_id,
                    "segment_id": seg_id,
                    "user_id": user_id,
                    "mode": mode,
                    "scenario_type": scenario_type,
                    "render_route": render_route,
                    "shot_type": shot_type,
                    "voice_gender_resolved": voice_cfg.get("voice_gender_resolved"),
                    "voice": voice_cfg.get("voice") or voice_cfg.get("voice_id"),
                    "output_profile": output_profile,
                    "quality_tier": _quality_tier_from_tags(tags, segment_plan),
                    "provider_hint": _provider_hint_from_tags(tags, segment_plan),
                },
                longform_job_id=longform_job_id,
                profile=profile,
                segment_id=seg_id,
                role="presenter_child",
            )

            st, selected_provider = await _run_talking_fusion_child_with_fallback(
                seg=seg,
                segment_plan=segment_plan,
                tags=tags,
                seg_id=seg_id,
                longform_job_id=longform_job_id,
                user_id=user_id,
                face_image_url=selected_face_image_url,
                face_artifact_id=selected_face_artifact_id,
                audio_res=audio_res,
                duration_sec=duration_sec,
                aspect_ratio=aspect_ratio,
                provider_options=presenter_provider_options,
                reference_image_urls=reference_image_urls,
                base_tags=base_child_tags,
                token_or_header=token_or_header,
                pool=pool,
            )
            video_url = _pick_video_url(st)
            logger.info("fusion segment child completed seg_id=%s provider=%s provider_job_id=%s video_url=%s", seg_id, selected_provider, st.get("provider_job_id"), _preview_url(video_url))
            if not video_url:
                raise RuntimeError("svc-fusion succeeded but no video artifact found")

            _, blob_path = parse_blob_path_from_sas_url(video_url)
            provider_job_id = st.get("provider_job_id")

            async with pool.acquire() as conn:
                await _mark_segment_succeeded_and_progress(
                    conn,
                    seg_id=seg_id,
                    longform_job_id=longform_job_id,
                    video_url=video_url,
                    storage_path=blob_path,
                    provider_job_id=provider_job_id,
                )

            child_fusion_job_id = _safe_str(st.get("job_id")) or _safe_str(st.get("id")) or _safe_str(seg.get("fusion_job_id"))
            logger.info(
                "fusion segment succeeded seg_id=%s fusion_job_id=%s route=%s shot_type=%s",
                seg_id,
                child_fusion_job_id or "<unknown>",
                render_route,
                shot_type or "<unset>",
            )
            return

        raise RuntimeError(f"Unsupported render_route={render_route}")

    except Exception as e:
        msg = _safe_errmsg(e)
        logger.error(
            "segment failed seg_id=%s job_id=%s user_id=%s route=%s shot_type=%s mode=%s scenario=%s err=%s",
            seg_id,
            longform_job_id,
            user_id,
            render_route,
            shot_type or "<unset>",
            mode,
            scenario_type or "<unset>",
            msg,
            exc_info=_exc_info_tuple(e),
        )

        async with pool.acquire() as conn:
            try:
                await release_longform_pricing_for_job(conn, job_id=longform_job_id, user_id=user_id, reason="segment_failed", tags=tags)
            except Exception:
                logger.exception("longform_pricing_release_failed_on_segment_error job_id=%s seg_id=%s", longform_job_id, seg_id)
            await segs_repo.mark_failed(conn, seg_id, error_code="SEGMENT_FAILED", error_message=msg)
            await conn.execute(
                """
                update public.longform_jobs
                set status='failed', error_code='SEGMENT_FAILED', error_message=$2
                where id=$1::uuid and status not in ('succeeded','failed')
                """,
                longform_job_id,
                msg,
            )


def _effective_max_inflight_per_job() -> int:
    """Launch performance floor for independent segment fan-out.

    Older deployments may still carry MAX_INFLIGHT_SEGMENTS_PER_JOB=2 in the
    environment. Preserve higher operator settings, but do not allow that legacy
    default to serialize a typical 60-120 second parent into waves of two.
    """
    try:
        configured = max(1, int(settings.MAX_INFLIGHT_SEGMENTS_PER_JOB))
    except Exception:
        configured = 1
    try:
        batch = max(1, int(settings.WORKER_BATCH_SIZE))
    except Exception:
        batch = 8
    return min(batch, max(configured, min(8, batch)))


def _segment_worker_concurrency() -> int:
    configured = (
        os.getenv("LONGFORM_SEGMENT_CONCURRENCY")
        or os.getenv("LONGFORM_WORKER_SEGMENT_CONCURRENCY")
        or ""
    ).strip()
    try:
        if configured:
            return max(1, int(configured))
    except Exception:
        pass
    try:
        return max(1, int(settings.WORKER_BATCH_SIZE))
    except Exception:
        return 4


async def _process_segments_concurrently(segs: List[Dict[str, Any]], pool) -> None:
    if not segs:
        return
    max_concurrency = max(1, min(len(segs), _segment_worker_concurrency()))
    sem = asyncio.Semaphore(max_concurrency)
    talking_job_locks: Dict[str, asyncio.Lock] = {}

    async def _run_one(seg_row: Dict[str, Any]) -> None:
        async with sem:
            seg_copy = dict(seg_row)
            if _segment_requires_talking_canary(seg_copy):
                job_key = str(seg_copy.get('job_id') or '')
                lock = talking_job_locks.setdefault(job_key, asyncio.Lock())
                async with lock:
                    await _process_segment(seg_copy, pool)
            else:
                await _process_segment(seg_copy, pool)

    results = await asyncio.gather(*(_run_one(dict(s)) for s in segs), return_exceptions=True)
    for seg_row, result in zip(segs, results):
        if isinstance(result, Exception):
            logger.exception(
                "segment task raised unexpectedly seg_id=%s job_id=%s",
                seg_row.get("id"),
                seg_row.get("job_id"),
                exc_info=(type(result), result, result.__traceback__),
            )


# -----------------------------
# Worker loop
# -----------------------------
async def worker_loop() -> None:
    if not settings.WORKER_ENABLED:
        logger.info("WORKER_ENABLED=false; exiting worker loop.")
        return

    pool = await get_db_pool()
    logger.info(
        "longform_worker started (batch=%s poll=%.2fs max_inflight=%s)",
        settings.WORKER_BATCH_SIZE,
        float(settings.WORKER_POLL_SECONDS),
        _effective_max_inflight_per_job(),
    )

    while True:
        async with pool.acquire() as conn:
            segs = await segs_repo.fetch_next_segments(
                conn,
                settings.WORKER_BATCH_SIZE,
                _effective_max_inflight_per_job(),
            )

        if not segs:
            await asyncio.sleep(settings.WORKER_POLL_SECONDS)
            continue

        await _process_segments_concurrently([dict(s) for s in segs], pool)

        await asyncio.sleep(settings.WORKER_POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(worker_loop())
