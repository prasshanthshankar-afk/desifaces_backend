
from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import httpx
import hashlib
import logging
import math

PRICING_IMPORT_ERROR: Optional[str] = None
try:
    from desifaces_shared.pricing.client import PricingClientError, SvcPricingClient
    from desifaces_shared.pricing.orchestration import (
        PricingPreviewSpec,
        PricingReserveSpec,
        PricingCommitSpec,
        PricingReleaseSpec,
        build_preview_request,
        build_reserve_request,
        build_commit_request,
        build_release_request,
        build_pricing_summary,
        make_preview_artifact,
        make_reserved_artifact,
        make_committed_artifact,
        make_released_artifact,
    )
except Exception as pricing_import_error:
    PRICING_IMPORT_ERROR = str(pricing_import_error)
    class PricingClientError(Exception):
        pass
    class SvcPricingClient:
        enabled = False
        @classmethod
        def from_env(cls, service_name: str):
            return cls()
        async def preview(self, req): raise PricingClientError("pricing client unavailable")
        async def reserve(self, req): raise PricingClientError("pricing client unavailable")
        async def commit(self, req): raise PricingClientError("pricing client unavailable")
        async def release(self, req): raise PricingClientError("pricing client unavailable")

from app.domain.enums import (
    LongformJobStatus,
    LongformMode,
    LongformStage,
    QcDecision,
    RenderRoute,
    ScenarioType,
    SegmentStatus,
    ShotType,
)
from app.domain.models import (
    LongformCreateRequest,
    QcIssue,
    QcResult,
    ScenarioPlan,
    ScriptSpec,
    ShotSpec,
    StoryBeat,
    TimelineManifest,
    VideoIntent,
)
from app.repos.longform_jobs_repo import LongformJobsRepo
from app.repos.longform_segments_repo import LongformSegmentsRepo
from app.services.stitch_service import compose_timeline, download_to_local, probe_duration_seconds, upload_final_mp4


def _safe_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _as_dict_loose(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}



def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(float(v))
    except Exception:
        return default


def _as_list_loose(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []



pricing_logger = logging.getLogger("svc_fusion_extension.longform_pricing")


def _notifications_base_url() -> str:
    return str(
        os.getenv("DF_NOTIFICATIONS_URL")
        or os.getenv("DF_CORE_URL")
        or os.getenv("SVC_CORE_URL")
        or ""
    ).strip().rstrip("/")


def _notifications_internal_events_url() -> str:
    base = _notifications_base_url()
    if not base:
        return ""
    if base.endswith("/api/internal/notifications/events"):
        return base
    if base.endswith("/api"):
        return f"{base}/internal/notifications/events"
    return f"{base}/api/internal/notifications/events"


def _notifications_bearer() -> str:
    return str(
        os.getenv("DF_NOTIFICATIONS_BEARER")
        or os.getenv("SVC_TO_SVC_BEARER")
        or os.getenv("DF_PRICING_INTERNAL_BEARER")
        or ""
    ).strip()


async def _emit_notification_best_effort(payload: Dict[str, Any], *, context: Dict[str, Any]) -> None:
    url = _notifications_internal_events_url()
    token = _notifications_bearer()
    if not url or not token:
        return

    body = _json_dumps(payload).encode("utf-8")

    def _send() -> None:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    try:
        await asyncio.to_thread(_send)
    except Exception:
        pricing_logger.exception("fusion_notification_emit_failed", extra=context)

_PRICING_CLIENT = None


def get_pricing_client() -> SvcPricingClient:
    global _PRICING_CLIENT
    if _PRICING_CLIENT is None:
        try:
            _PRICING_CLIENT = SvcPricingClient.from_env(service_name="svc-fusion-extension")
        except Exception:
            _PRICING_CLIENT = SvcPricingClient()
    return _PRICING_CLIENT


def _pricing_enabled() -> bool:
    try:
        return bool(getattr(get_pricing_client(), "enabled", False))
    except Exception:
        return False


def _pricing_required() -> bool:
    return str(os.getenv("DF_PRICING_REQUIRED", "0")).strip().lower() in {"1", "true", "yes", "y"}


def _pricing_disabled_reason() -> str:
    if PRICING_IMPORT_ERROR:
        return f"pricing_import_failed: {PRICING_IMPORT_ERROR}"
    return "svc-fusion-extension pricing client is disabled or not configured"


def _normalize_longform_profile_value(value: Any) -> Optional[str]:
    s = (_safe_str(value) or '').strip().lower()
    if not s:
        return None
    if s in {
        "talking_video",
        "talking",
        "tv",
        "legacy",
        "talking-video",
        "talking_video_economy",
        "talking_video_premium",
        "talking-economy",
        "talking-premium",
        "economy_talking_video",
        "premium_talking_video",
        "veed",
        "veed_fabric",
    }:
        return "talking_video"
    if s in {
        "cinematic_video_direction",
        "cinematic",
        "directed",
        "fast",
        "premium",
        "cinematic_fast",
        "cinematic_premium",
        "cinematic-video-fast",
        "cinematic-video-premium",
    }:
        return "cinematic_video_direction"
    return None


def _normalize_quality_tier_value(value: Any) -> str:
    s = (_safe_str(value) or '').strip().lower()
    if s in {'economy', 'eco', 'fast', 'budget', 'veed', 'veed_fabric'}:
        return 'economy'
    return 'premium'


def _quality_tier(payload: Dict[str, Any], tags: Optional[Dict[str, Any]] = None) -> str:
    tags = tags or {}
    intent_tags = _as_dict_loose(tags.get('intent'))
    for value in (
        payload.get('quality_tier'),
        tags.get('quality_tier'),
        tags.get('requested_quality_tier'),
        intent_tags.get('quality_tier'),
        payload.get('output_profile'),
        tags.get('output_profile'),
    ):
        if value is not None:
            return _normalize_quality_tier_value(value)
    return 'premium'


def _provider_hint(payload: Dict[str, Any], tags: Optional[Dict[str, Any]] = None) -> Optional[str]:
    tags = tags or {}
    intent_tags = _as_dict_loose(tags.get('intent'))
    for value in (
        payload.get('provider_hint'),
        tags.get('provider_hint'),
        tags.get('requested_provider_hint'),
        intent_tags.get('provider_hint'),
    ):
        s = _safe_str(value)
        if s:
            return s.strip().lower()
    return None


def _execution_provider_family(payload: Dict[str, Any], tags: Optional[Dict[str, Any]] = None) -> str:
    profile = _pricing_profile(payload, tags)
    if profile == 'cinematic_video_direction':
        return 'premium_cinematic_stack'
    quality = _quality_tier(payload, tags)
    provider_hint = (_provider_hint(payload, tags) or '').strip().lower()
    if profile == 'talking_video' and quality == 'premium' and provider_hint == 'kling':
        return 'kling_avatar'
    return 'veed_fabric'


def _pricing_profile(payload: Dict[str, Any], tags: Optional[Dict[str, Any]] = None) -> str:
    tags = tags or {}
    intent_tags = _as_dict_loose(tags.get("intent"))
    scenario_type = (_safe_str(payload.get("scenario_type")) or _safe_str(tags.get("scenario_type")) or '').lower()
    scenario_mode = (_safe_str(payload.get("scenario_name")) or _safe_str(tags.get("fusion_studio_mode")) or '').lower()

    for value in (
        payload.get("longform_profile"),
        tags.get("longform_profile"),
        tags.get("requested_longform_profile"),
        intent_tags.get("longform_profile"),
        payload.get("output_profile"),
        tags.get("output_profile"),
        payload.get("mode"),
        tags.get("mode"),
        tags.get("api_mode"),
    ):
        normalized = _normalize_longform_profile_value(value)
        if normalized:
            return normalized

    if scenario_mode.startswith('cinematic') or scenario_type in {'brand_film', 'campaign_promo', 'launch_announcement'}:
        return 'cinematic_video_direction'
    return 'talking_video'


def _pricing_variant_for_profile(profile: str) -> Tuple[str, str, str]:
    normalized = str(profile or "").strip().lower()
    if normalized == "talking_video_economy":
        return "TALKING_VIDEO_ECONOMY_30S", "LONGFORM_TALK_ECONOMY_30S", "fusion.longform.talking_video_economy_30s"
    if normalized == "talking_video_premium":
        return "TALKING_VIDEO_PREMIUM_30S", "LONGFORM_TALK_PREMIUM_30S", "fusion.longform.talking_video_premium_30s"
    if normalized == "cinematic_video_direction":
        return "CINEMATIC_VIDEO_DIRECTION", "LONGFORM_CINEMATIC_MIN", "fusion.longform.cinematic_video_direction"
    return "TALKING_VIDEO", "LONGFORM_TALK_MIN", "fusion.longform.talking_video"


def _talking_video_bucket_for_duration(duration_sec: Any, quality_tier: str) -> Tuple[str, str, str, str, int]:
    sec = max(1, _safe_int(duration_sec, 0))
    normalized_quality = "premium" if str(quality_tier or "").strip().lower() == "premium" else "economy"
    if normalized_quality == "premium":
        prefix = "PREMIUM"
        sku_prefix = "LONGFORM_TALK_PREMIUM"
        action_prefix = "fusion.longform.talking_video_premium"
    else:
        prefix = "ECONOMY"
        sku_prefix = "LONGFORM_TALK_ECONOMY"
        action_prefix = "fusion.longform.talking_video_economy"

    if sec <= 10:
        return f"{normalized_quality}_10s", f"TALKING_VIDEO_{prefix}_10S", f"{sku_prefix}_10S", f"{action_prefix}_10s", 10
    if sec <= 20:
        return f"{normalized_quality}_20s", f"TALKING_VIDEO_{prefix}_20S", f"{sku_prefix}_20S", f"{action_prefix}_20s", 20
    return f"{normalized_quality}_30s", f"TALKING_VIDEO_{prefix}_30S", f"{sku_prefix}_30S", f"{action_prefix}_30s", 30


def _economy_bucket_for_duration(duration_sec: Any) -> Tuple[str, str, str, str, int]:
    return _talking_video_bucket_for_duration(duration_sec, "economy")


def _premium_bucket_for_duration(duration_sec: Any) -> Tuple[str, str, str, str, int]:
    return _talking_video_bucket_for_duration(duration_sec, "premium")


def _economy_buffer_seconds() -> int:
    return max(0, _safe_int(os.getenv("LONGFORM_ECONOMY_DURATION_BUFFER_SEC"), 2))


def _talking_video_bucket_meta_from_variant(variant_code: Any) -> Tuple[str, str, str, int]:
    v = (_safe_str(variant_code) or "").strip().upper()
    if v == "TALKING_VIDEO_ECONOMY_10S":
        return "economy_10s", "LONGFORM_TALK_ECONOMY_10S", "fusion.longform.talking_video_economy_10s", 10
    if v == "TALKING_VIDEO_ECONOMY_20S":
        return "economy_20s", "LONGFORM_TALK_ECONOMY_20S", "fusion.longform.talking_video_economy_20s", 20
    if v == "TALKING_VIDEO_PREMIUM_10S":
        return "premium_10s", "LONGFORM_TALK_PREMIUM_10S", "fusion.longform.talking_video_premium_10s", 10
    if v == "TALKING_VIDEO_PREMIUM_20S":
        return "premium_20s", "LONGFORM_TALK_PREMIUM_20S", "fusion.longform.talking_video_premium_20s", 20
    if v == "TALKING_VIDEO_PREMIUM_30S":
        return "premium_30s", "LONGFORM_TALK_PREMIUM_30S", "fusion.longform.talking_video_premium_30s", 30
    return "economy_30s", "LONGFORM_TALK_ECONOMY_30S", "fusion.longform.talking_video_economy_30s", 30


def _economy_bucket_meta_from_variant(variant_code: Any) -> Tuple[str, str, str, int]:
    return _talking_video_bucket_meta_from_variant(variant_code)


def _economy_pricing_duration_seconds(payload: Dict[str, Any], tags: Optional[Dict[str, Any]] = None) -> int:
    """
    Pricing duration must follow the explicit short-form request exactly.
    Do not add buffer here, otherwise 10s can become 20s and 20s can become 30s.
    Execution/rendering may still use separate timing logic.
    """
    tags = tags or {}
    requested = _requested_duration_hint_seconds(payload, tags)
    provider_limit = _economy_provider_limit_sec(payload, tags)

    if requested > 0:
        return min(provider_limit, max(1, requested))

    detected_audio = _safe_int(_effective_voice_audio_source(payload).get("duration_sec"), 0)
    if detected_audio > 0:
        return min(provider_limit, detected_audio)

    return min(provider_limit, estimate_longform_duration_seconds(payload))


def _requested_duration_hint_seconds(payload: Dict[str, Any], tags: Optional[Dict[str, Any]] = None) -> int:
    tags = tags or {}
    video = _as_dict_loose(payload.get("video"))
    intent = _as_dict_loose(payload.get("intent"))
    # Pricing must follow the explicit short-form user request first.
    # Do not let inferred top-level duration fields (often populated from
    # narration/audio/script heuristics) override the selected studio duration.
    candidates = (
        payload.get("pricing_duration_sec"),
        payload.get("requested_duration_sec"),
        video.get("pricing_duration_sec"),
        video.get("requested_duration_sec"),
        video.get("duration_sec"),
        payload.get("video_duration_sec"),
        intent.get("requested_duration_sec"),
        intent.get("duration_sec"),
        tags.get("requested_duration_sec"),
        tags.get("pricing_duration_sec"),
        # legacy fallbacks last
        payload.get("duration_sec"),
        tags.get("duration_sec"),
        _as_dict_loose(tags.get("intent")).get("duration_sec"),
    )
    for value in candidates:
        sec = _safe_int(value, 0)
        if sec > 0:
            return sec
    return 0


def _economy_provider_limit_sec(payload: Dict[str, Any], tags: Optional[Dict[str, Any]] = None) -> int:
    tags = tags or {}
    for value in (
        payload.get("provider_cap_seconds"),
        tags.get("provider_cap_seconds"),
        os.getenv("DF_VEED_FABRIC_MAX_DURATION_SEC"),
        os.getenv("LONGFORM_ECONOMY_PROVIDER_CAP_SECONDS"),
    ):
        v = _safe_int(value, 0)
        if v > 0:
            return max(10, min(60, v))
    return 30


def _economy_effective_duration_seconds(payload: Dict[str, Any], tags: Optional[Dict[str, Any]] = None) -> int:
    tags = tags or {}
    requested = _requested_duration_hint_seconds(payload, tags)
    detected_audio = _safe_int(_effective_voice_audio_source(payload).get("duration_sec"), 0)
    provider_limit = _economy_provider_limit_sec(payload, tags)
    buffer_sec = _economy_buffer_seconds()
    if requested > 0:
        # align pricing to user-requested short form; small buffer to better match rendered output
        return min(provider_limit, max(1, requested + buffer_sec))
    if detected_audio > 0:
        return min(provider_limit, detected_audio)
    return min(provider_limit, estimate_longform_duration_seconds(payload))


def _economy_segment_plan_seconds(total_duration_sec: Any, *, segment_limit_sec: int = 30) -> List[int]:
    total = max(1, _safe_int(total_duration_sec, 0))
    limit = max(10, _safe_int(segment_limit_sec, 30))
    plan: List[int] = []
    remaining = total
    while remaining > 0:
        seg = min(limit, remaining)
        plan.append(seg)
        remaining -= seg
    return plan or [min(limit, total)]


def _pricing_selection(
    payload: Dict[str, Any],
    tags: Optional[Dict[str, Any]],
    duration_sec: Any,
) -> Tuple[str, str, str, str, int]:
    tags = tags or {}
    profile = _pricing_profile(payload, tags)
    quality = _quality_tier(payload, tags)
    if profile == "talking_video" and quality in {"economy", "premium"}:
        return _talking_video_bucket_for_duration(duration_sec, quality)
    if profile == "cinematic_video_direction":
        return "cinematic_video_direction", "CINEMATIC_VIDEO_DIRECTION", "LONGFORM_CINEMATIC_MIN", "fusion.longform.cinematic_video_direction", 60
    return "talking_video_premium", "TALKING_VIDEO", "LONGFORM_TALK_MIN", "fusion.longform.talking_video", 60


def _pricing_duration_seconds(payload: Dict[str, Any], tags: Optional[Dict[str, Any]] = None) -> int:
    tags = tags or {}
    profile = _pricing_profile(payload, tags)
    quality = _quality_tier(payload, tags)
    if profile == "talking_video" and quality == "economy":
        return _economy_effective_duration_seconds(payload, tags)
    if profile == "talking_video" and quality == "premium":
        voice_audio = _effective_voice_audio_source(payload)
        requested_duration_sec = _requested_duration_hint_seconds(payload, tags)
        detected_audio_sec = _safe_int(voice_audio.get("duration_sec"), 0)
        estimated_duration_sec = estimate_longform_duration_seconds(payload)
        return max(1, requested_duration_sec, detected_audio_sec, estimated_duration_sec)
    return estimate_longform_duration_seconds(payload)


def _minutes_units_from_duration(duration_sec: Any) -> str:
    try:
        sec = float(duration_sec or 0)
    except Exception:
        sec = 0.0
    return str(max(1, int(math.ceil(sec / 60.0))))


def _minutes_int_from_duration(duration_sec: Any) -> int:
    try:
        sec = float(duration_sec or 0)
    except Exception:
        sec = 0.0
    return max(1, int(math.ceil(sec / 60.0)))


def _pricing_meta_for_duration(estimated_duration_sec: Any, leaf_sku_code: str) -> Dict[str, Any]:
    minutes = _minutes_int_from_duration(estimated_duration_sec)
    return {
        "estimated_duration_sec": _safe_int(estimated_duration_sec, 0),
        "duration_sec": _safe_int(estimated_duration_sec, 0),
        "minutes": minutes,
        "requested_units": str(minutes),
        "leaf_sku_code": leaf_sku_code,
    }

def _normalize_committed_summary(
    committed: Dict[str, Any],
    summary: Optional[Dict[str, Any]],
    *,
    original_display_estimate: Optional[str] = None,
) -> Dict[str, Any]:
    """
    After a job is committed, the user-visible estimate must collapse to the
    final billed amount. Preserve the original estimate only for audit/debug.
    """
    out = dict(summary or {})
    amount = _safe_str(committed.get("final_amount")) or _safe_str(committed.get("amount"))
    currency = (_safe_str(committed.get("currency")) or "USD").upper()
    if amount:
        display = f"{currency} {amount}"
        out["display_final"] = display
        out["display_estimate"] = display
        out["display_delta"] = f"{currency} 0.00"
        out["display_note"] = "Final charge recorded after execution."
        if original_display_estimate and original_display_estimate != display:
            out["original_display_estimate"] = original_display_estimate
    return out



def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _job_tags_dict(job_row: Dict[str, Any]) -> Dict[str, Any]:
    return _as_dict_loose(job_row.get("tags"))


def _extract_pricing_view(tags: Dict[str, Any]) -> Dict[str, Any]:
    return _as_dict_loose(tags.get("pricing"))


def _extract_pricing_summary_view(tags: Dict[str, Any]) -> Dict[str, Any]:
    return _as_dict_loose(tags.get("pricing_summary"))


async def _persist_job_pricing(conn, job_id: str, pricing: Dict[str, Any], pricing_summary: Optional[Dict[str, Any]] = None) -> None:
    summary = dict(pricing_summary or build_pricing_summary(pricing))
    await conn.execute(
        """
        UPDATE public.longform_jobs
        SET tags = COALESCE(tags, '{}'::jsonb) || jsonb_build_object(
                'pricing', $2::jsonb,
                'pricing_summary', $3::jsonb
            ),
            updated_at = now()
        WHERE id = $1::uuid
        """,
        job_id,
        _json_dumps(pricing or {}),
        _json_dumps(summary or {}),
    )


async def _backfill_reservation_leaf_sku(
    conn,
    *,
    reservation_id: Optional[str],
    variant_code: Optional[str],
    leaf_sku_code: Optional[str],
) -> None:
    rid = _safe_str(reservation_id)
    leaf = _safe_str(leaf_sku_code)
    variant = _safe_str(variant_code)
    if not rid or not leaf:
        return

    await conn.execute(
        """
        UPDATE pricing_credit_reservations
        SET sku_code = $2::text,
            quote_json = jsonb_set(
                jsonb_set(
                    jsonb_set(
                        COALESCE(quote_json, '{}'::jsonb),
                        '{leaf_sku_code}',
                        to_jsonb($2::text),
                        true
                    ),
                    '{sku_code}',
                    to_jsonb($2::text),
                    true
                ),
                '{variant_code}',
                to_jsonb(COALESCE($3::text, quote_json->>'variant_code')),
                true
            ),
            updated_at = now()
        WHERE id = $1::uuid
        """,
        rid,
        leaf,
        variant,
    )


def _script_text_from_payload(payload: Dict[str, Any]) -> str:
    tags = _as_dict_loose(payload.get("tags"))
    intent = _as_dict_loose(payload.get("intent"))
    for value in (
        payload.get("script_text"),
        payload.get("goal"),
        intent.get("goal"),
        payload.get("script"),
        tags.get("script_text"),
        tags.get("goal"),
    ):
        s = _safe_str(value)
        if s:
            return s
    return ""



def _estimate_text_duration_seconds(text: str, *, wpm: int = 150, minimum: int = 30, default: int = 60) -> int:
    words = len(str(text or "").split())
    if words <= 0:
        return default
    return max(minimum, int(round((words / float(wpm)) * 60.0)))



def _duration_hint_seconds(payload: Dict[str, Any]) -> int:
    tags = _as_dict_loose(payload.get("tags"))
    intent = _as_dict_loose(payload.get("intent"))
    for src in (payload, tags, intent, _as_dict_loose(tags.get("intent"))):
        for key in (
            "duration_sec",
            "estimated_duration_sec",
            "audio_duration_sec",
            "voice_audio_duration_sec",
            "track_duration_sec",
        ):
            value = _safe_int(src.get(key), 0)
            if value > 0:
                return value
    return 0


def _extract_audio_candidate_url(src: Dict[str, Any]) -> Optional[str]:
    if not isinstance(src, dict):
        return None
    for key in ("voice_audio_url", "segment_audio_url", "audio_url", "signed_url", "sas_url", "url"):
        value = _safe_str(src.get(key))
        if value and value.startswith(("http://", "https://")):
            return value
    return None


def _effective_voice_audio_source(payload: Dict[str, Any]) -> Dict[str, Any]:
    tags = _as_dict_loose(payload.get("tags"))
    intent = _as_dict_loose(payload.get("intent"))
    assets = _as_dict_loose(payload.get("assets"))
    voice_audio = _as_dict_loose(payload.get("voice_audio"))
    selected_audio = _as_dict_loose(tags.get("selected_audio"))
    audio_result = _as_dict_loose(tags.get("audio_result"))
    resolved_audio = _as_dict_loose(tags.get("resolved_audio"))

    candidate_dicts = [
        voice_audio,
        selected_audio,
        audio_result,
        resolved_audio,
        assets,
        payload,
        tags,
        intent,
        _as_dict_loose(tags.get("intent")),
    ]

    audio_url = None
    for src in candidate_dicts:
        audio_url = _extract_audio_candidate_url(src)
        if audio_url:
            break

    audio_artifact_id = None
    for src in candidate_dicts:
        if not isinstance(src, dict):
            continue
        for key in ("voice_audio_artifact_id", "audio_artifact_id", "artifact_id"):
            value = _safe_str(src.get(key))
            if value:
                audio_artifact_id = value
                break
        if audio_artifact_id:
            break

    duration_sec = 0
    for src in candidate_dicts:
        if not isinstance(src, dict):
            continue
        for key in ("voice_audio_duration_sec", "segment_audio_duration_sec", "audio_duration_sec", "track_duration_sec", "duration_sec"):
            value = _safe_int(src.get(key), 0)
            if value > 0:
                duration_sec = value
                break
        if duration_sec > 0:
            break

    if duration_sec <= 0 and audio_url:
        try:
            with tempfile.TemporaryDirectory(prefix="df_longform_audio_probe_") as td:
                local_path = os.path.join(td, "probe_audio.bin")
                download_to_local(audio_url, local_path, timeout_seconds=180)
                dur = probe_duration_seconds(local_path) or 0.0
                duration_sec = max(0, int(math.ceil(float(dur))))
        except Exception:
            pricing_logger.exception("voice audio duration probe failed url=%s", audio_url)

    return {
        "audio_url": audio_url,
        "audio_artifact_id": audio_artifact_id,
        "duration_sec": duration_sec,
    }


def _attach_voice_audio_metadata(target: Dict[str, Any], voice_audio: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(target or {})
    if _safe_str(voice_audio.get("audio_url")):
        out["voice_audio_url"] = _safe_str(voice_audio.get("audio_url"))
    if _safe_str(voice_audio.get("audio_artifact_id")):
        out["voice_audio_artifact_id"] = _safe_str(voice_audio.get("audio_artifact_id"))
    if _safe_int(voice_audio.get("duration_sec"), 0) > 0:
        out["voice_audio_duration_sec"] = _safe_int(voice_audio.get("duration_sec"), 0)
    return out


def _split_text_into_n_chunks(text: str, count: int) -> List[str]:
    n = max(1, int(count or 1))
    raw = str(text or "").strip()
    if not raw:
        return [""] * n
    if n == 1:
        return [raw]

    sentence_parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", raw) if p.strip()]
    if len(sentence_parts) >= n:
        buckets: List[List[str]] = [[] for _ in range(n)]
        counts = [0] * n
        for sentence in sentence_parts:
            idx = counts.index(min(counts))
            buckets[idx].append(sentence)
            counts[idx] += max(1, len(sentence.split()))
        return [" ".join(part).strip() or raw for part in buckets]

    words = raw.split()
    size = max(1, int(math.ceil(len(words) / float(n))))
    out = [" ".join(words[i:i + size]).strip() for i in range(0, len(words), size)]
    while len(out) < n:
        out.append(out[-1] if out else raw)
    if len(out) > n:
        out = out[: n - 1] + [" ".join(out[n - 1:]).strip()]
    return [item or raw for item in out]


def _assign_audio_windows_to_rows(rows: List[Dict[str, Any]], total_duration_sec: int, voice_audio: Dict[str, Any]) -> List[Dict[str, Any]]:
    total_duration = max(1, _safe_int(total_duration_sec, 0))
    durations = [max(1, _safe_int(_as_dict_loose(row).get("duration_sec"), 1)) for row in rows]
    sum_durations = sum(durations) or len(durations)
    cursor = 0
    assigned: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows):
        current = dict(row)
        if idx == len(rows) - 1:
            end = total_duration
        else:
            seg_dur = max(1, int(round((durations[idx] / float(sum_durations)) * float(total_duration))))
            remaining_min = max(1, len(rows) - idx - 1)
            max_allowed = max(cursor + 1, total_duration - remaining_min)
            end = min(max_allowed, cursor + seg_dur)
            if end <= cursor:
                end = min(total_duration, cursor + 1)
        current["audio_start_sec"] = cursor
        current["audio_end_sec"] = end
        current["duration_sec"] = max(1, end - cursor)
        current["audio_source_kind"] = "original_voice_audio"
        current = _attach_voice_audio_metadata(current, voice_audio)
        assigned.append(current)
        cursor = end
    if assigned:
        assigned[-1]["audio_end_sec"] = total_duration
        assigned[-1]["duration_sec"] = max(1, total_duration - _safe_int(assigned[-1].get("audio_start_sec"), 0))
    return assigned



def estimate_longform_duration_seconds(payload: Dict[str, Any]) -> int:
    tags = _as_dict_loose(payload.get("tags"))
    directed = _as_dict_loose(payload.get("directed_plan")) or _as_dict_loose(tags.get("directed_plan"))
    timeline = _as_dict_loose(directed.get("timeline")) or _as_dict_loose(tags.get("timeline"))

    for mapping in (
        _as_dict_loose(directed.get("segments_by_index")),
        _as_dict_loose(timeline.get("segments_by_index")),
        _as_dict_loose(tags.get("segments_by_index")),
    ):
        if mapping:
            total = 0
            for item in mapping.values():
                total += max(0, _safe_int(_as_dict_loose(item).get("duration_sec"), 0))
            if total > 0:
                return total

    for items in (
        directed.get("segments"),
        timeline.get("segments"),
        directed.get("shots"),
        timeline.get("shots"),
    ):
        if isinstance(items, list) and items:
            total = sum(max(0, _safe_int(_as_dict_loose(item).get("duration_sec"), 0)) for item in items if isinstance(item, dict))
            if total > 0:
                return total

    hinted = _duration_hint_seconds(payload)
    if hinted > 0:
        return hinted

    voice_audio = _effective_voice_audio_source(payload)
    if _safe_int(voice_audio.get("duration_sec"), 0) > 0:
        return _safe_int(voice_audio.get("duration_sec"), 0)

    script_text = _script_text_from_payload(payload)
    return _estimate_text_duration_seconds(script_text)


def build_longform_pricing_preview_spec(user_id: str, payload: Dict[str, Any]) -> PricingPreviewSpec:
    tags = _as_dict_loose(payload.get("tags"))
    profile = _pricing_profile(payload, tags)
    quality = _quality_tier(payload, tags)
    requested_duration_sec = _requested_duration_hint_seconds(payload, tags)
    provider_limit_sec = _economy_provider_limit_sec(payload, tags) if (profile == "talking_video" and quality in {"economy", "premium"}) else 0

    request_fingerprint = hashlib.sha256(
        json.dumps({"user_id": str(user_id), "payload": payload}, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()

    if profile == "talking_video" and quality in {"economy", "premium"}:
        requested_for_pricing = (
            _safe_int(payload.get("pricing_duration_sec"), 0)
            or requested_duration_sec
            or _economy_pricing_duration_seconds(payload, tags)
        )

        pricing_duration_sec = min(provider_limit_sec, max(1, requested_for_pricing))
        segmented = requested_for_pricing > provider_limit_sec

        if segmented:
            pricing_segment_plan = _economy_segment_plan_seconds(
                requested_for_pricing,
                segment_limit_sec=provider_limit_sec,
            )
            first_segment_sec = pricing_segment_plan[0]
            bucket_code, variant_code, leaf_sku_code, service_action, bucket_max_sec = _talking_video_bucket_for_duration(first_segment_sec, quality)
            requested_units = str(len(pricing_segment_plan))
            requested_minutes = len(pricing_segment_plan)
            segment_plan = pricing_segment_plan
            effective_estimated_duration_sec = requested_for_pricing
        else:
            bucket_code, variant_code, leaf_sku_code, service_action, bucket_max_sec = _talking_video_bucket_for_duration(pricing_duration_sec, quality)
            requested_units = "1"
            requested_minutes = 1
            segment_plan = [pricing_duration_sec]
            effective_estimated_duration_sec = pricing_duration_sec

        meta = {
            "longform_profile": profile,
            "service_action": service_action,
            "variant_code": variant_code,
            "leaf_sku_code": leaf_sku_code,
            "aspect_ratio": _safe_str(payload.get("aspect_ratio")) or "9:16",
            "camera_angle": _safe_str(payload.get("camera_angle")) or _safe_str(tags.get("camera_angle")),
            "camera_framing": _safe_str(payload.get("camera_framing")) or _safe_str(tags.get("camera_framing")),
            "camera_motion_style": _safe_str(payload.get("camera_motion_style")) or _safe_str(tags.get("camera_motion_style")),
            "background_mode": _safe_str(payload.get("background_mode")) or _safe_str(tags.get("background_mode")),
            "quality_tier": quality,
            "provider_hint": _provider_hint(payload, tags),
            "execution_provider_family": _execution_provider_family(payload, tags),
            "preview_fingerprint": request_fingerprint,
            "estimated_duration_sec": effective_estimated_duration_sec,
            "duration_sec": effective_estimated_duration_sec,
            "requested_duration_sec": requested_duration_sec,
            "detected_audio_duration_sec": _safe_int(_effective_voice_audio_source(payload).get("duration_sec"), 0),
            "provider_limit_sec": provider_limit_sec,
            "units": requested_units,
            "requested_units": requested_units,
            "minutes": requested_minutes,
            "requested_minutes": requested_minutes,
            "quantity": int(requested_units),
            "billing_quantity": int(requested_units),
            "duration_minutes": requested_minutes,
            "talking_video_bucket_code": bucket_code,
            "talking_video_bucket_max_sec": bucket_max_sec,
            "economy_bucket_code": bucket_code if quality == "economy" else None,
            "economy_bucket_max_sec": bucket_max_sec if quality == "economy" else None,
            "premium_bucket_code": bucket_code if quality == "premium" else None,
            "premium_bucket_max_sec": bucket_max_sec if quality == "premium" else None,
            "segmented": segmented,
            "segment_count": len(segment_plan),
            "segment_durations_sec": segment_plan,
            "pricing_strategy": f"{quality}_segmented_sum" if segmented else f"{quality}_actual_short_form",
            "selected_mode": f"talking_video_{quality}",
        }
        return PricingPreviewSpec(
            user_id=str(user_id),
            service_name="svc-fusion-extension",
            service_action=service_action,
            sku_code=variant_code,
            units=requested_units,
            external_ref_type="longform_job_preview",
            external_ref_id=f"preview:{request_fingerprint}",
            idempotency_key=f"svc-fusion-extension:preview:{user_id}:{request_fingerprint}",
            meta=meta,
        )

    variant_code, leaf_sku_code, service_action = _pricing_variant_for_profile(profile)
    estimated_duration_sec = estimate_longform_duration_seconds(payload)
    requested_units = _minutes_units_from_duration(estimated_duration_sec)
    requested_minutes = _minutes_int_from_duration(estimated_duration_sec)
    meta = {
        "longform_profile": profile,
        "variant_code": variant_code,
        "leaf_sku_code": leaf_sku_code,
        "aspect_ratio": _safe_str(payload.get("aspect_ratio")) or "9:16",
        "camera_angle": _safe_str(payload.get("camera_angle")) or _safe_str(tags.get("camera_angle")),
        "camera_framing": _safe_str(payload.get("camera_framing")) or _safe_str(tags.get("camera_framing")),
        "camera_motion_style": _safe_str(payload.get("camera_motion_style")) or _safe_str(tags.get("camera_motion_style")),
        "background_mode": _safe_str(payload.get("background_mode")) or _safe_str(tags.get("background_mode")),
        "preview_fingerprint": request_fingerprint,
        **_pricing_meta_for_duration(estimated_duration_sec, leaf_sku_code),
        "units": requested_units,
        "requested_units": requested_units,
        "minutes": requested_minutes,
        "requested_minutes": requested_minutes,
        "quantity": requested_minutes,
        "billing_quantity": requested_minutes,
        "duration_minutes": requested_minutes,
    }
    return PricingPreviewSpec(
        user_id=str(user_id),
        service_name="svc-fusion-extension",
        service_action=service_action,
        sku_code=variant_code,
        units=requested_units,
        external_ref_type="longform_job_preview",
        external_ref_id=f"preview:{request_fingerprint}",
        idempotency_key=f"svc-fusion-extension:preview:{user_id}:{request_fingerprint}",
        meta=meta,
    )


async def preview_longform_pricing(user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    client = get_pricing_client()
    if not bool(getattr(client, "enabled", False)):
        raise PricingClientError("PRICING_CLIENT_DISABLED")
    spec = build_longform_pricing_preview_spec(user_id, payload)
    resp = await client.preview(build_preview_request(spec))
    artifact = make_preview_artifact(
        resp,
        service_name="svc-fusion-extension",
        service_action=spec.service_action,
        sku_code=spec.sku_code,
        meta=spec.meta,
    )
    pricing = dict(artifact.get("pricing") or {})
    pricing["enabled"] = True
    pricing["state"] = "quoted"
    pricing["variant_code"] = spec.sku_code
    pricing["sku_code"] = spec.meta.get("leaf_sku_code")
    pricing["leaf_sku_code"] = spec.meta.get("leaf_sku_code")
    pricing_summary = dict(artifact.get("pricing_summary") or build_pricing_summary(pricing))
    return {"pricing": pricing, "pricing_summary": pricing_summary}


async def reserve_longform_pricing_for_job(conn, *, user_id: str, job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    client = get_pricing_client()
    tags = _as_dict_loose(payload.get("tags"))
    profile = _pricing_profile(payload, tags)
    quality = _quality_tier(payload, tags)

    if profile == "talking_video" and quality in {"economy", "premium"}:
        requested_duration_sec = _requested_duration_hint_seconds(payload, tags)
        provider_limit_sec = _economy_provider_limit_sec(payload, tags)

        confirmation = _as_dict_loose(payload.get("pricing_confirmation"))
        confirmed_variant_code = _safe_str(confirmation.get("variant_code"))
        confirmed_leaf_sku_code = _safe_str(confirmation.get("leaf_sku_code")) or _safe_str(confirmation.get("sku_code"))
        confirmed_units = _safe_str(confirmation.get("estimated_units")) or _safe_str(confirmation.get("requested_units"))

        requested_for_pricing = (
            _safe_int(payload.get("pricing_duration_sec"), 0)
            or requested_duration_sec
            or _economy_pricing_duration_seconds(payload, tags)
        )

        pricing_duration_sec = min(provider_limit_sec, max(1, requested_for_pricing))
        segmented = requested_for_pricing > provider_limit_sec

        if segmented:
            pricing_segment_plan = _economy_segment_plan_seconds(
                requested_for_pricing,
                segment_limit_sec=provider_limit_sec,
            )
            execution_segment_plan = pricing_segment_plan
            first_segment_sec = pricing_segment_plan[0]
            bucket_code, variant_code, leaf_sku_code, service_action, bucket_max_sec = _talking_video_bucket_for_duration(first_segment_sec, quality)
            units = str(len(pricing_segment_plan))
            requested_minutes = len(pricing_segment_plan)
            effective_estimated_duration_sec = requested_for_pricing
        else:
            execution_segment_plan = [pricing_duration_sec]
            bucket_code, variant_code, leaf_sku_code, service_action, bucket_max_sec = _talking_video_bucket_for_duration(pricing_duration_sec, quality)
            units = "1"
            requested_minutes = 1
            effective_estimated_duration_sec = pricing_duration_sec

        if confirmed_variant_code:
            confirmed_bucket_code, derived_leaf_sku_code, derived_service_action, confirmed_bucket_max_sec = _talking_video_bucket_meta_from_variant(confirmed_variant_code)
            bucket_code = confirmed_bucket_code
            variant_code = confirmed_variant_code
            leaf_sku_code = confirmed_leaf_sku_code or derived_leaf_sku_code
            service_action = derived_service_action
            bucket_max_sec = confirmed_bucket_max_sec
            if confirmed_units:
                units = confirmed_units
                requested_minutes = max(1, _safe_int(units, 1))

        if not bool(getattr(client, "enabled", False)):
            if _pricing_required():
                raise PricingClientError(f"PRICING_CLIENT_DISABLED: {_pricing_disabled_reason()}")
            pricing = {
                "enabled": False,
                "state": "disabled",
                "variant_code": variant_code,
                "sku_code": leaf_sku_code,
                "leaf_sku_code": leaf_sku_code,
                "message": _pricing_disabled_reason(),
            }
            await _persist_job_pricing(conn, job_id, pricing, build_pricing_summary(pricing))
            return pricing

        reserve_meta = {
            "longform_profile": profile,
            "longform_job_id": str(job_id),
            "service_job_id": str(job_id),
            "service_job_table": "longform_jobs",
            "pricing_entity_kind": "service_job",
            "omit_studio_job_id": True,
            "external_ref_type": "longform_job",
            "service_name": "svc-fusion-extension",
            "service_action": service_action,
            "variant_code": variant_code,
            "leaf_sku_code": leaf_sku_code,
            "aspect_ratio": _safe_str(payload.get("aspect_ratio")) or "9:16",
            "camera_angle": _safe_str(payload.get("camera_angle")) or _safe_str(tags.get("camera_angle")),
            "camera_framing": _safe_str(payload.get("camera_framing")) or _safe_str(tags.get("camera_framing")),
            "camera_motion_style": _safe_str(payload.get("camera_motion_style")) or _safe_str(tags.get("camera_motion_style")),
            "background_mode": _safe_str(payload.get("background_mode")) or _safe_str(tags.get("background_mode")),
            "quality_tier": quality,
            "provider_hint": _provider_hint(payload, tags),
            "execution_provider_family": _execution_provider_family(payload, tags),
            "estimated_duration_sec": effective_estimated_duration_sec,
            "duration_sec": effective_estimated_duration_sec,
            "requested_duration_sec": requested_duration_sec,
            "detected_audio_duration_sec": _safe_int(_effective_voice_audio_source(payload).get("duration_sec"), 0),
            "provider_limit_sec": provider_limit_sec,
            "units": units,
            "requested_units": units,
            "minutes": requested_minutes,
            "requested_minutes": requested_minutes,
            "quantity": int(units),
            "billing_quantity": int(units),
            "duration_minutes": requested_minutes,
            "talking_video_bucket_code": bucket_code,
            "talking_video_bucket_max_sec": bucket_max_sec,
            "economy_bucket_code": bucket_code if quality == "economy" else None,
            "economy_bucket_max_sec": bucket_max_sec if quality == "economy" else None,
            "premium_bucket_code": bucket_code if quality == "premium" else None,
            "premium_bucket_max_sec": bucket_max_sec if quality == "premium" else None,
            "segmented": segmented,
            "segment_count": len(execution_segment_plan),
            "segment_durations_sec": execution_segment_plan,
            "pricing_strategy": f"{quality}_segmented_sum" if segmented else f"{quality}_actual_short_form",
            "selected_mode": f"talking_video_{quality}",
        }
        reserve_spec = PricingReserveSpec(
            user_id=str(user_id),
            service_name="svc-fusion-extension",
            service_action=service_action,
            sku_code=variant_code,
            units=units,
            external_ref_type="longform_job",
            external_ref_id=str(job_id),
            idempotency_key=f"svc-fusion-extension:job:{job_id}:reserve",
            meta=reserve_meta,
            quote_id=_safe_str(confirmation.get("quote_id")),
            preview_fingerprint=_safe_str(confirmation.get("preview_fingerprint")),
        )
        resp = await client.reserve(build_reserve_request(reserve_spec))
        artifact = make_reserved_artifact(
            resp,
            service_name="svc-fusion-extension",
            service_action=service_action,
            sku_code=variant_code,
            estimated_units=units,
            unit_type="job",
            meta=reserve_meta,
        )
        pricing = dict(artifact.get("pricing") or {})
        pricing["enabled"] = True
        pricing["state"] = "reserved"
        pricing["quote_id"] = _safe_str(getattr(resp, "quote_id", None)) or pricing.get("quote_id") or _safe_str(confirmation.get("quote_id"))
        pricing["preview_fingerprint"] = _safe_str(getattr(resp, "preview_fingerprint", None)) or pricing.get("preview_fingerprint") or _safe_str(confirmation.get("preview_fingerprint"))
        pricing["variant_code"] = variant_code
        pricing["sku_code"] = leaf_sku_code
        pricing["leaf_sku_code"] = leaf_sku_code
        summary = dict(artifact.get("pricing_summary") or build_pricing_summary(pricing))
        await _backfill_reservation_leaf_sku(
            conn,
            reservation_id=_safe_str(pricing.get("reservation_id")) or _safe_str(getattr(resp, "reservation_id", None)),
            variant_code=variant_code,
            leaf_sku_code=leaf_sku_code,
        )
        await _persist_job_pricing(conn, job_id, pricing, summary)
        return pricing

    # non-economy existing behavior
    if not bool(getattr(client, "enabled", False)):
        if _pricing_required():
            raise PricingClientError(f"PRICING_CLIENT_DISABLED: {_pricing_disabled_reason()}")
        variant_code, leaf_sku_code, _ = _pricing_variant_for_profile(profile)
        pricing = {
            "enabled": False,
            "state": "disabled",
            "variant_code": variant_code,
            "sku_code": leaf_sku_code,
            "leaf_sku_code": leaf_sku_code,
            "message": _pricing_disabled_reason(),
        }
        await _persist_job_pricing(conn, job_id, pricing, build_pricing_summary(pricing))
        return pricing

    variant_code, leaf_sku_code, service_action = _pricing_variant_for_profile(profile)
    estimated_duration_sec = estimate_longform_duration_seconds(payload)
    units = _minutes_units_from_duration(estimated_duration_sec)
    requested_minutes = _minutes_int_from_duration(estimated_duration_sec)
    confirmation = _as_dict_loose(payload.get("pricing_confirmation"))
    reserve_meta = {
        "longform_profile": profile,
        "longform_job_id": str(job_id),
        "service_job_id": str(job_id),
        "service_job_table": "longform_jobs",
        "pricing_entity_kind": "service_job",
        "omit_studio_job_id": True,
        "external_ref_type": "longform_job",
        "service_name": "svc-fusion-extension",
        "service_action": service_action,
        "variant_code": variant_code,
        "leaf_sku_code": leaf_sku_code,
        "aspect_ratio": _safe_str(payload.get("aspect_ratio")) or "9:16",
        "camera_angle": _safe_str(payload.get("camera_angle")) or _safe_str(tags.get("camera_angle")),
        "camera_framing": _safe_str(payload.get("camera_framing")) or _safe_str(tags.get("camera_framing")),
        "camera_motion_style": _safe_str(payload.get("camera_motion_style")) or _safe_str(tags.get("camera_motion_style")),
        "background_mode": _safe_str(payload.get("background_mode")) or _safe_str(tags.get("background_mode")),
        **_pricing_meta_for_duration(estimated_duration_sec, leaf_sku_code),
        "units": units,
        "requested_units": units,
        "minutes": requested_minutes,
        "requested_minutes": requested_minutes,
        "quantity": requested_minutes,
        "billing_quantity": requested_minutes,
        "duration_minutes": requested_minutes,
    }
    reserve_spec = PricingReserveSpec(
        user_id=str(user_id),
        service_name="svc-fusion-extension",
        service_action=service_action,
        sku_code=variant_code,
        units=units,
        external_ref_type="longform_job",
        external_ref_id=str(job_id),
        idempotency_key=f"svc-fusion-extension:job:{job_id}:reserve",
        meta=reserve_meta,
        quote_id=_safe_str(confirmation.get("quote_id")),
        preview_fingerprint=_safe_str(confirmation.get("preview_fingerprint")),
    )
    resp = await client.reserve(build_reserve_request(reserve_spec))
    artifact = make_reserved_artifact(
        resp,
        service_name="svc-fusion-extension",
        service_action=service_action,
        sku_code=variant_code,
        estimated_units=units,
        unit_type="minute",
        meta=reserve_meta,
    )
    pricing = dict(artifact.get("pricing") or {})
    pricing["enabled"] = True
    pricing["state"] = "reserved"
    pricing["quote_id"] = _safe_str(getattr(resp, "quote_id", None)) or pricing.get("quote_id") or _safe_str(confirmation.get("quote_id"))
    pricing["preview_fingerprint"] = _safe_str(getattr(resp, "preview_fingerprint", None)) or pricing.get("preview_fingerprint") or _safe_str(confirmation.get("preview_fingerprint"))
    pricing["variant_code"] = variant_code
    pricing["sku_code"] = leaf_sku_code
    pricing["leaf_sku_code"] = leaf_sku_code
    summary = dict(artifact.get("pricing_summary") or build_pricing_summary(pricing))
    await _persist_job_pricing(conn, job_id, pricing, summary)
    return pricing


async def commit_longform_pricing_for_job(conn, *, job_row: Dict[str, Any], final_duration_sec: Optional[float]) -> Dict[str, Any]:
    tags = _job_tags_dict(job_row)
    pricing = _extract_pricing_view(tags)
    if not pricing or not pricing.get("enabled"):
        return pricing
    reservation_id = _safe_str(pricing.get("reservation_id"))
    user_id = _safe_str(job_row.get("user_id"))
    if not reservation_id or not user_id:
        raise RuntimeError(
            f"LONGFORM_PRICING_COMMIT_MISSING_CONTEXT reservation_id={reservation_id!r} user_id={user_id!r}"
        )

    pricing_meta = _as_dict_loose(pricing.get("meta"))
    reserved_variant_code = _safe_str(pricing.get("variant_code")) or ""
    reserved_leaf_sku_code = _safe_str(pricing.get("leaf_sku_code")) or _safe_str(pricing.get("sku_code"))
    reserved_bucket_code = _safe_str(pricing_meta.get("talking_video_bucket_code")) or _safe_str(pricing_meta.get("economy_bucket_code")) or _safe_str(pricing_meta.get("premium_bucket_code")) or ""
    reserved_service_action = (
        _safe_str(pricing_meta.get("service_action"))
        or _safe_str(pricing.get("service_action"))
        or ""
    )

    is_talking_video_bucket = (
        reserved_bucket_code.startswith("economy_")
        or reserved_bucket_code.startswith("premium_")
        or reserved_variant_code.startswith("TALKING_VIDEO_ECONOMY_")
        or reserved_variant_code.startswith("TALKING_VIDEO_PREMIUM_")
    )

    if is_talking_video_bucket:
        effective_variant_code = reserved_variant_code
        effective_leaf_sku_code = reserved_leaf_sku_code
        effective_bucket_code = reserved_bucket_code
        effective_bucket_max_sec = (
            pricing_meta.get("talking_video_bucket_max_sec")
            or pricing_meta.get("economy_bucket_max_sec")
            or pricing_meta.get("premium_bucket_max_sec")
        )

        if not effective_variant_code:
            seed_duration_sec = (
                pricing_meta.get("requested_duration_sec")
                or pricing_meta.get("duration_sec")
                or final_duration_sec
            )
            quality = "premium" if reserved_bucket_code.startswith("premium_") or reserved_variant_code.startswith("TALKING_VIDEO_PREMIUM_") else "economy"
            effective_bucket_code, effective_variant_code, effective_leaf_sku_code, derived_service_action, effective_bucket_max_sec = _talking_video_bucket_for_duration(seed_duration_sec, quality)
            reserved_service_action = reserved_service_action or derived_service_action
        else:
            derived_bucket_code, derived_leaf_sku_code, derived_service_action, derived_bucket_max_sec = _talking_video_bucket_meta_from_variant(effective_variant_code)
            effective_bucket_code = effective_bucket_code or derived_bucket_code
            effective_leaf_sku_code = effective_leaf_sku_code or derived_leaf_sku_code
            reserved_service_action = reserved_service_action or derived_service_action
            effective_bucket_max_sec = effective_bucket_max_sec or derived_bucket_max_sec

        units = _safe_str(pricing.get("estimated_units")) or _safe_str(pricing_meta.get("requested_units")) or "1"
        minutes = max(1, _safe_int(units, 1))
        service_action = reserved_service_action or ("fusion.longform.talking_video_premium_30s" if str(effective_bucket_code or "").startswith("premium_") else "fusion.longform.talking_video_economy_30s")
    else:
        effective_variant_code = reserved_variant_code
        effective_leaf_sku_code = reserved_leaf_sku_code
        effective_bucket_code = reserved_bucket_code
        effective_bucket_max_sec = (
            pricing_meta.get("talking_video_bucket_max_sec")
            or pricing_meta.get("economy_bucket_max_sec")
            or pricing_meta.get("premium_bucket_max_sec")
        )
        units = _minutes_units_from_duration(final_duration_sec)
        minutes = _minutes_int_from_duration(final_duration_sec)
        service_action = reserved_service_action or _pricing_variant_for_profile(_pricing_profile({}, tags))[2]

    commit_meta = {
        "longform_profile": _pricing_profile({}, tags),
        "longform_job_id": str(job_row["id"]),
        "service_job_id": str(job_row["id"]),
        "service_job_table": "longform_jobs",
        "pricing_entity_kind": "service_job",
        "omit_studio_job_id": True,
        "external_ref_type": "longform_job",
        "service_name": "svc-fusion-extension",
        "service_action": service_action,
        "actual_duration_sec": final_duration_sec,
        "leaf_sku_code": effective_leaf_sku_code,
        "variant_code": effective_variant_code,
        "units": units,
        "requested_units": units,
        "minutes": minutes,
        "talking_video_bucket_code": effective_bucket_code or None,
        "talking_video_bucket_max_sec": effective_bucket_max_sec,
        "economy_bucket_code": effective_bucket_code if str(effective_bucket_code or "").startswith("economy_") else None,
        "economy_bucket_max_sec": effective_bucket_max_sec if str(effective_bucket_code or "").startswith("economy_") else None,
        "premium_bucket_code": effective_bucket_code if str(effective_bucket_code or "").startswith("premium_") else None,
        "premium_bucket_max_sec": effective_bucket_max_sec if str(effective_bucket_code or "").startswith("premium_") else None,
    }
    resp = await get_pricing_client().commit(
        build_commit_request(
            PricingCommitSpec(
                user_id=str(user_id),
                reservation_id=reservation_id,
                actual_units=units,
                external_ref_type="longform_job",
                external_ref_id=str(job_row["id"]),
                idempotency_key=f"svc-fusion-extension:job:{job_row['id']}:commit",
                meta=commit_meta,
            )
        )
    )
    artifact = make_committed_artifact(resp, base_pricing=pricing, actual_units=units, meta=commit_meta)
    committed = dict(artifact.get("pricing") or {})
    committed["enabled"] = True
    committed["state"] = "committed"
    committed["quote_id"] = committed.get("quote_id") or pricing.get("quote_id")
    committed["preview_fingerprint"] = committed.get("preview_fingerprint") or pricing.get("preview_fingerprint")
    committed["variant_code"] = effective_variant_code or pricing.get("variant_code")
    committed["sku_code"] = effective_leaf_sku_code or pricing.get("leaf_sku_code") or pricing.get("sku_code")
    committed["leaf_sku_code"] = effective_leaf_sku_code or pricing.get("leaf_sku_code")
    original_display_estimate = _safe_str(_as_dict_loose(pricing.get("summary")).get("display_estimate")) or _safe_str(_extract_pricing_summary_view(tags).get("display_estimate"))
    summary = _normalize_committed_summary(
        committed,
        artifact.get("pricing_summary") or build_pricing_summary(committed),
        original_display_estimate=original_display_estimate,
    )
    committed["summary"] = dict(summary)
    committed["estimated_units"] = committed.get("actual_units") or committed.get("estimated_units")
    await _backfill_reservation_leaf_sku(
        conn,
        reservation_id=reservation_id,
        variant_code=effective_variant_code,
        leaf_sku_code=effective_leaf_sku_code,
    )
    await _persist_job_pricing(conn, str(job_row["id"]), committed, summary)
    return committed


async def release_longform_pricing_for_job(conn, *, job_id: str, user_id: Optional[str], reason: str, tags: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    tags = tags or {}
    pricing = _extract_pricing_view(tags)
    if not pricing:
        row = await conn.fetchrow("SELECT tags FROM public.longform_jobs WHERE id = $1::uuid", job_id)
        tags = _as_dict_loose(row["tags"]) if row else {}
        pricing = _extract_pricing_view(tags)
    if not pricing or not pricing.get("enabled"):
        return pricing
    reservation_id = _safe_str(pricing.get("reservation_id"))
    if not reservation_id or not user_id:
        raise RuntimeError(
            f"LONGFORM_PRICING_COMMIT_MISSING_CONTEXT reservation_id={reservation_id!r} user_id={user_id!r}"
        )
    resp = await get_pricing_client().release(
        build_release_request(
            PricingReleaseSpec(
                user_id=str(user_id),
                reservation_id=reservation_id,
                reason=reason,
                external_ref_type="longform_job",
                external_ref_id=str(job_id),
                idempotency_key=f"svc-fusion-extension:job:{job_id}:release",
                meta={
                    "longform_profile": _pricing_profile({}, tags),
                    "longform_job_id": str(job_id),
                    "leaf_sku_code": _safe_str(pricing.get("leaf_sku_code")),
                    "units": _safe_str(pricing.get("estimated_units")) or "1",
                    "requested_units": _safe_str(pricing.get("estimated_units")) or "1",
                    "minutes": _safe_int(pricing.get("estimated_units"), 1),
                },
            )
        )
    )
    artifact = make_released_artifact(resp, base_pricing=pricing, meta={"release_reason": reason})
    released = dict(artifact.get("pricing") or {})
    released["enabled"] = True
    released["state"] = "released"
    released["quote_id"] = released.get("quote_id") or pricing.get("quote_id")
    released["preview_fingerprint"] = released.get("preview_fingerprint") or pricing.get("preview_fingerprint")
    released["variant_code"] = pricing.get("variant_code")
    released["sku_code"] = pricing.get("leaf_sku_code") or pricing.get("sku_code")
    released["leaf_sku_code"] = pricing.get("leaf_sku_code")
    summary = dict(artifact.get("pricing_summary") or build_pricing_summary(released))
    await _persist_job_pricing(conn, str(job_id), released, summary)
    return released


def _normalize_shot_type_value(value: Any) -> ShotType:
    s = (_safe_str(value) or "montage").strip().lower()
    mapping = {
        "hook_open": ShotType.hook_open,
        "hook_close": ShotType.transition_bridge,
        "talking_head": ShotType.talking_head,
        "presenter_anchor": ShotType.talking_head,
        "presenter_with_motion_bg": ShotType.talking_head,
        "direct_address": ShotType.talking_head,
        "presenter_open": ShotType.talking_head,
        "presenter_close": ShotType.talking_head,
        "host_intro": ShotType.talking_head,
        "host_outro": ShotType.outro_cta,
        "spokesperson": ShotType.talking_head,
        "voiceover_broll": ShotType.voiceover_broll,
        "motion_plate_realistic": ShotType.voiceover_broll,
        "transition_cinematic": ShotType.transition_bridge,
        "transition": ShotType.transition_bridge,
        "stylized_transition": ShotType.transition_bridge,
        "camera_move": ShotType.transition_bridge,
        "montage": ShotType.montage,
        "closing_montage": ShotType.montage,
        "title_card": ShotType.title_card,
        "quote_card": ShotType.quote_card,
        "stat_card": ShotType.stat_card,
        "product_showcase": ShotType.product_showcase,
        "screen_demo": ShotType.screen_demo,
        "social_proof": ShotType.social_proof,
        "outro_cta": ShotType.outro_cta,
        "logo_sting": ShotType.logo_sting,
    }
    return mapping.get(s, ShotType.montage)


def _normalize_render_route_value(value: Any) -> RenderRoute:
    s = (_safe_str(value) or RenderRoute.internal_montage.value).strip().lower()
    mapping = {
        RenderRoute.fusion.value: RenderRoute.fusion,
        RenderRoute.audio_broll.value: RenderRoute.audio_broll,
        RenderRoute.internal_card.value: RenderRoute.internal_card,
        RenderRoute.internal_montage.value: RenderRoute.internal_montage,
        RenderRoute.imported_asset.value: RenderRoute.imported_asset,
        RenderRoute.legacy_segment_pipeline.value: RenderRoute.legacy_segment_pipeline,
    }
    if s in mapping:
        return mapping[s]
    if s in {"card", "title_card"}:
        return RenderRoute.internal_card
    if s in {"montage", "broll", "voiceover_broll"}:
        return RenderRoute.audio_broll if s == "voiceover_broll" else RenderRoute.internal_montage
    return RenderRoute.internal_montage


def _normalize_external_segment_rows_from_plan(plan: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], List[ShotSpec], TimelineManifest, Optional[str]]:
    """
    Convert an externally supplied directed plan into:
    - segment rows for longform_segments
    - segments_by_index for worker lookup (preserving original custom shot_type/render_route/provider metadata)
    - normalized ShotSpec list and TimelineManifest for LongformJobView / GET route compatibility
    """
    if not isinstance(plan, dict) or not plan:
        empty_timeline = TimelineManifest(project_id="external", aspect_ratio="9:16", shots=[])
        return [], {}, [], empty_timeline, None

    timeline_in = _as_dict_loose(plan.get("timeline"))
    aspect_ratio = (
        _safe_str(plan.get("aspect_ratio"))
        or _safe_str(timeline_in.get("aspect_ratio"))
        or "9:16"
    )

    raw_by_index: Dict[str, Dict[str, Any]] = {}

    explicit_mapping = _as_dict_loose(plan.get("segments_by_index"))
    for k, v in explicit_mapping.items():
        item = _as_dict_loose(v)
        if not item:
            continue
        if item.get("segment_index") is None:
            try:
                item["segment_index"] = int(k)
            except Exception:
                pass
        idx = _safe_int(item.get("segment_index", k), -1)
        if idx >= 0:
            raw_by_index[str(idx)] = item

    def _ingest_items(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = _safe_int(item.get("segment_index", item.get("shot_index")), -1)
            if idx < 0:
                continue
            merged = dict(item)
            merged.setdefault("segment_index", idx)
            raw_by_index[str(idx)] = merged

    _ingest_items(plan.get("segments"))
    _ingest_items(plan.get("shots"))
    _ingest_items(timeline_in.get("segments"))
    _ingest_items(timeline_in.get("shots"))

    segment_rows: List[Dict[str, Any]] = []
    normalized_shots: List[ShotSpec] = []

    for idx_s, item in sorted(raw_by_index.items(), key=lambda kv: int(kv[0])):
        idx = int(idx_s)
        script = _as_dict_loose(item.get("script"))
        spoken = (
            _safe_str(item.get("text_chunk"))
            or _safe_str(script.get("spoken_text"))
            or _safe_str(script.get("voiceover_text"))
        )
        if not spoken:
            onscreen = script.get("onscreen_text") or item.get("onscreen_text")
            if isinstance(onscreen, list):
                spoken = " ".join(str(x).strip() for x in onscreen if str(x).strip()) or None
            elif onscreen:
                spoken = _safe_str(onscreen)
        if not spoken:
            spoken = _safe_str(item.get("title")) or _safe_str(item.get("shot_type")) or "Story beat"

        duration_sec = _safe_int(item.get("duration_sec"), 5) or 5

        segment_rows.append(
            {
                "segment_index": idx,
                "status": SegmentStatus.queued.value,
                "duration_sec": duration_sec,
                "text_chunk": spoken,
            }
        )

        beat_id = _safe_str(item.get("beat_id")) or f"external_beat_{idx:02d}"
        shot_id = _safe_str(item.get("shot_id")) or f"{beat_id}_shot_{idx:02d}"
        title = _safe_str(item.get("title")) or _safe_str(item.get("shot_type")) or f"shot_{idx:02d}"

        normalized_shots.append(
            ShotSpec(
                shot_id=shot_id,
                beat_id=beat_id,
                shot_index=idx,
                shot_type=_normalize_shot_type_value(item.get("shot_type")),
                render_route=_normalize_render_route_value(item.get("render_route")),
                duration_sec=duration_sec,
                title=title,
                script=ScriptSpec(
                    spoken_text=_safe_str(script.get("spoken_text")),
                    voiceover_text=_safe_str(script.get("voiceover_text")) or _safe_str(script.get("spoken_text")),
                    subtitle_text=_safe_str(script.get("subtitle_text")) or _safe_str(script.get("spoken_text")),
                    onscreen_text=[str(x) for x in _as_list_loose(script.get("onscreen_text"))],
                ),
                visual_brief=_safe_str(item.get("visual_brief")),
                asset_requirements=_as_dict_loose(item.get("asset_requirements")),
                resolved_assets=_as_dict_loose(item.get("resolved_assets")),
                transition_in=_safe_str(item.get("transition_in")),
                transition_out=_safe_str(item.get("transition_out")),
                meta={"raw_shot_type": _safe_str(item.get("shot_type")), **_as_dict_loose(item.get("meta"))},
            )
        )

    timeline = TimelineManifest(
        project_id=_safe_str(timeline_in.get("project_id")) or "external",
        aspect_ratio=aspect_ratio if aspect_ratio in {"16:9", "9:16", "1:1"} else "9:16",
        shots=normalized_shots,
        subtitle_track_url=_safe_str(timeline_in.get("subtitle_track_url")),
        music_track_url=_safe_str(timeline_in.get("music_track_url")),
        overlay_meta={**_as_dict_loose(timeline_in.get("overlay_meta")), "stitch_mode": _safe_str(_as_dict_loose(timeline_in.get("overlay_meta")).get("stitch_mode")) or "xfade"},
        export_meta=_as_dict_loose(timeline_in.get("export_meta")),
    )

    return segment_rows, raw_by_index, normalized_shots, timeline, aspect_ratio


def _extract_external_directed_plan(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    tags = _as_dict_loose(payload.get("tags"))
    for candidate in (
        payload.get("directed_plan"),
        tags.get("directed_plan"),
    ):
        if isinstance(candidate, dict) and candidate:
            return candidate
    return {}


def normalize_request_to_intent(payload: Dict[str, Any]) -> VideoIntent:
    req = LongformCreateRequest.model_validate(payload)
    voice_audio = _effective_voice_audio_source(payload)

    goal = None
    if req.intent and req.intent.goal:
        goal = req.intent.goal
    elif req.goal:
        goal = req.goal
    elif req.script_text:
        goal = req.script_text

    if not goal:
        goal = "Create a longform cinematic video"

    duration_sec = (
        (req.intent.duration_sec if req.intent and req.intent.duration_sec else 0)
        or _safe_int(voice_audio.get("duration_sec"), 0)
        or _duration_hint_seconds(payload)
        or _estimate_text_duration_seconds(goal)
    )

    return VideoIntent(
        mode=req.mode if req.mode == LongformMode.directed else LongformMode.legacy,
        goal=goal,
        audience=req.intent.audience if req.intent else req.audience,
        tone=list(req.intent.tone if req.intent else req.tone),
        style=list(req.intent.style if req.intent else req.style),
        scenario_type=req.intent.scenario_type if req.intent else req.scenario_type,
        duration_sec=duration_sec,
        message={
            "must_include": list(req.message.must_include),
            "must_avoid": list(req.message.must_avoid),
            "cta": req.message.cta,
        },
        assets={
            "face_artifact_id": req.assets.face_artifact_id or req.face_artifact_id,
            "voice_audio_artifact_id": req.assets.voice_audio_artifact_id,
            "voice_audio_url": _safe_str(voice_audio.get("audio_url")),
            "logo_url": req.assets.logo_url,
            "image_urls": list(req.assets.image_urls),
            "video_urls": list(req.assets.video_urls),
            "screenshot_urls": list(req.assets.screenshot_urls),
        },
        constraints={
            "external_provider_ok": req.constraints.external_provider_ok,
            "require_subtitles": req.constraints.require_subtitles,
            "max_repair_rounds": req.constraints.max_repair_rounds,
            "aspect_ratios": list(req.constraints.aspect_ratios) or [req.aspect_ratio],
        },
        meta={
            "voice_gender_mode": req.voice_gender_mode,
            "voice_gender": req.voice_gender,
            "segment_seconds": req.segment_seconds,
            "max_segment_seconds": req.max_segment_seconds,
            "tags": dict(req.tags),
            "source_mode": req.mode.value,
            "longform_profile": getattr(req, "longform_profile", "talking_video"),
            "camera_angle": getattr(req, "camera_angle", None),
            "camera_framing": getattr(req, "camera_framing", None),
            "camera_motion_style": getattr(req, "camera_motion_style", None),
            "background_mode": ("movement_based" if req.mode == LongformMode.directed else (getattr(req, "background_mode", None) or "fixed")),
            "voice_audio_url": _safe_str(voice_audio.get("audio_url")),
            "voice_audio_artifact_id": _safe_str(voice_audio.get("audio_artifact_id")) or _safe_str(req.assets.voice_audio_artifact_id),
            "voice_audio_duration_sec": _safe_int(voice_audio.get("duration_sec"), 0),
        },
    )


def choose_scenario_plan(intent: VideoIntent) -> ScenarioPlan:
    scenario_type = intent.scenario_type

    if scenario_type == ScenarioType.auto:
        text = f"{intent.goal} {' '.join(intent.tone)} {' '.join(intent.style)}".lower()
        if "founder" in text or "mission" in text or "vision" in text:
            scenario_type = ScenarioType.founder_story
        elif "explain" in text or "how it works" in text or "product" in text or "demo" in text:
            scenario_type = ScenarioType.product_explainer
        elif "campaign" in text or "launch" in text or "promo" in text:
            scenario_type = ScenarioType.campaign_promo
        elif "testimonial" in text or "customer" in text:
            scenario_type = ScenarioType.testimonial
        elif "festival" in text or "festive" in text or "celebration" in text:
            scenario_type = ScenarioType.festive_campaign
        else:
            scenario_type = ScenarioType.brand_film

    if scenario_type == ScenarioType.founder_story:
        return ScenarioPlan(
            scenario_type=scenario_type,
            rationale="Personal, mission-led arc with presenter anchors and proof beats",
            target_duration_sec=intent.duration_sec,
            talking_ratio=0.50,
            montage_ratio=0.20,
            card_ratio=0.10,
            proof_ratio=0.20,
            suggested_arc=[
                "hook",
                "personal_problem",
                "why_now",
                "product_reveal",
                "trust_proof",
                "future_vision",
                "cta",
            ],
        )

    if scenario_type == ScenarioType.product_explainer:
        return ScenarioPlan(
            scenario_type=scenario_type,
            rationale="Clear explanatory structure with product proof and direct CTA",
            target_duration_sec=intent.duration_sec,
            talking_ratio=0.30,
            montage_ratio=0.20,
            card_ratio=0.20,
            proof_ratio=0.30,
            suggested_arc=[
                "hook",
                "problem",
                "solution",
                "how_it_works",
                "proof",
                "cta",
            ],
        )

    if scenario_type in {ScenarioType.campaign_promo, ScenarioType.festive_campaign, ScenarioType.launch_announcement}:
        return ScenarioPlan(
            scenario_type=scenario_type,
            rationale="Higher-energy promotional structure with stronger montage rhythm",
            target_duration_sec=intent.duration_sec,
            talking_ratio=0.25,
            montage_ratio=0.40,
            card_ratio=0.15,
            proof_ratio=0.20,
            suggested_arc=["hook", "reveal", "proof", "offer", "cta"],
        )

    return ScenarioPlan(
        scenario_type=scenario_type,
        rationale="General cinematic brand format",
        target_duration_sec=intent.duration_sec,
        talking_ratio=0.35,
        montage_ratio=0.30,
        card_ratio=0.15,
        proof_ratio=0.20,
        suggested_arc=["hook", "story", "proof", "cta"],
    )


def build_story_beats(intent: VideoIntent, scenario: ScenarioPlan) -> List[StoryBeat]:
    beats: List[StoryBeat] = []
    arc = scenario.suggested_arc or ["hook", "story", "proof", "cta"]
    duration_per = max(5, scenario.target_duration_sec // max(1, len(arc)))

    for idx, name in enumerate(arc, start=1):
        beats.append(
            StoryBeat(
                beat_id=f"beat_{idx:02d}",
                name=name,
                purpose=_purpose_for_beat(name),
                emotion=_emotion_for_beat(name),
                duration_sec=duration_per,
                talking_priority=_talking_priority_for_beat(name),
                visual_direction=f"{scenario.scenario_type.value}::{name}",
                message_points=_message_points_for_beat(name, intent),
            )
        )
    return beats


def _purpose_for_beat(name: str) -> str:
    return {
        "hook": "Capture attention immediately",
        "problem": "Frame the pain point or market need",
        "personal_problem": "Make the story human and specific",
        "why_now": "Explain urgency and relevance",
        "solution": "Introduce the solution clearly",
        "how_it_works": "Explain the product simply",
        "product_reveal": "Reveal the core product promise",
        "proof": "Build credibility and evidence",
        "trust_proof": "Add trust or validation signals",
        "future_vision": "Leave the viewer inspired",
        "offer": "Present a compelling offer or launch framing",
        "reveal": "Create a strong reveal moment",
        "story": "Advance the emotional or brand narrative",
        "cta": "Drive the next action",
    }.get(name, "Advance the story")


def _emotion_for_beat(name: str) -> str:
    return {
        "hook": "curious",
        "personal_problem": "empathetic",
        "why_now": "urgent",
        "solution": "clear",
        "product_reveal": "excited",
        "proof": "confident",
        "trust_proof": "reassuring",
        "future_vision": "inspiring",
        "offer": "decisive",
        "cta": "decisive",
    }.get(name, "clear")


def _talking_priority_for_beat(name: str) -> float:
    if name in {"hook", "personal_problem", "product_reveal", "future_vision", "cta"}:
        return 0.8
    if name in {"proof", "trust_proof", "offer"}:
        return 0.5
    return 0.4


def _message_points_for_beat(name: str, intent: VideoIntent) -> List[str]:
    must_include = list(intent.message.must_include)
    if name == "cta" and intent.message.cta:
        return [intent.message.cta]
    if must_include:
        return must_include[:2]
    return [intent.goal]


def build_shot_specs(intent: VideoIntent, scenario: ScenarioPlan, beats: List[StoryBeat]) -> List[ShotSpec]:
    shots: List[ShotSpec] = []
    shot_index = 0

    for beat in beats:
        beat_shots = _shots_for_beat(intent, scenario, beat, shot_index)
        shots.extend(beat_shots)
        shot_index += len(beat_shots)

    return shots


def _shots_for_beat(
    intent: VideoIntent,
    scenario: ScenarioPlan,
    beat: StoryBeat,
    shot_index_start: int,
) -> List[ShotSpec]:
    specs: List[ShotSpec] = []

    if beat.name == "hook":
        specs.append(
            _make_shot(
                beat=beat,
                shot_index=shot_index_start,
                shot_type=ShotType.hook_open,
                render_route=RenderRoute.fusion,
                duration_sec=min(8, beat.duration_sec),
                spoken_text=_hook_line(intent),
                onscreen_text=[intent.goal],
                visual_brief="Confident presenter-led hook opening",
            )
        )
        specs.append(
            _make_shot(
                beat=beat,
                shot_index=shot_index_start + 1,
                shot_type=ShotType.title_card,
                render_route=RenderRoute.internal_card,
                duration_sec=3,
                spoken_text=None,
                onscreen_text=[intent.goal],
                visual_brief="Minimal premium title card",
            )
        )
        return specs

    if beat.name in {"product_reveal", "solution", "cta", "offer"}:
        primary_shot_type = ShotType.talking_head if beat.name != "solution" else ShotType.product_showcase
        primary_route = RenderRoute.fusion if primary_shot_type == ShotType.talking_head else RenderRoute.internal_montage

        specs.append(
            _make_shot(
                beat=beat,
                shot_index=shot_index_start,
                shot_type=primary_shot_type,
                render_route=primary_route,
                duration_sec=max(5, beat.duration_sec // 2),
                spoken_text=_spoken_text_for_beat(beat, intent),
                onscreen_text=list(beat.message_points),
                visual_brief=f"{beat.name} primary reveal/proposition shot",
            )
        )

        if beat.name != "cta":
            specs.append(
                _make_shot(
                    beat=beat,
                    shot_index=shot_index_start + 1,
                    shot_type=ShotType.voiceover_broll,
                    render_route=RenderRoute.audio_broll,
                    duration_sec=max(4, beat.duration_sec // 2),
                    spoken_text=_spoken_text_for_beat(beat, intent),
                    onscreen_text=list(beat.message_points),
                    visual_brief=f"{beat.name} supporting visuals / proof sequence",
                )
            )
        else:
            specs.append(
                _make_shot(
                    beat=beat,
                    shot_index=shot_index_start + 1,
                    shot_type=ShotType.outro_cta,
                    render_route=RenderRoute.internal_card,
                    duration_sec=4,
                    spoken_text=intent.message.cta or _spoken_text_for_beat(beat, intent),
                    onscreen_text=[intent.message.cta] if intent.message.cta else list(beat.message_points),
                    visual_brief="Explicit CTA end card",
                )
            )
        return specs

    specs.append(
        _make_shot(
            beat=beat,
            shot_index=shot_index_start,
            shot_type=ShotType.voiceover_broll,
            render_route=RenderRoute.audio_broll,
            duration_sec=max(5, beat.duration_sec),
            spoken_text=_spoken_text_for_beat(beat, intent),
            onscreen_text=list(beat.message_points),
            visual_brief=f"{beat.name} cinematic support sequence",
        )
    )
    return specs


def _hook_line(intent: VideoIntent) -> str:
    if intent.message.must_include:
        return intent.message.must_include[0]
    return intent.goal


def _spoken_text_for_beat(beat: StoryBeat, intent: VideoIntent) -> str:
    if beat.name == "cta" and intent.message.cta:
        return intent.message.cta
    if beat.message_points:
        return " ".join(beat.message_points)
    return beat.purpose


def _make_shot(
    *,
    beat: StoryBeat,
    shot_index: int,
    shot_type: ShotType,
    render_route: RenderRoute,
    duration_sec: int,
    spoken_text: Optional[str],
    onscreen_text: List[str],
    visual_brief: Optional[str],
) -> ShotSpec:
    return ShotSpec(
        shot_id=f"{beat.beat_id}_shot_{shot_index:02d}",
        beat_id=beat.beat_id,
        shot_index=shot_index,
        shot_type=shot_type,
        render_route=render_route,
        duration_sec=duration_sec,
        title=beat.name,
        script=ScriptSpec(
            spoken_text=spoken_text,
            voiceover_text=spoken_text,
            subtitle_text=spoken_text,
            onscreen_text=onscreen_text,
        ),
        visual_brief=visual_brief,
    )


def evaluate_directed_plan_qc(intent: VideoIntent, shots: List[ShotSpec]) -> QcResult:
    issues: List[QcIssue] = []
    score = 1.0

    if not shots:
        return QcResult(
            decision=QcDecision.fail,
            score=0.0,
            issues=[QcIssue(code="no_shots", severity="high", message="No shots planned")],
        )

    if shots[0].shot_type not in {ShotType.hook_open, ShotType.talking_head}:
        issues.append(QcIssue(code="weak_open", severity="medium", message="Opening lacks a strong hook"))
        score -= 0.20

    shot_types = {shot.shot_type.value for shot in shots}
    if len(shot_types) < 3:
        issues.append(QcIssue(code="low_variety", severity="medium", message="Shot grammar is too repetitive"))
        score -= 0.20

    if intent.message.cta and not any(shot.shot_type in {ShotType.outro_cta, ShotType.logo_sting} for shot in shots):
        issues.append(QcIssue(code="missing_cta", severity="high", message="CTA scene missing"))
        score -= 0.25

    if score >= 0.85:
        decision = QcDecision.accept
    elif any(issue.code == "missing_cta" for issue in issues):
        decision = QcDecision.insert_cta
    elif any(issue.code == "weak_open" for issue in issues):
        decision = QcDecision.insert_hook
    else:
        decision = QcDecision.rebalance_pacing

    return QcResult(
        decision=decision,
        score=max(0.0, score),
        issues=issues,
        recommended_repairs=[{"decision": decision.value, "issues": [i.code for i in issues]}],
    )


def repair_directed_shots(intent: VideoIntent, shots: List[ShotSpec], qc: QcResult) -> List[ShotSpec]:
    repaired = [shot.model_copy(deep=True) for shot in shots]

    if qc.decision == QcDecision.insert_hook:
        repaired.insert(
            0,
            ShotSpec(
                shot_id="repair_hook_00",
                beat_id="repair",
                shot_index=0,
                shot_type=ShotType.hook_open,
                render_route=RenderRoute.fusion,
                duration_sec=5,
                title="repair_hook",
                script=ScriptSpec(
                    spoken_text=intent.goal,
                    subtitle_text=intent.goal,
                    onscreen_text=[intent.goal],
                ),
                visual_brief="Repaired stronger opening shot",
            ),
        )

    if qc.decision == QcDecision.insert_cta and intent.message.cta:
        repaired.append(
            ShotSpec(
                shot_id="repair_cta_00",
                beat_id="repair",
                shot_index=len(repaired),
                shot_type=ShotType.outro_cta,
                render_route=RenderRoute.internal_card,
                duration_sec=4,
                title="repair_cta",
                script=ScriptSpec(
                    spoken_text=intent.message.cta,
                    subtitle_text=intent.message.cta,
                    onscreen_text=[intent.message.cta],
                ),
                visual_brief="Explicit CTA repair card",
            )
        )

    return repaired


def build_directed_plan(
    payload: Dict[str, Any],
) -> Tuple[VideoIntent, ScenarioPlan, List[StoryBeat], List[ShotSpec], QcResult]:
    intent = normalize_request_to_intent(payload)
    scenario = choose_scenario_plan(intent)
    beats = build_story_beats(intent, scenario)
    shots = build_shot_specs(intent, scenario, beats)
    qc = evaluate_directed_plan_qc(intent, shots)

    if qc.decision != QcDecision.accept and intent.constraints.max_repair_rounds > 0:
        shots = repair_directed_shots(intent, shots, qc)
        qc = evaluate_directed_plan_qc(intent, shots)

    return intent, scenario, beats, shots, qc


def _longform_profile_from_payload(payload: Dict[str, Any]) -> str:
    tags = _as_dict_loose(payload.get("tags"))
    profile = _safe_str(payload.get("longform_profile")) or _safe_str(tags.get("longform_profile")) or "talking_video"
    return profile if profile in {"talking_video", "cinematic_video_direction"} else "talking_video"



def _single_segment_provider_cap_seconds(payload: Dict[str, Any]) -> int:
    tags = _as_dict_loose(payload.get("tags"))
    profile = _pricing_profile(payload, tags)
    quality = _quality_tier(payload, tags)
    provider_hint = (_provider_hint(payload, tags) or "").strip().lower()
    if profile == "cinematic_video_direction":
        env_name = "OMNIHUMAN_MAX_SINGLE_SEGMENT_SECONDS"
    elif profile == "talking_video" and quality == "premium" and provider_hint == "kling":
        env_name = "FAL_KLING_AVATAR_MAX_DURATION_SEC"
    else:
        env_name = "DF_VEED_FABRIC_MAX_DURATION_SEC"
    for candidate in (
        payload.get("provider_cap_seconds"),
        tags.get("provider_cap_seconds"),
        _as_dict_loose(payload.get("provider_options")).get("segment_provider_cap_sec"),
        getattr(os, "getenv")(env_name),
    ):
        value = _safe_int(candidate, 0)
        if value > 0:
            return max(8, min(120, value))
    return 30



def _safe_segment_seconds(payload: Dict[str, Any]) -> int:
    cap = _single_segment_provider_cap_seconds(payload)
    margin = max(1, _safe_int(os.getenv("LONGFORM_PROVIDER_SEGMENT_MARGIN_SECONDS"), 2))
    configured = _safe_int(payload.get("max_segment_seconds"), 0)
    if configured <= 0:
        configured = _safe_int(_as_dict_loose(payload.get("intent")).get("max_segment_seconds"), 0)
    safe_default = max(8, cap - margin)
    if configured > 0:
        safe_default = min(safe_default, configured)
    return max(8, min(cap, safe_default))



def _talking_background_mode(payload: Dict[str, Any]) -> str:
    tags = _as_dict_loose(payload.get("tags"))
    profile = _longform_profile_from_payload(payload)
    default_mode = "movement_based" if profile == "cinematic_video_direction" else "fixed"
    mode = _safe_str(payload.get("background_mode")) or _safe_str(tags.get("background_mode")) or default_mode
    normalized = (mode or default_mode).strip().lower()
    if normalized in {"movement_based", "moving", "dynamic", "animated", "motion"}:
        return "movement_based"
    return "fixed"



def _talking_shot_type_for_background(background_mode: str) -> str:
    return "presenter_with_motion_bg" if background_mode == "movement_based" else "presenter_anchor"



def _split_talking_script(script_text: str, *, target_segment_seconds: int) -> List[Dict[str, Any]]:
    text = str(script_text or "").strip()
    if not text:
        return [{"text": "Create a strong presenter-led story beat.", "duration_sec": max(5, target_segment_seconds)}]

    target_words = max(24, int(round(float(target_segment_seconds) / 60.0 * 150.0)))
    sentence_parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    if not sentence_parts:
        sentence_parts = [text]

    chunks: List[str] = []
    current_words: List[str] = []
    current_count = 0

    for sentence in sentence_parts:
        words = sentence.split()
        if not words:
            continue
        if len(words) >= int(target_words * 1.35):
            if current_words:
                chunks.append(" ".join(current_words).strip())
                current_words = []
                current_count = 0
            for i in range(0, len(words), target_words):
                part = " ".join(words[i:i + target_words]).strip()
                if part:
                    chunks.append(part)
            continue
        if current_count and (current_count + len(words)) > target_words:
            chunks.append(" ".join(current_words).strip())
            current_words = []
            current_count = 0
        current_words.extend(words)
        current_count += len(words)

    if current_words:
        chunks.append(" ".join(current_words).strip())

    out: List[Dict[str, Any]] = []
    for chunk in chunks:
        duration_sec = min(_single_segment_provider_cap_seconds({}), max(5, _estimate_text_duration_seconds(chunk, minimum=5, default=5)))
        out.append({"text": chunk, "duration_sec": duration_sec})
    return out or [{"text": text, "duration_sec": max(5, _estimate_text_duration_seconds(text, minimum=5, default=5))}]



def build_talking_video_segment_payloads(payload: Dict[str, Any]) -> Dict[str, Any]:
    intent = normalize_request_to_intent(payload)
    profile = "talking_video"
    payload_tags = _as_dict_loose(payload.get("tags"))
    provider_options = _as_dict_loose(payload.get("provider_options"))

    quality = _quality_tier(payload, payload_tags)
    is_premium = quality == "premium"

    # Product rule:
    # - economy stays on veed_fabric
    # - premium is kling only
    provider_hint = (
        _provider_hint(payload, payload_tags)
        or _safe_str(provider_options.get("provider_hint"))
        or ("kling" if is_premium else "veed_fabric")
    )
    provider_hint = str(provider_hint or ("kling" if is_premium else "veed_fabric")).strip().lower()
    execution_provider_family = "kling_avatar" if (is_premium and provider_hint == "kling") else "veed_fabric"

    background_mode = "fixed"
    shot_type_raw = "presenter_anchor"
    render_route = RenderRoute.fusion
    voice_audio = _effective_voice_audio_source(payload)

    aspect_ratio = (intent.constraints.aspect_ratios[0] if intent.constraints.aspect_ratios else "9:16") or "9:16"
    script_text = _script_text_from_payload(payload) or intent.goal or "Tell this story clearly and naturally."
    requested_duration_sec = _requested_duration_hint_seconds(payload, payload_tags)

    if is_premium and provider_hint == "kling":
        total_duration_sec = max(
            1,
            requested_duration_sec,
            _safe_int(voice_audio.get("duration_sec"), 0),
            estimate_longform_duration_seconds(payload),
        )
    else:
        total_duration_sec = _economy_effective_duration_seconds(payload, payload_tags)

    provider_cap_sec = _single_segment_provider_cap_seconds(payload)
    segment_target_sec = min(provider_cap_sec, _safe_segment_seconds(payload))

    segment_rows: List[Dict[str, Any]]
    if total_duration_sec <= provider_cap_sec:
        segment_rows = [
            {
                "segment_index": 0,
                "status": SegmentStatus.queued.value,
                "duration_sec": max(1, total_duration_sec),
                "text_chunk": script_text,
            }
        ]
    else:
        windows: List[Tuple[int, int]] = []
        cursor = 0
        while cursor < total_duration_sec:
            end = min(total_duration_sec, cursor + segment_target_sec)
            windows.append((cursor, end))
            cursor = end
        text_chunks = _split_text_into_n_chunks(script_text, len(windows))
        segment_rows = []
        for idx, ((start_sec, end_sec), chunk_text) in enumerate(zip(windows, text_chunks)):
            segment_rows.append(
                {
                    "segment_index": idx,
                    "status": SegmentStatus.queued.value,
                    "duration_sec": max(1, end_sec - start_sec),
                    "text_chunk": _safe_str(chunk_text) or script_text,
                    "audio_start_sec": start_sec,
                    "audio_end_sec": end_sec,
                }
            )

    if _safe_int(voice_audio.get("duration_sec"), 0) > 0:
        segment_rows = _assign_audio_windows_to_rows(
            segment_rows,
            _safe_int(voice_audio.get("duration_sec"), 0),
            voice_audio,
        )

    scenario = choose_scenario_plan(intent)
    segments_by_index: Dict[str, Dict[str, Any]] = {}
    story_beats: List[Dict[str, Any]] = []
    shots: List[ShotSpec] = []

    for row in segment_rows:
        idx = int(row["segment_index"])
        chunk_text = _safe_str(row.get("text_chunk")) or script_text
        duration_sec = max(1, _safe_int(row.get("duration_sec"), segment_target_sec or provider_cap_sec))
        beat_id = f"talking_beat_{idx:02d}"
        shot_id = f"{beat_id}_shot_{idx:02d}"

        plan_item = {
            "segment_index": idx,
            "mode": intent.mode.value,
            "stage": LongformStage.shot_planning.value,
            "scenario_type": scenario.scenario_type.value,
            "beat_id": beat_id,
            "shot_id": shot_id,
            "shot_type": shot_type_raw,
            "render_route": render_route.value,
            "provider_hint": provider_hint,
            "execution_provider_family": execution_provider_family,
            "fusion_provider": provider_hint,
            "title": "presenter_segment" if len(segment_rows) == 1 else f"presenter_segment_{idx + 1}",
            "visual_brief": "Presenter-led delivery with premium realism and grounded human expression.",
            "script": {
                "spoken_text": chunk_text,
                "voiceover_text": chunk_text,
                "subtitle_text": chunk_text,
                "onscreen_text": [chunk_text[:140]],
            },
            "asset_requirements": _attach_voice_audio_metadata({
                "face_artifact_id": intent.assets.face_artifact_id,
                "voice_audio_artifact_id": intent.assets.voice_audio_artifact_id,
            }, voice_audio),
            "resolved_assets": _attach_voice_audio_metadata({
                "face_artifact_id": intent.assets.face_artifact_id,
                "voice_audio_artifact_id": intent.assets.voice_audio_artifact_id,
                "logo_url": intent.assets.logo_url,
                "image_urls": intent.assets.image_urls,
                "video_urls": intent.assets.video_urls,
                "screenshot_urls": intent.assets.screenshot_urls,
            }, voice_audio),
            "aspect_ratio": aspect_ratio,
            "duration_sec": duration_sec,
            "camera_angle": _safe_str(intent.meta.get("camera_angle")),
            "camera_framing": _safe_str(intent.meta.get("camera_framing")),
            "camera_motion_style": _safe_str(intent.meta.get("camera_motion_style")),
            "background_mode": background_mode,
            "provider_options": {
                "background_mode": background_mode,
                "composition_role": "presenter",
                "provider_hint": provider_hint,
                "fusion_provider": provider_hint,
                "presenter_provider": provider_hint,
                "avatar_mode": "audio_driven" if (is_premium and provider_hint == "kling") else None,
                "segment_provider_cap_sec": provider_cap_sec,
                "presenter_with_motion_bg": {
                    "enabled": False
                },
            },
            "audio_source_kind": row.get("audio_source_kind"),
            "audio_start_sec": _safe_int(row.get("audio_start_sec"), 0),
            "audio_end_sec": _safe_int(row.get("audio_end_sec"), duration_sec),
        }
        plan_item = _attach_voice_audio_metadata(plan_item, voice_audio)
        segments_by_index[str(idx)] = plan_item

        story_beats.append({
            "beat_id": beat_id,
            "name": "presenter_continuation" if idx else "presenter_open",
            "purpose": "Deliver the narration naturally to camera.",
            "emotion": "clear",
            "duration_sec": duration_sec,
            "talking_priority": 1.0,
            "visual_direction": shot_type_raw,
            "message_points": [chunk_text[:140]],
        })

        shots.append(
            ShotSpec(
                shot_id=shot_id,
                beat_id=beat_id,
                shot_index=idx,
                shot_type=ShotType.talking_head,
                render_route=render_route,
                duration_sec=duration_sec,
                title="presenter_segment" if len(segment_rows) == 1 else f"presenter_segment_{idx + 1}",
                script=ScriptSpec(
                    spoken_text=chunk_text,
                    voiceover_text=chunk_text,
                    subtitle_text=chunk_text,
                    onscreen_text=[chunk_text[:140]],
                ),
                visual_brief="Presenter-led delivery with premium realism and grounded human expression.",
                meta={"raw_shot_type": shot_type_raw},
            )
        )

    timeline = TimelineManifest(
        project_id="talking_video",
        aspect_ratio=aspect_ratio if aspect_ratio in {"16:9", "9:16", "1:1"} else "9:16",
        shots=shots,
        overlay_meta={
            "stitch_mode": "concat",
            "provider_cap_sec": provider_cap_sec,
            "segment_target_sec": segment_target_sec,
            "provider_hint": provider_hint,
            "execution_provider_family": execution_provider_family,
            "voice_audio_url": _safe_str(voice_audio.get("audio_url")),
            "voice_audio_artifact_id": _safe_str(voice_audio.get("audio_artifact_id")),
            "voice_audio_duration_sec": _safe_int(voice_audio.get("duration_sec"), 0),
            "segmented": len(segment_rows) > 1,
        },
    )
    qc = {"decision": QcDecision.accept.value, "score": 1.0, "issues": [], "recommended_repairs": []}

    job_tags = _attach_voice_audio_metadata({
        "mode": intent.mode.value,
        "stage": LongformStage.shot_planning.value,
        "scenario_type": scenario.scenario_type.value,
        "intent": intent.model_dump(mode="json"),
        "scenario": scenario.model_dump(mode="json"),
        "longform_profile": profile,
        "quality_tier": quality,
        "provider_hint": provider_hint,
        "execution_provider_family": execution_provider_family,
        "fusion_provider": provider_hint,
        "camera_angle": _safe_str(intent.meta.get("camera_angle")),
        "camera_framing": _safe_str(intent.meta.get("camera_framing")),
        "camera_motion_style": _safe_str(intent.meta.get("camera_motion_style")),
        "background_mode": background_mode,
        "story_beats": story_beats,
        "timeline": timeline.model_dump(mode="json"),
        "qc": qc,
        "directed_plan": {
            "segments_by_index": segments_by_index,
            "shot_count": len(segment_rows),
            "source": "talking_video_audio_driven_segmenter" if _safe_int(voice_audio.get("duration_sec"), 0) > 0 else "talking_video_auto_segmenter",
            "single_segment": len(segment_rows) == 1,
            "provider_cap_sec": provider_cap_sec,
            "segment_target_sec": segment_target_sec,
            "estimated_duration_sec": total_duration_sec,
        },
    }, voice_audio)

    return {
        "mode": intent.mode.value,
        "stage": LongformStage.shot_planning.value,
        "intent": intent.model_dump(mode="json"),
        "scenario": scenario.model_dump(mode="json"),
        "story_beats": story_beats,
        "shots": [s.model_dump(mode="json") for s in shots],
        "timeline": timeline.model_dump(mode="json"),
        "qc": qc,
        "segments_total": len(segment_rows),
        "segments": [{k: v for k, v in row.items() if k in {"segment_index", "status", "duration_sec", "text_chunk"}} for row in segment_rows],
        "job_tags": job_tags,
    }


def build_longform_execution_payloads(payload: Dict[str, Any]) -> Dict[str, Any]:
    profile = _longform_profile_from_payload(payload)
    external_plan = _extract_external_directed_plan(payload)
    source_mode = _safe_str(payload.get("mode"))
    if external_plan or profile == "cinematic_video_direction":
        planned = build_directed_segment_payloads(payload)
        job_tags = _as_dict_loose(planned.get("job_tags"))
        if profile == "cinematic_video_direction":
            job_tags["background_mode"] = job_tags.get("background_mode") or "movement_based"
            job_tags["longform_profile"] = "cinematic_video_direction"
            planned["job_tags"] = job_tags
        return planned
    return build_talking_video_segment_payloads(payload)



def build_directed_segment_payloads(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Current storage contract:
    - longform_jobs.tags holds planner / timeline metadata
    - longform_segments stores only concrete runtime rows
    - worker looks up segment metadata via job.tags.directed_plan.segments_by_index

    Supports both:
    1) built-in planner flow
    2) externally supplied directed plans from the OmniHuman E2E / future planner layers
    """
    intent = normalize_request_to_intent(payload)
    scenario = choose_scenario_plan(intent)
    external_plan = _extract_external_directed_plan(payload)
    voice_audio = _effective_voice_audio_source(payload)

    if external_plan:
        story_beats = _as_list_loose(payload.get("story_beats")) or _as_list_loose(_as_dict_loose(payload.get("tags")).get("story_beats"))
        qc = _as_dict_loose(payload.get("qc")) or _as_dict_loose(_as_dict_loose(payload.get("tags")).get("qc")) or {
            "decision": QcDecision.accept.value,
            "score": 1.0,
            "issues": [],
            "recommended_repairs": [],
        }

        segment_rows, raw_segments_by_index, normalized_shots, normalized_timeline, aspect_ratio = _normalize_external_segment_rows_from_plan(external_plan)

        if _safe_int(voice_audio.get("duration_sec"), 0) > 0 and segment_rows:
            segment_rows = _assign_audio_windows_to_rows(segment_rows, _safe_int(voice_audio.get("duration_sec"), 0), voice_audio)
            for row in segment_rows:
                idx = int(row["segment_index"])
                raw = dict(raw_segments_by_index.get(str(idx)) or {})
                raw.update({
                    "duration_sec": int(row["duration_sec"]),
                    "audio_start_sec": _safe_int(row.get("audio_start_sec"), 0),
                    "audio_end_sec": _safe_int(row.get("audio_end_sec"), int(row["duration_sec"])),
                    "audio_source_kind": row.get("audio_source_kind"),
                })
                raw["resolved_assets"] = _attach_voice_audio_metadata(_as_dict_loose(raw.get("resolved_assets")), voice_audio)
                raw["asset_requirements"] = _attach_voice_audio_metadata(_as_dict_loose(raw.get("asset_requirements")), voice_audio)
                raw_segments_by_index[str(idx)] = _attach_voice_audio_metadata(raw, voice_audio)
            normalized_shots = [shot.model_copy(update={"duration_sec": int(segment_rows[i]["duration_sec"])}) if i < len(segment_rows) else shot for i, shot in enumerate(normalized_shots)]
            normalized_timeline = normalized_timeline.model_copy(update={"shots": normalized_shots, "overlay_meta": {**_as_dict_loose(normalized_timeline.overlay_meta), "stitch_mode": "xfade", "voice_audio_url": _safe_str(voice_audio.get("audio_url")), "voice_audio_artifact_id": _safe_str(voice_audio.get("audio_artifact_id")), "voice_audio_duration_sec": _safe_int(voice_audio.get("duration_sec"), 0)}})

        directed_tags = _attach_voice_audio_metadata({
            "mode": intent.mode.value,
            "stage": LongformStage.shot_planning.value,
            "scenario_type": scenario.scenario_type.value,
            "intent": intent.model_dump(mode="json"),
            "scenario": scenario.model_dump(mode="json"),
            "longform_profile": _safe_str(intent.meta.get("longform_profile")) or "talking_video",
            "camera_angle": _safe_str(intent.meta.get("camera_angle")),
            "camera_framing": _safe_str(intent.meta.get("camera_framing")),
            "camera_motion_style": _safe_str(intent.meta.get("camera_motion_style")),
            "background_mode": "movement_based",
            "story_beats": story_beats,
            "timeline": {**normalized_timeline.model_dump(mode="json"), "overlay_meta": {**_as_dict_loose(normalized_timeline.overlay_meta), "stitch_mode": "xfade"}},
            "qc": qc,
            "directed_plan": {
                "segments_by_index": raw_segments_by_index,
                "shot_count": len(segment_rows),
                "source": "external_directed_plan",
            },
        }, voice_audio)

        return {
            "mode": intent.mode.value,
            "stage": LongformStage.shot_planning.value,
            "intent": intent.model_dump(mode="json"),
            "scenario": scenario.model_dump(mode="json"),
            "story_beats": story_beats,
            "shots": [s.model_dump(mode="json") for s in normalized_shots],
            "timeline": {**normalized_timeline.model_dump(mode="json"), "overlay_meta": {**_as_dict_loose(normalized_timeline.overlay_meta), "stitch_mode": "xfade"}},
            "qc": qc,
            "segments_total": len(segment_rows),
            "segments": segment_rows,
            "job_tags": directed_tags,
        }

    # Built-in planner fallback
    beats = build_story_beats(intent, scenario)
    shots = build_shot_specs(intent, scenario, beats)
    qc = evaluate_directed_plan_qc(intent, shots)

    if qc.decision != QcDecision.accept and intent.constraints.max_repair_rounds > 0:
        shots = repair_directed_shots(intent, shots, qc)
        qc = evaluate_directed_plan_qc(intent, shots)

    segment_rows: List[Dict[str, Any]] = []
    segments_by_index: Dict[str, Dict[str, Any]] = {}

    aspect_ratio = intent.constraints.aspect_ratios[0] if intent.constraints.aspect_ratios else "9:16"

    for shot in shots:
        spoken = shot.script.spoken_text or shot.script.voiceover_text
        text_chunk = spoken or " ".join(shot.script.onscreen_text) or (shot.title or shot.shot_type.value)

        plan_item = {
            "segment_index": shot.shot_index,
            "mode": intent.mode.value,
            "stage": LongformStage.shot_planning.value,
            "scenario_type": scenario.scenario_type.value,
            "beat_id": shot.beat_id,
            "shot_id": shot.shot_id,
            "shot_type": shot.shot_type.value,
            "render_route": shot.render_route.value,
            "title": shot.title,
            "visual_brief": shot.visual_brief,
            "script": shot.script.model_dump(mode="json"),
            "asset_requirements": _attach_voice_audio_metadata(shot.asset_requirements, voice_audio),
            "resolved_assets": _attach_voice_audio_metadata({
                "face_artifact_id": intent.assets.face_artifact_id,
                "voice_audio_artifact_id": intent.assets.voice_audio_artifact_id,
                "logo_url": intent.assets.logo_url,
                "image_urls": intent.assets.image_urls,
                "video_urls": intent.assets.video_urls,
                "screenshot_urls": intent.assets.screenshot_urls,
            }, voice_audio),
            "aspect_ratio": aspect_ratio,
            "duration_sec": shot.duration_sec,
            "camera_angle": _safe_str(intent.meta.get("camera_angle")),
            "camera_framing": _safe_str(intent.meta.get("camera_framing")),
            "camera_motion_style": _safe_str(intent.meta.get("camera_motion_style")),
            "background_mode": "movement_based",
        }
        segments_by_index[str(shot.shot_index)] = plan_item

        segment_rows.append(
            {
                "segment_index": shot.shot_index,
                "status": SegmentStatus.queued.value,
                "duration_sec": shot.duration_sec,
                "text_chunk": text_chunk,
            }
        )

    if _safe_int(voice_audio.get("duration_sec"), 0) > 0 and segment_rows:
        segment_rows = _assign_audio_windows_to_rows(segment_rows, _safe_int(voice_audio.get("duration_sec"), 0), voice_audio)
        for shot in shots:
            idx = int(shot.shot_index)
            if idx < len(segment_rows):
                shot.duration_sec = int(segment_rows[idx]["duration_sec"])
                plan_item = dict(segments_by_index.get(str(idx)) or {})
                plan_item.update({
                    "duration_sec": int(segment_rows[idx]["duration_sec"]),
                    "audio_start_sec": _safe_int(segment_rows[idx].get("audio_start_sec"), 0),
                    "audio_end_sec": _safe_int(segment_rows[idx].get("audio_end_sec"), int(segment_rows[idx]["duration_sec"])),
                    "audio_source_kind": segment_rows[idx].get("audio_source_kind"),
                })
                plan_item["resolved_assets"] = _attach_voice_audio_metadata(_as_dict_loose(plan_item.get("resolved_assets")), voice_audio)
                plan_item["asset_requirements"] = _attach_voice_audio_metadata(_as_dict_loose(plan_item.get("asset_requirements")), voice_audio)
                segments_by_index[str(idx)] = _attach_voice_audio_metadata(plan_item, voice_audio)

    timeline = TimelineManifest(
        project_id="pending",
        aspect_ratio=aspect_ratio,
        shots=shots,
        overlay_meta={
            "stitch_mode": "xfade",
            "voice_audio_url": _safe_str(voice_audio.get("audio_url")),
            "voice_audio_artifact_id": _safe_str(voice_audio.get("audio_artifact_id")),
            "voice_audio_duration_sec": _safe_int(voice_audio.get("duration_sec"), 0),
        },
    )

    directed_tags = _attach_voice_audio_metadata({
        "mode": intent.mode.value,
        "stage": LongformStage.shot_planning.value,
        "scenario_type": scenario.scenario_type.value,
        "intent": intent.model_dump(mode="json"),
        "scenario": scenario.model_dump(mode="json"),
        "longform_profile": _safe_str(intent.meta.get("longform_profile")) or "talking_video",
        "camera_angle": _safe_str(intent.meta.get("camera_angle")),
        "camera_framing": _safe_str(intent.meta.get("camera_framing")),
        "camera_motion_style": _safe_str(intent.meta.get("camera_motion_style")),
        "story_beats": [b.model_dump(mode="json") for b in beats],
        "timeline": timeline.model_dump(mode="json"),
        "qc": qc.model_dump(mode="json"),
        "directed_plan": {
            "segments_by_index": segments_by_index,
            "shot_count": len(shots),
        },
    }, voice_audio)

    return {
        "mode": intent.mode.value,
        "stage": LongformStage.shot_planning.value,
        "intent": intent.model_dump(mode="json"),
        "scenario": scenario.model_dump(mode="json"),
        "story_beats": [b.model_dump(mode="json") for b in beats],
        "shots": [s.model_dump(mode="json") for s in shots],
        "timeline": timeline.model_dump(mode="json"),
        "qc": qc.model_dump(mode="json"),
        "segments_total": len(segment_rows),
        "segments": segment_rows,
        "job_tags": directed_tags,
    }


async def stitch_if_ready(
    jobs: LongformJobsRepo,
    segs: LongformSegmentsRepo,
    conn,
    job_row: Dict[str, Any],
) -> None:
    job_id = str(job_row["id"])
    job_status = _safe_str(job_row.get("status")) or LongformJobStatus.queued.value

    if job_status not in {LongformJobStatus.running.value, LongformJobStatus.stitching.value}:
        return

    if await segs.any_failed(conn, job_id):
        await release_longform_pricing_for_job(conn, job_id=job_id, user_id=_safe_str(job_row.get("user_id")), reason="segment_failed", tags=_job_tags_dict(job_row))
        await jobs.set_status(conn, job_id, LongformJobStatus.failed.value, error_message="One or more segments failed")
        await _emit_notification_best_effort(
            {
                "event_type": "FUSION_FAILED",
                "category": "jobs",
                "priority": "important",
                "source_service": "svc-fusion-extension",
                "source_ref_type": "job",
                "source_ref_id": str(job_id),
                "actor_user_id": None,
                "title": "Your Fusion job needs attention",
                "body": "One or more longform segments failed.",
                "action_route": "/notifications",
                "action_label": "Review issue",
                "image_url": None,
                "payload_json": {"job_id": str(job_id), "error_code": "SEGMENT_FAILED"},
                "metadata_json": {"job_id": str(job_id), "error_code": "SEGMENT_FAILED"},
                "dedupe_key": f"fusion-failed:{job_id}:segment_failed",
                "recipients": [{"user_id": str(_safe_str(job_row.get("user_id")) or ""), "channels": {"in_app": True, "push": True, "email": True}}],
            },
            context={"job_id": str(job_id), "user_id": str(_safe_str(job_row.get("user_id")) or ""), "event_type": "FUSION_FAILED", "reason": "segment_failed"},
        )
        return

    done = await segs.count_done(conn, job_id)
    total = int(job_row.get("total_segments") or 0)
    await jobs.set_counts(conn, job_id, total, done)

    if total <= 0 or done != total:
        return

    await jobs.set_status(conn, job_id, LongformJobStatus.stitching.value)

    rows = await segs.list_by_job(conn, job_id)
    rows = sorted(rows, key=lambda r: int(r.get("segment_index") or 0))
    video_urls = [_safe_str(r.get("segment_video_url")) for r in rows]

    if any(not u for u in video_urls):
        await release_longform_pricing_for_job(conn, job_id=job_id, user_id=_safe_str(job_row.get("user_id")), reason="missing_segment_video_url", tags=_job_tags_dict(job_row))
        await jobs.set_status(conn, job_id, LongformJobStatus.failed.value, error_message="Missing segment video_url")
        await _emit_notification_best_effort(
            {
                "event_type": "FUSION_FAILED",
                "category": "jobs",
                "priority": "important",
                "source_service": "svc-fusion-extension",
                "source_ref_type": "job",
                "source_ref_id": str(job_id),
                "actor_user_id": None,
                "title": "Your Fusion job needs attention",
                "body": "A required segment video was missing during finalization.",
                "action_route": "/notifications",
                "action_label": "Review issue",
                "image_url": None,
                "payload_json": {"job_id": str(job_id), "error_code": "MISSING_SEGMENT_VIDEO_URL"},
                "metadata_json": {"job_id": str(job_id), "error_code": "MISSING_SEGMENT_VIDEO_URL"},
                "dedupe_key": f"fusion-failed:{job_id}:missing_segment_video_url",
                "recipients": [{"user_id": str(_safe_str(job_row.get("user_id")) or ""), "channels": {"in_app": True, "push": True, "email": True}}],
            },
            context={"job_id": str(job_id), "user_id": str(_safe_str(job_row.get("user_id")) or ""), "event_type": "FUSION_FAILED", "reason": "missing_segment_video_url"},
        )
        return

    aspect_ratio = _safe_str(job_row.get("aspect_ratio")) or "9:16"
    tags = _as_dict_loose(job_row.get("tags"))
    overlay_meta = _as_dict_loose(_as_dict_loose(tags.get("timeline")).get("overlay_meta")) if tags else {}
    if (_safe_str(tags.get("longform_profile")) or "talking_video") == "talking_video":
        overlay_meta.setdefault("stitch_mode", "concat")
    else:
        overlay_meta.setdefault("stitch_mode", "xfade")

    try:
        with tempfile.TemporaryDirectory() as td:
            local_files: List[str] = []

            async with httpx.AsyncClient(timeout=300) as client:
                for i, url in enumerate(video_urls):
                    outp = os.path.join(td, f"seg_{i:04d}.mp4")
                    rr = await client.get(url)
                    rr.raise_for_status()
                    with open(outp, "wb") as f:
                        f.write(rr.content)
                    local_files.append(outp)

            if not local_files:
                await release_longform_pricing_for_job(conn, job_id=job_id, user_id=_safe_str(job_row.get("user_id")), reason="no_segment_files", tags=_job_tags_dict(job_row))
                await jobs.set_status(conn, job_id, LongformJobStatus.failed.value, error_message="No segment files to stitch")
                await _emit_notification_best_effort(
                    {
                        "event_type": "FUSION_FAILED",
                        "category": "jobs",
                        "priority": "important",
                        "source_service": "svc-fusion-extension",
                        "source_ref_type": "job",
                        "source_ref_id": str(job_id),
                        "actor_user_id": None,
                        "title": "Your Fusion job needs attention",
                        "body": "No segment files were available for final stitching.",
                        "action_route": "/notifications",
                        "action_label": "Review issue",
                        "image_url": None,
                        "payload_json": {"job_id": str(job_id), "error_code": "NO_SEGMENT_FILES"},
                        "metadata_json": {"job_id": str(job_id), "error_code": "NO_SEGMENT_FILES"},
                        "dedupe_key": f"fusion-failed:{job_id}:no_segment_files",
                        "recipients": [{"user_id": str(_safe_str(job_row.get("user_id")) or ""), "channels": {"in_app": True, "push": True, "email": True}}],
                    },
                    context={"job_id": str(job_id), "user_id": str(_safe_str(job_row.get("user_id")) or ""), "event_type": "FUSION_FAILED", "reason": "no_segment_files"},
                )
                return

            final_local = os.path.join(td, "final.mp4")
            compose_result = compose_timeline(
                local_files,
                final_local,
                job_id=job_id,
                aspect_ratio=aspect_ratio,
                overlay_meta=overlay_meta or {},
            )
            storage_path, signed_url = upload_final_mp4(final_local)
            await jobs.set_final(conn, job_id, storage_path, signed_url)
            refreshed_job_row = await jobs.get_job(conn, job_id)
            effective_job_row = dict(refreshed_job_row) if refreshed_job_row else dict(job_row)
            refreshed_tags = _job_tags_dict(effective_job_row)
            refreshed_pricing = _extract_pricing_view(refreshed_tags)
            pricing_commit_required = bool(refreshed_pricing and refreshed_pricing.get("enabled"))
            committed = await commit_longform_pricing_for_job(conn, job_row=effective_job_row, final_duration_sec=(compose_result or {}).get("duration_sec"))
            committed_state = (_safe_str(_as_dict_loose(committed).get("state")) or '').lower()
            if pricing_commit_required and committed_state != 'committed':
                raise RuntimeError(f"LONGFORM_PRICING_COMMIT_NOT_FINALIZED state={committed_state or '<empty>'}")
            await jobs.set_status(conn, job_id, LongformJobStatus.succeeded.value)
            await _emit_notification_best_effort(
                {
                    "event_type": "FUSION_READY",
                    "category": "jobs",
                    "priority": "important",
                    "source_service": "svc-fusion-extension",
                    "source_ref_type": "job",
                    "source_ref_id": str(job_id),
                    "actor_user_id": None,
                    "title": "Your Fusion video is ready",
                    "body": "Your desifaces.ai Fusion video completed successfully.",
                    "action_route": "/notifications",
                    "action_label": "View video",
                    "image_url": None,
                    "payload_json": {"job_id": str(job_id), "video_url": signed_url},
                    "metadata_json": {"job_id": str(job_id), "video_url": signed_url},
                    "dedupe_key": f"fusion-ready:{job_id}",
                    "recipients": [{"user_id": str(_safe_str(job_row.get("user_id")) or ""), "channels": {"in_app": True, "push": True, "email": True}}],
                },
                context={"job_id": str(job_id), "user_id": str(_safe_str(job_row.get("user_id")) or ""), "event_type": "FUSION_READY"},
            )
    except Exception as exc:
        pricing_logger.exception("longform finalization failed job_id=%s", job_id)
        await jobs.set_status(
            conn,
            job_id,
            LongformJobStatus.failed.value,
            error_message=f"Finalization failed: {exc}",
        )
        await _emit_notification_best_effort(
            {
                "event_type": "FUSION_FAILED",
                "category": "jobs",
                "priority": "important",
                "source_service": "svc-fusion-extension",
                "source_ref_type": "job",
                "source_ref_id": str(job_id),
                "actor_user_id": None,
                "title": "Your Fusion job needs attention",
                "body": f"Finalization failed: {exc}",
                "action_route": "/notifications",
                "action_label": "Review issue",
                "image_url": None,
                "payload_json": {"job_id": str(job_id), "error_code": "FINALIZATION_FAILED"},
                "metadata_json": {"job_id": str(job_id), "error_code": "FINALIZATION_FAILED"},
                "dedupe_key": f"fusion-failed:{job_id}:finalization_failed",
                "recipients": [{"user_id": str(_safe_str(job_row.get("user_id")) or ""), "channels": {"in_app": True, "push": True, "email": True}}],
            },
            context={"job_id": str(job_id), "user_id": str(_safe_str(job_row.get("user_id")) or ""), "event_type": "FUSION_FAILED", "reason": "finalization_failed"},
        )
        raise
