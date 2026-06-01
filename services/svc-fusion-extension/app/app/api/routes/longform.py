
from __future__ import annotations

import json
import logging
import math
import os
from typing import Any, Dict, Optional

import asyncpg
import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.deps import (
    get_current_token,
    get_current_user_id,
    get_db_pool_dep as get_db_pool,
)
from app.config import settings
from app.domain.enums import LongformMode
from app.domain.models import (
    LongformCreateRequest,
    LongformJobCreated,
    LongformJobView,
    LongformSegmentView,
)
from app.repos.longform_jobs_repo import LongformJobsRepo
from app.repos.longform_segments_repo import LongformSegmentsRepo
from app.services.longform_orchestrator import (
    PricingClientError,
    build_longform_execution_payloads,
    preview_longform_pricing,
    reserve_longform_pricing_for_job,
)
from app.services.sas_service import AzureBlobService

router = APIRouter(prefix="/api/longform", tags=["longform"])

jobs_repo = LongformJobsRepo()
segs_repo = LongformSegmentsRepo()
logger = logging.getLogger("svc_fusion_extension.longform_route")


class LongformPricingPreviewResponse(BaseModel):
    pricing: Dict[str, Any] = Field(default_factory=dict)
    pricing_summary: Dict[str, Any] = Field(default_factory=dict)
    before_credits: Optional[str] = None
    after_estimated_credits: Optional[str] = None
    estimated_amount: Optional[str] = None
    currency: Optional[str] = None
    quote_breakdown: Dict[str, Any] = Field(default_factory=dict)
    summary: Dict[str, Any] = Field(default_factory=dict)
    insufficient_balance: bool = False
    message: Optional[str] = None


def _clamp_fusion_duration(sec: int) -> int:
    # svc-fusion hard limit is 120 today
    return max(1, min(120, int(sec)))


def _normalize_bearer(token_or_header: Optional[str]) -> Optional[str]:
    t = (token_or_header or "").strip()
    if not t:
        return None
    if t.lower().startswith("bearer "):
        return t
    return f"Bearer {t}"


def _service_bearer_for_workers() -> Optional[str]:
    """
    Product-grade rule:
    - NEVER persist short-lived user JWTs for async workers.
    - Prefer service bearer for worker execution.
    """
    tok = getattr(settings, "SVC_TO_SVC_BEARER", None)
    tok = tok or os.getenv("SVC_TO_SVC_BEARER") or os.getenv("SVC_FUSION_EXTENSION_BEARER")
    return _normalize_bearer(tok)


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


def _safe_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _safe_positive_int(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        parsed = int(float(str(value).strip()))
        return parsed if parsed > 0 else 0
    except Exception:
        return 0


def _country_from_payload(source: Dict[str, Any]) -> Optional[str]:
    if not isinstance(source, dict):
        return None

    tags = _as_dict_loose(source.get("tags"))
    context = _as_dict_loose(source.get("context"))
    client_context = _as_dict_loose(source.get("client_context"))
    pricing_context = _as_dict_loose(source.get("pricing_context"))
    provider_options = _as_dict_loose(source.get("provider_options"))

    for value in (
        source.get("country_code"),
        source.get("country"),
        source.get("billing_country_code"),
        source.get("pricing_country_code"),
        context.get("country_code"),
        context.get("country"),
        client_context.get("country_code"),
        client_context.get("country"),
        pricing_context.get("country_code"),
        pricing_context.get("country"),
        provider_options.get("country_code"),
        tags.get("country_code"),
        tags.get("billing_country_code"),
        tags.get("pricing_country_code"),
    ):
        s = (_safe_str(value) or "").upper()
        if s:
            return s[:2]
    return None


def _resolve_country_code(raw_body: Dict[str, Any], request: Optional[Request]) -> str:
    """
    Resolve billing country deterministically from the API boundary.

    Important: svc-pricing does not infer currency from login/IP. The caller must send
    X-Country-Code or country_code. Missing country falls back to US/USD.
    """
    header_cc = ""
    if request is not None:
        header_cc = (
            request.headers.get("X-Country-Code")
            or request.headers.get("x-country-code")
            or request.headers.get("CF-IPCountry")
            or request.headers.get("cf-ipcountry")
            or ""
        )
    cc = (_safe_str(header_cc) or _country_from_payload(raw_body) or "").upper()
    if not cc or cc == "XX":
        return "US"
    return cc[:2]


def _extract_requested_duration_sec(source: Dict[str, Any]) -> int:
    if not isinstance(source, dict):
        return 0

    tags = _as_dict_loose(source.get("tags"))
    intent = _as_dict_loose(source.get("intent"))
    video = _as_dict_loose(source.get("video"))
    provider_options = _as_dict_loose(source.get("provider_options"))
    pricing_context = _as_dict_loose(source.get("pricing_context"))

    for value in (
        source.get("requested_duration_sec"),
        source.get("duration_sec"),
        source.get("total_duration_sec"),
        source.get("target_duration_sec"),
        source.get("estimated_duration_sec"),
        video.get("requested_duration_sec"),
        video.get("duration_sec"),
        video.get("total_duration_sec"),
        provider_options.get("requested_duration_sec"),
        provider_options.get("duration_sec"),
        provider_options.get("total_duration_sec"),
        intent.get("requested_duration_sec"),
        intent.get("duration_sec"),
        pricing_context.get("requested_duration_sec"),
        pricing_context.get("duration_sec"),
        tags.get("requested_duration_sec"),
        tags.get("duration_sec"),
        tags.get("total_duration_sec"),
        tags.get("estimated_duration_sec"),
        source.get("segment_seconds"),
        tags.get("segment_seconds"),
    ):
        parsed = _safe_positive_int(value)
        if parsed > 0:
            return parsed
    return 0


def _talking_duration_bucket_sec(duration_sec: int) -> int:
    sec = _safe_positive_int(duration_sec)
    if sec <= 10:
        return 10
    if sec <= 20:
        return 20
    return 30


def _apply_request_pricing_context(
    body: Dict[str, Any],
    *,
    country_code: Optional[str],
    requested_duration_sec: int,
) -> Dict[str, Any]:
    """
    Stamp country and requested duration into every area downstream code may inspect:
    top-level payload, tags, provider_options, video, and pricing_context.

    This protects against Pydantic extra-field drops and keeps preview/reserve/status
    rendering on the same country and duration bucket.
    """
    if not isinstance(body, dict):
        return body

    tags = _as_dict_loose(body.get("tags"))
    provider_options = _as_dict_loose(body.get("provider_options"))
    video = _as_dict_loose(body.get("video"))
    pricing_context = _as_dict_loose(body.get("pricing_context"))

    cc = (_safe_str(country_code) or _country_from_payload(body) or "US").upper()[:2]
    body["country_code"] = cc
    tags["country_code"] = cc
    tags["billing_country_code"] = cc
    provider_options["country_code"] = cc
    pricing_context["country_code"] = cc

    duration = _safe_positive_int(requested_duration_sec) or _extract_requested_duration_sec(body)
    if duration > 0:
        body["requested_duration_sec"] = duration
        body["duration_sec"] = duration
        body["total_duration_sec"] = duration

        tags["requested_duration_sec"] = duration
        tags["duration_sec"] = duration
        tags["total_duration_sec"] = duration
        tags["duration_bucket_sec"] = _talking_duration_bucket_sec(duration)

        provider_options["requested_duration_sec"] = duration
        provider_options["duration_sec"] = duration
        provider_options["total_duration_sec"] = duration

        video["requested_duration_sec"] = duration
        video["duration_sec"] = duration

        pricing_context["requested_duration_sec"] = duration
        pricing_context["duration_sec"] = duration
        pricing_context["duration_bucket_sec"] = _talking_duration_bucket_sec(duration)

        # For talking-video jobs, segment_seconds is the execution duration.
        # Without this, the LongformCreateRequest model/default can silently fall back to 30s.
        profile = _resolve_longform_profile_from_payload(body, tags)
        if profile == "talking_video":
            body["segment_seconds"] = duration
            existing_max = _safe_positive_int(body.get("max_segment_seconds"))
            body["max_segment_seconds"] = max(existing_max, duration, 30)

    body["tags"] = tags
    body["provider_options"] = provider_options
    body["video"] = video
    body["pricing_context"] = pricing_context
    return body


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


def _resolve_longform_profile_from_payload(body: Dict[str, Any], tags: Optional[Dict[str, Any]] = None) -> str:
    tags = tags or {}
    intent = _as_dict_loose(body.get('intent'))

    scenario_name = (
        _safe_str(body.get('scenario_name'))
        or _safe_str(tags.get('scenario_name'))
        or _safe_str(tags.get('fusion_studio_mode'))
        or ''
    ).lower()
    if scenario_name in {'talking_video_economy', 'talking_video_premium'}:
        return 'talking_video'
    if scenario_name.startswith('cinematic'):
        return 'cinematic_video_direction'

    for value in (
        body.get('longform_profile'),
        tags.get('longform_profile'),
        tags.get('requested_longform_profile'),
        intent.get('longform_profile'),
        body.get('mode'),
        tags.get('mode'),
        tags.get('api_mode'),
        body.get('output_profile'),
        tags.get('output_profile'),
    ):
        normalized = _normalize_longform_profile_value(value)
        if normalized:
            return normalized
    return 'talking_video'


def _normalize_longform_request_body(raw: Any) -> Dict[str, Any]:
    body = _as_dict_loose(raw)
    tags = _as_dict_loose(body.get("tags"))
    resolved_profile = _resolve_longform_profile_from_payload(body, tags)
    body['longform_profile'] = resolved_profile
    tags['longform_profile'] = resolved_profile

    requested_mode = (_safe_str(body.get("mode")) or "").lower()
    if requested_mode in {'talking_video', 'legacy'}:
        body['mode'] = LongformMode.legacy.value
    elif requested_mode in {'cinematic_video_direction', 'directed'}:
        body['mode'] = LongformMode.directed.value
    else:
        body['mode'] = LongformMode.directed.value if resolved_profile == 'cinematic_video_direction' else LongformMode.legacy.value

    requested_output_profile = (_safe_str(body.get('output_profile')) or '').lower()
    if requested_output_profile not in {'talking_video', 'economy', 'fast', 'premium'}:
        body['output_profile'] = 'premium' if resolved_profile == 'cinematic_video_direction' else 'talking_video'
        requested_output_profile = str(body['output_profile']).lower()
    elif requested_output_profile:
        body['output_profile'] = requested_output_profile

    scenario_name = (_safe_str(body.get('scenario_name')) or '').lower()
    requested_quality_tier = (
        _safe_str(body.get('quality_tier'))
        or _safe_str(tags.get('quality_tier'))
        or _safe_str(_as_dict_loose(body.get('intent')).get('quality_tier'))
        or ''
    ).lower()
    provider_hint = (_safe_str(body.get('provider_hint')) or _safe_str(tags.get('provider_hint')) or '').lower()

    if resolved_profile == 'talking_video':
        is_talking_economy = (
            requested_output_profile == 'economy'
            or scenario_name == 'talking_video_economy'
            or requested_quality_tier in {'economy', 'eco', 'fast', 'budget'}
        )
        is_talking_premium = not is_talking_economy

        body['quality_tier'] = 'economy' if is_talking_economy else 'premium'
        tags['quality_tier'] = body['quality_tier']

        provider_options = _as_dict_loose(body.get('provider_options'))

        if is_talking_economy:
            body['provider_hint'] = 'veed_fabric'
            tags['provider_hint'] = 'veed_fabric'
            tags['execution_provider_family'] = 'veed_fabric'
            if not _safe_str(body.get('background_mode')):
                body['background_mode'] = 'fixed'
            provider_options.setdefault('provider_hint', 'veed_fabric')
            provider_options.setdefault('fusion_provider', 'veed_fabric')
            provider_options.setdefault('presenter_provider', 'veed_fabric')
            provider_options.setdefault('quality_tier', 'economy')
            provider_options.setdefault('output_profile', body.get('output_profile') or 'economy')
            provider_options.setdefault('background_mode', 'fixed')
            provider_options.setdefault('segment_provider_cap_sec', 30)
            provider_options.pop('background_provider', None)
            provider_options.pop('background_profile', None)
            body['provider_options'] = provider_options
        else:
            body['provider_hint'] = 'kling'
            tags['provider_hint'] = 'kling'
            tags['execution_provider_family'] = 'kling_avatar'
            if not _safe_str(body.get('background_mode')):
                body['background_mode'] = 'fixed'
            if not _safe_str(body.get('camera_motion_style')):
                body['camera_motion_style'] = 'gentle_push_in'

            provider_options['provider_hint'] = 'kling'
            provider_options['fusion_provider'] = 'kling'
            provider_options['presenter_provider'] = 'kling'
            provider_options['quality_tier'] = 'premium'
            provider_options['output_profile'] = body.get('output_profile') or 'premium'
            provider_options['avatar_mode'] = 'audio_driven'
            provider_options['direct_avatar_enabled'] = True
            provider_options['background_mode'] = 'fixed'
            provider_options['segment_provider_cap_sec'] = 30
            provider_options.setdefault('resolution', '720p')
            provider_options.pop('background_provider', None)
            provider_options.pop('background_profile', None)
            provider_options.pop('presenter_with_motion_bg', None)
            body['provider_options'] = provider_options

            tags['requested_longform_profile'] = 'talking_video'
            tags['requested_quality_tier'] = 'premium'
            tags['direct_avatar_provider'] = 'kling'
            tags['talking_video_execution_mode'] = 'kling_avatar_segments'
            tags['camera_motion_style'] = body.get('camera_motion_style')
    elif requested_quality_tier:
        body['quality_tier'] = requested_quality_tier
        tags.setdefault('quality_tier', requested_quality_tier)

    if not _safe_str(body.get('background_mode')):
        body['background_mode'] = 'movement_based' if resolved_profile == 'cinematic_video_direction' else 'fixed'
    if resolved_profile == 'talking_video':
        tags.setdefault('selected_mode', 'talking_video_economy' if body.get('quality_tier') == 'economy' else 'talking_video_premium')
    tags.setdefault('background_mode', body.get('background_mode'))
    tags.setdefault('output_profile', body.get('output_profile'))
    tags.setdefault('api_mode', body.get('mode'))

    if body.get("mode") == LongformMode.legacy.value:
        script_text = (
            _safe_str(body.get("script_text"))
            or _safe_str(body.get("script"))
            or _safe_str(body.get("goal"))
            or _safe_str(_as_dict_loose(body.get("intent")).get("goal"))
            or _safe_str(tags.get("script_text"))
            or _safe_str(tags.get("goal"))
        )
        if script_text:
            body["script_text"] = script_text

    body["tags"] = tags

    # Preserve explicit requested duration before LongformCreateRequest validation.
    # This is critical for pricing buckets: 10s must not become the model/default 30s.
    requested_duration_sec = _extract_requested_duration_sec(body)
    if requested_duration_sec > 0:
        _apply_request_pricing_context(
            body,
            country_code=_country_from_payload(body),
            requested_duration_sec=requested_duration_sec,
        )

    return body

def _pick_url(*values: Any) -> Optional[str]:
    for value in values:
        s = _safe_str(value)
        if s and s.startswith(("http://", "https://")):
            return s
    return None


def _settings_or_env(*names: str) -> str:
    for name in names:
        if hasattr(settings, name):
            value = getattr(settings, name)
            if value and str(value).strip():
                return str(value).strip()
        value = os.getenv(name, "")
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _deep_first_url(obj: Any, preferred_keys: tuple[str, ...]) -> Optional[str]:
    if isinstance(obj, dict):
        for key in preferred_keys:
            value = _pick_url(obj.get(key))
            if value:
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


def _deep_first_int(obj: Any, preferred_keys: tuple[str, ...]) -> int:
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


def _resolver_auth_token(request_token: Optional[str]) -> Optional[str]:
    return _service_bearer_for_workers() or _normalize_bearer(request_token)


def _artifact_resolver_candidates(kind: str, artifact_id: str) -> list[str]:
    artifact_id = str(artifact_id or '').strip()
    if not artifact_id:
        return []
    if artifact_id.startswith(("http://", "https://")):
        return [artifact_id]

    urls: list[str] = []
    template_names = [
        f'DF_{kind.upper()}_ARTIFACT_RESOLVE_URL_TEMPLATE',
        f'{kind.upper()}_ARTIFACT_RESOLVE_URL_TEMPLATE',
        f'SVC_{kind.upper()}_ARTIFACT_RESOLVE_URL_TEMPLATE',
    ]
    for name in template_names:
        template = _settings_or_env(name)
        if template and '{artifact_id}' in template:
            urls.append(template.replace('{artifact_id}', artifact_id))

    if kind == 'audio':
        base = _settings_or_env('SVC_AUDIO_URL', 'DF_AUDIO_URL', 'AUDIO_URL')
        paths = [
            '/api/audio/artifacts/{artifact_id}',
            '/api/audio/assets/{artifact_id}',
            '/api/audio/media/{artifact_id}',
            '/api/audio/artifact/{artifact_id}',
        ]
    else:
        base = _settings_or_env('SVC_FACE_URL', 'DF_FACE_URL', 'FACE_URL')
        paths = [
            '/api/face/artifacts/{artifact_id}',
            '/api/face/assets/{artifact_id}',
            '/api/face/media/{artifact_id}',
            '/api/face/artifact/{artifact_id}',
        ]
    if base:
        base = base.rstrip('/')
        for path in paths:
            urls.append(base + path.replace('{artifact_id}', artifact_id))

    deduped: list[str] = []
    seen = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def _artifact_headers(token_or_header: Optional[str], user_id: str) -> Dict[str, str]:
    headers = {'Accept': 'application/json'}
    tok = _normalize_bearer(token_or_header)
    if tok:
        headers['Authorization'] = tok
    if user_id:
        headers['X-User-Id'] = str(user_id)
    return headers


def _extract_audio_resolution_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'voice_audio_url': _deep_first_url(data, ('audio_url', 'voice_audio_url', 'signed_url', 'sas_url', 'download_url', 'url')),
        'voice_audio_duration_sec': _deep_first_int(data, ('duration_sec', 'audio_duration_sec', 'track_duration_sec', 'voice_audio_duration_sec', 'duration_ms')),
    }


def _extract_face_resolution_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'face_image_url': _deep_first_url(data, ('face_image_url', 'image_url', 'signed_url', 'sas_url', 'preview_url', 'url')),
    }


async def _resolve_artifact_json(kind: str, artifact_id: str, *, token_or_header: Optional[str], user_id: str) -> Dict[str, Any]:
    urls = _artifact_resolver_candidates(kind, artifact_id)
    if not urls:
        return {}
    headers = _artifact_headers(token_or_header, user_id)
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = await client.get(url, headers=headers)
            except Exception:
                logger.exception('artifact_resolver_request_failed kind=%s url=%s', kind, url)
                continue
            if resp.status_code in {401, 403, 404, 405}:
                continue
            if resp.status_code >= 400:
                logger.warning('artifact_resolver_http_error kind=%s url=%s status=%s', kind, url, resp.status_code)
                continue
            try:
                data = resp.json() if resp.content else {}
            except Exception:
                data = {}
            if isinstance(data, dict) and data:
                return data
    return {}


async def _resolve_audio_planning_hints(payload: Dict[str, Any], *, user_id: str, request_token: Optional[str]) -> Dict[str, Any]:
    hints = _extract_audio_planning_hints(payload)
    if hints.get('voice_audio_url') and int(hints.get('voice_audio_duration_sec') or 0) > 0:
        return hints
    artifact_id = _safe_str(hints.get('voice_audio_artifact_id'))
    if not artifact_id:
        return hints
    resolved = await _resolve_artifact_json('audio', artifact_id, token_or_header=_resolver_auth_token(request_token), user_id=user_id)
    if resolved:
        extracted = _extract_audio_resolution_payload(resolved)
        if extracted.get('voice_audio_url') and not hints.get('voice_audio_url'):
            hints['voice_audio_url'] = extracted['voice_audio_url']
        if int(extracted.get('voice_audio_duration_sec') or 0) > 0 and int(hints.get('voice_audio_duration_sec') or 0) <= 0:
            hints['voice_audio_duration_sec'] = int(extracted['voice_audio_duration_sec'])
    return hints


async def _planning_payload_from_request(req: LongformCreateRequest, *, user_id: str, request_token: Optional[str]) -> Dict[str, Any]:
    payload = req.model_dump(by_alias=False, mode='json', exclude_none=True)
    tags = _as_dict_loose(payload.get('tags'))
    hints = await _resolve_audio_planning_hints(payload, user_id=user_id, request_token=request_token)
    if hints.get('voice_audio_url'):
        payload['voice_audio_url'] = hints['voice_audio_url']
    if hints.get('voice_audio_artifact_id'):
        payload['voice_audio_artifact_id'] = hints['voice_audio_artifact_id']
    if int(hints.get('voice_audio_duration_sec') or 0) > 0:
        payload['voice_audio_duration_sec'] = int(hints['voice_audio_duration_sec'])

    if hints.get('voice_audio_url') or hints.get('voice_audio_artifact_id') or int(hints.get('voice_audio_duration_sec') or 0) > 0:
        tags['resolved_audio'] = {
            'audio_url': hints.get('voice_audio_url'),
            'audio_artifact_id': hints.get('voice_audio_artifact_id'),
            'audio_duration_sec': int(hints.get('voice_audio_duration_sec') or 0),
        }
        payload['tags'] = tags
    return payload


def _extract_audio_planning_hints(source: Dict[str, Any]) -> Dict[str, Any]:
    tags = _as_dict_loose(source.get("tags")) if isinstance(source, dict) else {}

    def _pick_url(*values: Any) -> Optional[str]:
        for value in values:
            s = _safe_str(value)
            if s and s.startswith(("http://", "https://")):
                return s
        return None

    voice_audio = _as_dict_loose(tags.get("voice_audio"))
    selected_audio = _as_dict_loose(tags.get("selected_audio"))
    audio_result = _as_dict_loose(tags.get("audio_result"))
    resolved_audio = _as_dict_loose(tags.get("resolved_audio"))

    audio_url = _pick_url(
        source.get("voice_audio_url"),
        source.get("audio_url"),
        voice_audio.get("audio_url"),
        selected_audio.get("audio_url"),
        audio_result.get("audio_url"),
        resolved_audio.get("audio_url"),
        tags.get("voice_audio_url"),
        tags.get("audio_url"),
        voice_audio.get("signed_url"),
        selected_audio.get("signed_url"),
        audio_result.get("signed_url"),
        resolved_audio.get("signed_url"),
        voice_audio.get("url"),
        selected_audio.get("url"),
        audio_result.get("url"),
        resolved_audio.get("url"),
    )

    audio_artifact_id = None
    for value in (
        source.get("voice_audio_artifact_id"),
        source.get("audio_artifact_id"),
        voice_audio.get("voice_audio_artifact_id"),
        voice_audio.get("audio_artifact_id"),
        selected_audio.get("voice_audio_artifact_id"),
        selected_audio.get("audio_artifact_id"),
        audio_result.get("voice_audio_artifact_id"),
        audio_result.get("audio_artifact_id"),
        resolved_audio.get("voice_audio_artifact_id"),
        resolved_audio.get("audio_artifact_id"),
        tags.get("voice_audio_artifact_id"),
        tags.get("audio_artifact_id"),
    ):
        s = _safe_str(value)
        if s:
            audio_artifact_id = s
            break

    duration_sec = 0
    for value in (
        source.get("voice_audio_duration_sec"),
        source.get("audio_duration_sec"),
        source.get("track_duration_sec"),
        voice_audio.get("voice_audio_duration_sec"),
        voice_audio.get("audio_duration_sec"),
        selected_audio.get("voice_audio_duration_sec"),
        selected_audio.get("audio_duration_sec"),
        audio_result.get("voice_audio_duration_sec"),
        audio_result.get("audio_duration_sec"),
        resolved_audio.get("voice_audio_duration_sec"),
        resolved_audio.get("audio_duration_sec"),
        tags.get("voice_audio_duration_sec"),
        tags.get("audio_duration_sec"),
        tags.get("track_duration_sec"),
    ):
        try:
            if value is not None and int(float(value)) > 0:
                duration_sec = int(float(value))
                break
        except Exception:
            pass

    return {
        "voice_audio_url": audio_url,
        "voice_audio_artifact_id": audio_artifact_id,
        "voice_audio_duration_sec": duration_sec,
    }




def _longform_profile_from_source(source: Dict[str, Any]) -> str:
    tags = _as_dict_loose(source.get("tags")) if isinstance(source, dict) else {}
    return _resolve_longform_profile_from_payload(source if isinstance(source, dict) else {}, tags)


def _variant_and_leaf_for_profile(profile: str, source: Optional[Dict[str, Any]] = None) -> tuple[str, str]:
    normalized = str(profile or "").strip().lower()
    source = source or {}
    tags = _as_dict_loose(source.get("tags")) if isinstance(source, dict) else {}
    quality_tier = (
        _safe_str(source.get("quality_tier"))
        or _safe_str(tags.get("quality_tier"))
        or "premium"
    )
    quality_tier = str(quality_tier).strip().lower()

    if normalized == "cinematic_video_direction":
        return "CINEMATIC_VIDEO_DIRECTION", "LONGFORM_CINEMATIC_MIN"

    # Talking video is priced by duration bucket. Never hardcode 30S here,
    # because this also drives API/status fallback views when quote_json is partial.
    requested_duration_sec = _extract_requested_duration_sec(source)
    bucket_sec = _talking_duration_bucket_sec(requested_duration_sec)

    if normalized == "talking_video":
        if quality_tier == "economy":
            return (
                f"TALKING_VIDEO_ECONOMY_{bucket_sec}S",
                f"LONGFORM_TALK_ECONOMY_{bucket_sec}S",
            )
        return (
            f"TALKING_VIDEO_PREMIUM_{bucket_sec}S",
            f"LONGFORM_TALK_PREMIUM_{bucket_sec}S",
        )

    return (
        f"TALKING_VIDEO_PREMIUM_{bucket_sec}S",
        f"LONGFORM_TALK_PREMIUM_{bucket_sec}S",
    )



def _safe_int_string(value: Any) -> Optional[str]:
    try:
        if value is None or value == "":
            return None
        return str(int(float(value)))
    except Exception:
        return _safe_str(value)

def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None

def _derive_preview_insufficient_balance(pricing: Dict[str, Any]) -> bool:
    p = _as_dict_loose(pricing)
    if bool(p.get("insufficient_balance") or p.get("insufficientBalance")):
        return True
    before = _safe_float(p.get("before_credits"))
    after = _safe_float(p.get("after_estimated_credits"))
    if before is not None and after is not None and after < 0:
        return True
    msg = (_safe_str(p.get("message")) or "").lower()
    return "insufficient" in msg or "not enough credit" in msg

def _normalize_pricing_view(pricing: Dict[str, Any], source: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    p = _as_dict_loose(pricing)
    if not p:
        return {}

    profile = _longform_profile_from_source(source or {})
    default_variant, default_leaf = _variant_and_leaf_for_profile(profile, source or {})

    state = _safe_str(p.get("state"))
    variant_code = _safe_str(p.get("variant_code")) or _safe_str(p.get("sku_code")) or default_variant
    sku_code = _safe_str(p.get("leaf_sku_code")) or _safe_str(p.get("sku_code")) or default_leaf
    final_amount = p.get("final_amount")
    if final_amount in (None, "") and (state or "").lower() == "committed":
        final_amount = p.get("amount") or p.get("estimated_amount")

    quote_breakdown = _as_dict_loose(p.get("quote_breakdown"))
    summary = _as_dict_loose(p.get("summary"))
    before_credits = _safe_int_string(
        p.get("before_credits") if p.get("before_credits") not in (None, "") else quote_breakdown.get("before_credits")
    )
    after_estimated_credits = _safe_int_string(
        p.get("after_estimated_credits") if p.get("after_estimated_credits") not in (None, "") else quote_breakdown.get("after_estimated_credits")
    )
    insufficient_balance = _derive_preview_insufficient_balance(
        {
            **p,
            "before_credits": before_credits,
            "after_estimated_credits": after_estimated_credits,
            "quote_breakdown": quote_breakdown,
        }
    )

    actual_units = _safe_str(p.get("actual_units"))
    billed_units = _safe_str(p.get("billed_units"))
    estimated_units = _safe_str(p.get("estimated_units"))
    reserved_units = _safe_str(p.get("reserved_units"))
    if (state or "").lower() == "committed":
        if not actual_units:
            actual_units = billed_units or reserved_units or estimated_units
        if not billed_units:
            billed_units = actual_units or reserved_units or estimated_units

    settlement_mode = _safe_str(p.get("settlement_mode"))
    if not settlement_mode and (state or "").lower() == "committed":
        if billed_units or actual_units or estimated_units:
            settlement_mode = "credits"

    reservation_status = _safe_str(p.get("reservation_status"))
    commit_status = _safe_str(p.get("commit_status"))
    if (state or "").lower() == "committed":
        reservation_status = reservation_status or "committed"
        commit_status = commit_status or "committed"

    return {
        **p,
        "enabled": bool(p.get("enabled", False)),
        "state": state,
        "billing_mode": _safe_str(p.get("billing_mode")),
        "settlement_mode": settlement_mode,
        "pricing_mode": _safe_str(p.get("pricing_mode")),
        "tier_code": _safe_str(p.get("tier_code")),
        "quote_id": _safe_str(p.get("quote_id")),
        "reservation_id": _safe_str(p.get("reservation_id")),
        "reservation_status": reservation_status,
        "commit_status": commit_status,
        "variant_code": variant_code,
        "sku_code": sku_code,
        "leaf_sku_code": _safe_str(p.get("leaf_sku_code")) or sku_code,
        "estimated_units": estimated_units,
        "reserved_units": reserved_units,
        "actual_units": actual_units,
        "billed_units": billed_units,
        "released_units": _safe_str(p.get("released_units")),
        "amount": _safe_str(p.get("amount")),
        "estimated_amount": _safe_str(p.get("estimated_amount")) or _safe_str(p.get("amount")),
        "final_amount": _safe_str(final_amount),
        "currency": _safe_str(p.get("currency")),
        "ledger_entry_id": _safe_str(p.get("ledger_entry_id")) or _safe_str(p.get("ledger_id")) or _safe_str(p.get("ledger_event_id")),
        "billing_account_id": _safe_str(p.get("billing_account_id")),
        "service_name": _safe_str(p.get("service_name")),
        "service_action": _safe_str(p.get("service_action")),
        "before_credits": before_credits,
        "after_estimated_credits": after_estimated_credits,
        "quote_breakdown": quote_breakdown,
        "summary": summary,
        "message": _safe_str(p.get("message")),
        "insufficient_balance": insufficient_balance,
    }

def _normalize_pricing_summary_view(pricing: Dict[str, Any], summary: Dict[str, Any]) -> Dict[str, Any]:
    s = _as_dict_loose(summary)
    p = _normalize_pricing_view(pricing)
    if not p:
        return s

    out = dict(s or {})
    for key in (
        "state",
        "estimated_units",
        "reserved_units",
        "actual_units",
        "billed_units",
        "released_units",
        "amount",
        "estimated_amount",
        "final_amount",
        "currency",
        "billing_mode",
        "settlement_mode",
        "pricing_mode",
        "reservation_id",
        "reservation_status",
        "commit_status",
        "ledger_entry_id",
        "billing_account_id",
        "variant_code",
        "sku_code",
        "leaf_sku_code",
        "service_name",
        "service_action",
    ):
        value = p.get(key)
        if value not in (None, "", {}, []):
            out[key] = value

    currency = _safe_str(p.get("currency"))
    def _fmt(v: Any) -> Optional[str]:
        sv = _safe_str(v)
        if not sv:
            return None
        return f"{currency} {sv}" if currency else sv

    estimate = p.get("amount") or p.get("estimated_amount")
    final = p.get("final_amount") or estimate

    if "display_estimate" not in out or not _safe_str(out.get("display_estimate")):
        out["display_estimate"] = _fmt(estimate)
    if "display_final" not in out or not _safe_str(out.get("display_final")):
        out["display_final"] = _fmt(final)
    if (p.get("state") or "").lower() == "committed":
        out.setdefault("display_delta", None)
        out["display_note"] = out.get("display_note") or "Final charge recorded after execution."
    return out


def _resolve_face_artifact_id(req: LongformCreateRequest) -> Optional[str]:
    return _safe_str(req.face_artifact_id) or _safe_str(req.assets.face_artifact_id)


def _resolve_job_script_text(req: LongformCreateRequest, planning_payload: Optional[Dict[str, Any]] = None) -> str:
    for value in (
        req.script_text,
        req.goal,
        getattr(req.intent, "goal", None),
    ):
        s = _safe_str(value)
        if s:
            return s
    planning_payload = planning_payload or {}
    if _safe_str(planning_payload.get("voice_audio_url")) or _safe_str(planning_payload.get("voice_audio_artifact_id")) or _safe_str(req.assets.voice_audio_artifact_id):
        return "Narration-driven longform video"
    raise HTTPException(status_code=400, detail="missing_script_or_goal")


def _normalize_voice_policy(req: LongformCreateRequest) -> tuple[Optional[str], Optional[str]]:
    voice_gender_mode = getattr(req, "voice_gender_mode", None)
    voice_gender = getattr(req, "voice_gender", None)
    if voice_gender_mode is not None:
        voice_gender_mode = str(voice_gender_mode).strip().lower() or None
    if voice_gender is not None:
        voice_gender = str(voice_gender).strip().lower() or None
    return voice_gender_mode, voice_gender


def _extract_pricing_view(job_or_tags: Dict[str, Any]) -> Dict[str, Any]:
    source = _as_dict_loose(job_or_tags.get("tags")) if "tags" in job_or_tags else _as_dict_loose(job_or_tags)
    pricing = _as_dict_loose(source.get("pricing"))
    return _normalize_pricing_view(pricing, source)


def _extract_pricing_summary_view(job_or_tags: Dict[str, Any]) -> Dict[str, Any]:
    source = _as_dict_loose(job_or_tags.get("tags")) if "tags" in job_or_tags else _as_dict_loose(job_or_tags)
    pricing = _as_dict_loose(source.get("pricing"))
    summary = _as_dict_loose(source.get("pricing_summary"))
    return _normalize_pricing_summary_view(pricing, summary)


def _merge_pricing_non_empty(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Merge pricing views without letting partial DB quote rows blank committed receipt fields."""
    out = dict(base or {})
    for key, value in dict(overlay or {}).items():
        if value not in (None, "", {}, []):
            out[key] = value
    return out


async def _lookup_latest_ledger_entry_id(
    conn: asyncpg.Connection,
    *,
    reservation_id: Optional[str],
    job_id: Optional[str],
) -> Optional[str]:
    """Best-effort lookup of the ledger row for receipt display."""
    try:
        exists = await conn.fetchval("select to_regclass('public.pricing_credit_ledger_events')::text")
        if not exists:
            return None

        rows = await conn.fetch(
            """
            select column_name
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'pricing_credit_ledger_events'
            """
        )
        cols = {str(r["column_name"]) for r in rows}
        if "id" not in cols:
            return None

        conditions: list[str] = []
        args: list[Any] = []

        def add_arg(value: str) -> str:
            args.append(str(value))
            return f"${len(args)}"

        rid = _safe_str(reservation_id)
        jid = _safe_str(job_id)

        if rid and "reservation_id" in cols:
            conditions.append(f"reservation_id::text = {add_arg(rid)}")
        if jid:
            for c in ("external_ref_id", "job_ref", "service_job_id", "source_ref_id", "ref_id", "job_id"):
                if c in cols:
                    conditions.append(f"{c}::text = {add_arg(jid)}")
        if rid and "metadata_json" in cols:
            conditions.append(f"metadata_json->>'reservation_id' = {add_arg(rid)}")
        if jid and "metadata_json" in cols:
            conditions.append(f"(metadata_json->>'job_id' = {add_arg(jid)} or metadata_json->>'longform_job_id' = {add_arg(jid)} or metadata_json->>'service_job_id' = {add_arg(jid)})")
        if rid and "meta_json" in cols:
            conditions.append(f"meta_json->>'reservation_id' = {add_arg(rid)}")
        if jid and "meta_json" in cols:
            conditions.append(f"(meta_json->>'job_id' = {add_arg(jid)} or meta_json->>'longform_job_id' = {add_arg(jid)} or meta_json->>'service_job_id' = {add_arg(jid)})")

        if not conditions:
            return None

        order_col = "created_at" if "created_at" in cols else "id"
        sql = f"""
            select id::text
            from public.pricing_credit_ledger_events
            where {" or ".join(conditions)}
            order by {order_col} desc
            limit 1
        """
        return _safe_str(await conn.fetchval(sql, *args))
    except Exception:
        logger.exception(
            "longform.ledger_lookup_failed",
            extra={"reservation_id": reservation_id, "job_id": job_id},
        )
        return None


def _pricing_receipt_summary(pricing: Dict[str, Any], summary: Dict[str, Any]) -> Dict[str, Any]:
    return _normalize_pricing_summary_view(pricing, summary)


def _first_nonempty(*values: Any) -> Optional[str]:
    for value in values:
        s = _safe_str(value)
        if s:
            return s
    return None


def _build_run_receipt_view(
    pricing: Dict[str, Any],
    pricing_summary: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Build a stable, UI-facing run receipt from canonical pricing fields.

    The mobile app may read either snake_case or camelCase keys. Keep both here so
    old and new client builds can render the same completed receipt.
    """
    p = _as_dict_loose(pricing)
    s = _as_dict_loose(pricing_summary)
    if not p and not s:
        return None

    state = _first_nonempty(p.get("state"), s.get("state"))
    estimated_units = _first_nonempty(
        p.get("estimated_units"),
        s.get("estimated_units"),
        p.get("reserved_units"),
        s.get("reserved_units"),
    )
    reserved_units = _first_nonempty(
        p.get("reserved_units"),
        s.get("reserved_units"),
        estimated_units,
    )
    actual_units = _first_nonempty(
        p.get("actual_units"),
        s.get("actual_units"),
        p.get("billed_units"),
        s.get("billed_units"),
    )
    billed_units = _first_nonempty(
        p.get("billed_units"),
        s.get("billed_units"),
        actual_units,
    )
    settlement_mode = _first_nonempty(p.get("settlement_mode"), s.get("settlement_mode"))
    billing_mode = _first_nonempty(p.get("billing_mode"), s.get("billing_mode"))
    reservation_id = _first_nonempty(p.get("reservation_id"), s.get("reservation_id"))
    reservation_status = _first_nonempty(p.get("reservation_status"), s.get("reservation_status"))
    commit_status = _first_nonempty(p.get("commit_status"), s.get("commit_status"))
    ledger_entry_id = _first_nonempty(p.get("ledger_entry_id"), s.get("ledger_entry_id"))

    amount = _first_nonempty(p.get("amount"), s.get("amount"))
    estimated_amount = _first_nonempty(
        p.get("estimated_amount"),
        s.get("estimated_amount"),
        amount,
    )
    final_amount = _first_nonempty(
        p.get("final_amount"),
        s.get("final_amount"),
        amount,
        estimated_amount,
    )
    currency = _first_nonempty(p.get("currency"), s.get("currency"))
    variant_code = _first_nonempty(p.get("variant_code"), s.get("variant_code"))
    sku_code = _first_nonempty(p.get("sku_code"), s.get("sku_code"))
    leaf_sku_code = _first_nonempty(p.get("leaf_sku_code"), s.get("leaf_sku_code"))
    service_name = _first_nonempty(p.get("service_name"), s.get("service_name"))
    service_action = _first_nonempty(p.get("service_action"), s.get("service_action"))

    display_estimate = _first_nonempty(s.get("display_estimate"))
    display_final = _first_nonempty(s.get("display_final"))
    display_delta = _first_nonempty(s.get("display_delta"))
    display_note = _first_nonempty(s.get("display_note")) or (
        "Final charge recorded after execution." if state == "committed" else None
    )

    receipt = {
        # Canonical snake_case for API/debug consistency.
        "state": state,
        "status": state,
        "estimated_units": estimated_units,
        "reserved_units": reserved_units,
        "actual_units": actual_units,
        "billed_units": billed_units,
        "settlement_mode": settlement_mode,
        "billing_mode": billing_mode,
        "reservation_id": reservation_id,
        "reservation_status": reservation_status,
        "commit_status": commit_status,
        "ledger_entry_id": ledger_entry_id,
        "estimated_amount": estimated_amount,
        "final_amount": final_amount,
        "amount": final_amount or amount,
        "currency": currency,
        "display_estimate": display_estimate,
        "display_final": display_final,
        "display_delta": display_delta,
        "display_note": display_note,
        "variant_code": variant_code,
        "sku_code": sku_code,
        "leaf_sku_code": leaf_sku_code,
        "service_name": service_name,
        "service_action": service_action,

        # Backward/forward compatible camelCase aliases for mobile clients.
        "estimatedUnits": estimated_units,
        "reservedUnits": reserved_units,
        "actualUnits": actual_units,
        "billedUnits": billed_units,
        "settlementMode": settlement_mode,
        "billingMode": billing_mode,
        "reservationId": reservation_id,
        "reservationStatus": reservation_status,
        "commitStatus": commit_status,
        "ledgerEntryId": ledger_entry_id,
        "estimatedAmount": estimated_amount,
        "finalAmount": final_amount,
        "displayEstimate": display_estimate,
        "displayFinal": display_final,
        "displayDelta": display_delta,
        "displayNote": display_note,
        "variantCode": variant_code,
        "skuCode": sku_code,
        "leafSkuCode": leaf_sku_code,
        "serviceName": service_name,
        "serviceAction": service_action,
    }

    cleaned = {k: v for k, v in receipt.items() if v not in (None, "", {}, [])}
    return cleaned or None


async def _load_latest_pricing_view(conn: asyncpg.Connection, job_id: str, source: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    source_pricing = _extract_pricing_view(source)
    source_summary = _extract_pricing_summary_view(source)

    row = await conn.fetchrow(
        """
        select *
        from pricing_credit_reservations
        where job_ref = $1
        order by created_at desc
        limit 1
        """,
        str(job_id),
    )
    if not row:
        pricing = _normalize_pricing_view(source_pricing, source)
        return pricing, _pricing_receipt_summary(pricing, source_summary)

    quote = _as_dict_loose(row.get('quote_json'))
    profile = _longform_profile_from_source(source)
    default_variant, default_leaf = _variant_and_leaf_for_profile(profile, source)

    state = (
        _safe_str(row.get('status'))
        or _safe_str(quote.get('status'))
        or _safe_str(quote.get('state'))
        or _safe_str(source_pricing.get('state'))
        or 'reserved'
    ).lower()

    row_final_credits = row.get('final_charged_credits') if 'final_charged_credits' in row else None
    row_reserved_credits = row.get('reserved_credits') if 'reserved_credits' in row else None
    row_final_money = row.get('final_charged_money') if 'final_charged_money' in row else None

    amount = (
        _safe_str(quote.get('estimated_amount'))
        or _safe_str(quote.get('amount'))
        or (_safe_str(row.get('estimated_money')) if 'estimated_money' in row and row.get('estimated_money') is not None else None)
        or source_pricing.get('amount')
        or source_pricing.get('estimated_amount')
    )
    final_amount = (
        _safe_str(quote.get('final_charged_money'))
        or _safe_str(quote.get('final_amount'))
        or (_safe_str(row_final_money) if row_final_money is not None else None)
        or source_pricing.get('final_amount')
    )
    if not final_amount and state == 'committed':
        final_amount = amount

    estimated_units = (
        _safe_str(quote.get('estimated_units'))
        or _safe_str(quote.get('requested_units'))
        or _safe_str(quote.get('units'))
        or _safe_str(source_pricing.get('estimated_units'))
        or (_safe_str(row_reserved_credits) if row_reserved_credits is not None else None)
    )
    actual_units = (
        _safe_str(quote.get('actual_units'))
        or _safe_str(quote.get('final_charged_credits'))
        or (_safe_str(row_final_credits) if row_final_credits is not None else None)
        or _safe_str(source_pricing.get('actual_units'))
    )
    billed_units = (
        _safe_str(quote.get('billed_units'))
        or _safe_str(quote.get('final_charged_credits'))
        or (_safe_str(row_final_credits) if row_final_credits is not None else None)
        or _safe_str(source_pricing.get('billed_units'))
        or actual_units
    )
    if state == 'committed':
        actual_units = actual_units or billed_units or estimated_units
        billed_units = billed_units or actual_units or estimated_units

    settlement_mode = (
        (_safe_str(row.get('settlement_mode')) if 'settlement_mode' in row else None)
        or _safe_str(quote.get('settlement_mode'))
        or _safe_str(source_pricing.get('settlement_mode'))
    )
    if not settlement_mode and state == 'committed':
        if _safe_str(billed_units):
            settlement_mode = 'credits'
        elif final_amount:
            settlement_mode = 'money'

    reservation_id = str(row.get('id'))
    ledger_entry_id = (
        _safe_str(quote.get('ledger_entry_id'))
        or _safe_str(quote.get('ledger_id'))
        or _safe_str(quote.get('ledger_event_id'))
        or _safe_str(source_pricing.get('ledger_entry_id'))
    )
    if not ledger_entry_id and state == 'committed':
        ledger_entry_id = await _lookup_latest_ledger_entry_id(conn, reservation_id=reservation_id, job_id=str(job_id))

    db_pricing = _normalize_pricing_view({
        'enabled': True,
        'state': state,
        'billing_mode': _safe_str(quote.get('billing_mode_snapshot')) or _safe_str(quote.get('billing_mode')) or source_pricing.get('billing_mode'),
        'settlement_mode': settlement_mode,
        'pricing_mode': _safe_str(quote.get('pricing_mode')) or source_pricing.get('pricing_mode'),
        'tier_code': _safe_str(quote.get('tier_code')) or source_pricing.get('tier_code'),
        'quote_id': _safe_str(quote.get('quote_id')) or source_pricing.get('quote_id'),
        'preview_fingerprint': _safe_str(quote.get('preview_fingerprint')) or source_pricing.get('preview_fingerprint'),
        'reservation_id': reservation_id,
        'reservation_status': _safe_str(quote.get('reservation_status')) or state,
        'commit_status': _safe_str(quote.get('commit_status')) or ('committed' if state == 'committed' else None),
        'variant_code': _safe_str(quote.get('variant_code')) or source_pricing.get('variant_code') or default_variant,
        'sku_code': _safe_str(quote.get('leaf_sku_code')) or _safe_str(quote.get('sku_code')) or (_safe_str(row.get('sku_code')) if 'sku_code' in row else None) or source_pricing.get('sku_code') or default_leaf,
        'leaf_sku_code': _safe_str(quote.get('leaf_sku_code')) or _safe_str(quote.get('sku_code')) or (_safe_str(row.get('sku_code')) if 'sku_code' in row else None) or source_pricing.get('leaf_sku_code') or default_leaf,
        'estimated_units': estimated_units,
        'reserved_units': _safe_str(quote.get('reserved_units')) or estimated_units,
        'actual_units': actual_units,
        'billed_units': billed_units,
        'amount': amount,
        'estimated_amount': _safe_str(quote.get('estimated_amount')) or amount,
        'final_amount': final_amount,
        'currency': (_safe_str(row.get('currency')) if 'currency' in row else None) or _safe_str(quote.get('currency')) or source_pricing.get('currency') or 'USD',
        'ledger_entry_id': ledger_entry_id,
        'billing_account_id': (str(row.get('billing_account_id')) if 'billing_account_id' in row and row.get('billing_account_id') else None) or _safe_str(quote.get('billing_account_id')) or source_pricing.get('billing_account_id'),
        'service_name': (_safe_str(row.get('service_name')) if 'service_name' in row else None) or _safe_str(quote.get('service_name')) or source_pricing.get('service_name') or 'svc-fusion-extension',
        'service_action': (_safe_str(row.get('service_action')) if 'service_action' in row else None) or _safe_str(quote.get('service_action')) or source_pricing.get('service_action'),
    }, source)

    pricing = _merge_pricing_non_empty(source_pricing, db_pricing)
    if source_pricing.get('state') == 'committed' or db_pricing.get('state') == 'committed':
        pricing['state'] = 'committed'
        pricing['reservation_status'] = pricing.get('reservation_status') or 'committed'
        pricing['commit_status'] = pricing.get('commit_status') or 'committed'
        pricing['actual_units'] = pricing.get('actual_units') or pricing.get('billed_units') or pricing.get('estimated_units')
        pricing['billed_units'] = pricing.get('billed_units') or pricing.get('actual_units') or pricing.get('estimated_units')
        pricing['settlement_mode'] = pricing.get('settlement_mode') or ('credits' if pricing.get('billed_units') else None)
        pricing['final_amount'] = pricing.get('final_amount') or pricing.get('amount')

    pricing = _normalize_pricing_view(pricing, source)
    db_summary = _normalize_pricing_summary_view(db_pricing, _as_dict_loose(quote.get('summary')))
    summary = _pricing_receipt_summary(pricing, _merge_pricing_non_empty(source_summary, db_summary))

    logger.info(
        "longform.status_pricing_receipt job_id=%s state=%s estimated_units=%s billed_units=%s settlement=%s reservation_id=%s ledger_entry_id=%s",
        job_id,
        pricing.get("state"),
        pricing.get("estimated_units"),
        pricing.get("billed_units"),
        pricing.get("settlement_mode"),
        pricing.get("reservation_id"),
        pricing.get("ledger_entry_id"),
    )
    return pricing, summary


def _pricing_error_code(exc: Exception) -> str:
    msg = str(exc or "")
    if "PRICING_CLIENT_DISABLED" in msg or "pricing client unavailable" in msg.lower():
        return "PRICING_CLIENT_DISABLED"
    if "PRICING_UNKNOWN_OR_INACTIVE_VARIANT" in msg:
        return "PRICING_UNKNOWN_OR_INACTIVE_VARIANT"
    if "PRICING_VARIANT_ZERO_QTY_LINES" in msg:
        return "PRICING_VARIANT_ZERO_QTY_LINES"
    if "PRICING_VARIANT_HAS_NO_LINES" in msg:
        return "PRICING_VARIANT_HAS_NO_LINES"
    if "PRICING_INSUFFICIENT_CREDITS" in msg:
        return "PRICING_INSUFFICIENT_CREDITS"
    if "ENTITLEMENT_BLOCKED_FEATURE_FLAG" in msg:
        return "ENTITLEMENT_BLOCKED_FEATURE_FLAG"
    return "PRICING_RESERVATION_FAILED"


def _raise_http_for_pricing_error(exc: Exception) -> None:
    code = _pricing_error_code(exc)
    if code == "PRICING_CLIENT_DISABLED":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=code)
    if code == "PRICING_INSUFFICIENT_CREDITS":
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=code)
    if code == "ENTITLEMENT_BLOCKED_FEATURE_FLAG":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=code)
    raise HTTPException(status_code=status.HTTP_424_FAILED_DEPENDENCY, detail=code)


def _enrich_segment_view_from_job_tags(segment_row: asyncpg.Record, job_tags: Dict[str, Any]) -> Dict[str, Any]:
    directed = _as_dict_loose(job_tags.get("directed_plan"))
    seg_map = _as_dict_loose(directed.get("segments_by_index"))
    seg_meta = _as_dict_loose(seg_map.get(str(int(segment_row["segment_index"]))))
    script_meta = _as_dict_loose(seg_meta.get("script"))

    return {
        "beat_id": _safe_str(seg_meta.get("beat_id")),
        "shot_id": _safe_str(seg_meta.get("shot_id")),
        "shot_type": _safe_str(seg_meta.get("shot_type")),
        "render_route": _safe_str(seg_meta.get("render_route")),
        "title": _safe_str(seg_meta.get("title")),
        "onscreen_text": script_meta.get("onscreen_text") if isinstance(script_meta.get("onscreen_text"), list) else None,
    }


@router.post("/pricing/preview", response_model=LongformPricingPreviewResponse)
async def preview_longform(
    request: Request,
    raw_req: Dict[str, Any] = Body(...),
    user_id: str = Depends(get_current_user_id),
    _request_token: str = Depends(get_current_token),
) -> LongformPricingPreviewResponse:
    country_code = _resolve_country_code(raw_req, request)
    normalized_req = _normalize_longform_request_body(raw_req)
    requested_duration_sec = _extract_requested_duration_sec(normalized_req)
    _apply_request_pricing_context(
        normalized_req,
        country_code=country_code,
        requested_duration_sec=requested_duration_sec,
    )

    req = LongformCreateRequest.model_validate(normalized_req)
    payload = await _planning_payload_from_request(req, user_id=str(user_id), request_token=_request_token)
    _apply_request_pricing_context(
        payload,
        country_code=country_code,
        requested_duration_sec=requested_duration_sec,
    )
    try:
        artifact = await preview_longform_pricing(str(user_id), payload)
    except PricingClientError as e:
        code = _pricing_error_code(e)
        logger.warning("longform.preview_pricing_error user_id=%s code=%s error=%s", user_id, code, str(e))
        if code == "ENTITLEMENT_BLOCKED_FEATURE_FLAG":
            return LongformPricingPreviewResponse(
                pricing={
                    "message": "Upgrade your plan to use this video feature.",
                    "entitlement_reason": code,
                    "insufficient_balance": False,
                },
                pricing_summary={
                    "display_note": "Upgrade required for this feature.",
                    "cta_intent": "upgrade",
                    "blocking_reason": code,
                },
                quote_breakdown={"blocking_reason": code},
                summary={
                    "display_note": "Upgrade required for this feature.",
                    "cta_intent": "upgrade",
                    "blocking_reason": code,
                },
                insufficient_balance=False,
                message="Upgrade your plan to use this video feature.",
            )
        _raise_http_for_pricing_error(e)
        raise

    artifact_dict = _as_dict_loose(artifact)
    raw_pricing = _as_dict_loose(artifact_dict.get("pricing"))
    logger.info(
        "longform.preview_artifact user_id=%s keys=%s pricing_keys=%s",
        user_id,
        sorted(list(artifact_dict.keys())),
        sorted(list(raw_pricing.keys())),
    )
    merged_preview = {
        **artifact_dict,
        **raw_pricing,
        "quote_breakdown": _as_dict_loose(raw_pricing.get("quote_breakdown")) or _as_dict_loose(artifact_dict.get("quote_breakdown")),
        "summary": _as_dict_loose(raw_pricing.get("summary")) or _as_dict_loose(artifact_dict.get("summary")),
    }
    pricing = _normalize_pricing_view(merged_preview, payload)
    pricing_summary = _normalize_pricing_summary_view(pricing, _as_dict_loose(artifact_dict.get("pricing_summary")) or _as_dict_loose(artifact_dict.get("summary")))
    before_credits = _safe_str(pricing.get("before_credits"))
    after_estimated_credits = _safe_str(pricing.get("after_estimated_credits"))
    quote_breakdown = _as_dict_loose(pricing.get("quote_breakdown"))
    summary = _as_dict_loose(pricing.get("summary")) or pricing_summary
    estimated_amount = _safe_str(pricing.get("estimated_amount")) or _safe_str(pricing.get("amount"))
    currency = _safe_str(pricing.get("currency"))
    insufficient_balance = bool(pricing.get("insufficient_balance"))
    message = _safe_str(pricing.get("message"))
    if not message and insufficient_balance:
        message = "Not enough credits for this run."
    logger.info(
        "longform.preview_response user_id=%s country_code=%s requested_duration_sec=%s before_credits=%s after_estimated_credits=%s insufficient_balance=%s estimated_amount=%s currency=%s quote_total=%s message=%s",
        user_id,
        country_code,
        requested_duration_sec,
        before_credits,
        after_estimated_credits,
        insufficient_balance,
        estimated_amount,
        currency,
        _safe_str(quote_breakdown.get("total_credits")),
        message,
    )
    return LongformPricingPreviewResponse(
        pricing=pricing,
        pricing_summary=pricing_summary,
        before_credits=before_credits,
        after_estimated_credits=after_estimated_credits,
        estimated_amount=estimated_amount,
        currency=currency,
        quote_breakdown=quote_breakdown,
        summary=summary,
        insufficient_balance=insufficient_balance,
        message=message,
    )


@router.post("/jobs", response_model=LongformJobCreated)
async def create_longform_job(
    request: Request,
    raw_req: Dict[str, Any] = Body(...),
    pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: str = Depends(get_current_user_id),
    _request_token: str = Depends(get_current_token),
):
    country_code = _resolve_country_code(raw_req, request)
    normalized_req = _normalize_longform_request_body(raw_req)
    requested_duration_sec = _extract_requested_duration_sec(normalized_req)
    _apply_request_pricing_context(
        normalized_req,
        country_code=country_code,
        requested_duration_sec=requested_duration_sec,
    )

    req = LongformCreateRequest.model_validate(normalized_req)
    face_artifact_id = _resolve_face_artifact_id(req)
    if not face_artifact_id:
        raise HTTPException(status_code=400, detail="face_artifact_id_or_image_ref_required")

    if req.segment_seconds <= 0 or req.max_segment_seconds <= 0:
        raise HTTPException(status_code=400, detail="segment_seconds/max_segment_seconds must be positive")

    voice_gender_mode, voice_gender = _normalize_voice_policy(req)

    worker_auth_token = _service_bearer_for_workers()
    if not worker_auth_token:
        raise HTTPException(
            status_code=503,
            detail="svc_to_svc_bearer_missing: configure SVC_TO_SVC_BEARER for longform workers",
        )

    planning_payload = await _planning_payload_from_request(req, user_id=str(user_id), request_token=_request_token)
    _apply_request_pricing_context(
        planning_payload,
        country_code=country_code,
        requested_duration_sec=requested_duration_sec,
    )

    planned = build_longform_execution_payloads(planning_payload)
    segments = planned.get("segments") or []
    if not segments:
        raise HTTPException(status_code=400, detail="longform_planner_produced_no_segments")

    if len(segments) > settings.MAX_TOTAL_SEGMENTS_PER_JOB:
        raise HTTPException(
            status_code=400,
            detail=f"Too many segments ({len(segments)} > {settings.MAX_TOTAL_SEGMENTS_PER_JOB})",
        )

    normalized_tags = _as_dict_loose(normalized_req.get("tags"))
    job_tags = dict(req.tags or {})
    job_tags.update(normalized_tags)
    job_tags.update(_as_dict_loose(planned.get("job_tags")))
    job_tags.update(
        {
            "mode": planned.get("mode") or req.mode.value,
            "stage": planned.get("stage") or job_tags.get("stage") or "shot_planning",
            "scenario_type": (planned.get("scenario") or {}).get("scenario_type") or job_tags.get("scenario_type"),
            "longform_profile": _safe_str(job_tags.get("longform_profile")) or getattr(req, "longform_profile", "talking_video"),
            "camera_angle": getattr(req, "camera_angle", None) or job_tags.get("camera_angle"),
            "camera_framing": getattr(req, "camera_framing", None) or job_tags.get("camera_framing"),
            "camera_motion_style": getattr(req, "camera_motion_style", None) or job_tags.get("camera_motion_style"),
            "background_mode": getattr(req, "background_mode", None) or job_tags.get("background_mode") or "fixed",
            "country_code": country_code,
            "billing_country_code": country_code,
            "requested_duration_sec": requested_duration_sec or _extract_requested_duration_sec(planning_payload),
            "duration_sec": requested_duration_sec or _extract_requested_duration_sec(planning_payload),
            "duration_bucket_sec": _talking_duration_bucket_sec(requested_duration_sec or _extract_requested_duration_sec(planning_payload)),
        }
    )
    script_text = _resolve_job_script_text(req, planning_payload=planning_payload)
    is_directed = _safe_str(job_tags.get("longform_profile")) == "cinematic_video_direction" or _safe_str(req.mode.value) == LongformMode.directed.value

    payload_for_pricing = planning_payload

    async with pool.acquire() as conn:
        async with conn.transaction():
            job_id = await jobs_repo.create_job(
                conn,
                user_id=user_id,
                face_artifact_id=face_artifact_id,
                script_text=script_text,
                voice_cfg=req.voice_cfg.model_dump(mode="json"),
                aspect_ratio=req.aspect_ratio,
                segment_seconds=req.segment_seconds,
                max_segment_seconds=req.max_segment_seconds,
                tags=job_tags,
                total_segments=len(segments),
                auth_token=worker_auth_token,
                voice_gender_mode=voice_gender_mode,
                voice_gender=voice_gender,
            )

            for seg in segments:
                await segs_repo.insert_segment(
                    conn,
                    job_id=job_id,
                    segment_index=int(seg["segment_index"]),
                    text_chunk=str(seg.get("text_chunk") or seg.get("script_text") or ""),
                    duration_sec=_clamp_fusion_duration(int(seg.get("duration_sec") or req.segment_seconds)),
                )

        try:
            async with conn.transaction():
                pricing = await reserve_longform_pricing_for_job(
                    conn,
                    user_id=str(user_id),
                    job_id=job_id,
                    payload=payload_for_pricing,
                )
        except PricingClientError as e:
            code = _pricing_error_code(e)
            failed_status = "blocked" if code in {"PRICING_INSUFFICIENT_CREDITS", "ENTITLEMENT_BLOCKED_FEATURE_FLAG"} else "failed"
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE public.longform_jobs
                    SET status = $2::text,
                        error_code = $3::text,
                        error_message = $4::text,
                        updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    job_id,
                    failed_status,
                    code,
                    str(e),
                )
            _raise_http_for_pricing_error(e)
            raise
        except Exception as e:
            mapped_exc = PricingClientError(str(e))
            code = _pricing_error_code(mapped_exc)
            failed_status = "blocked" if code in {"PRICING_INSUFFICIENT_CREDITS", "ENTITLEMENT_BLOCKED_FEATURE_FLAG"} else "failed"
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE public.longform_jobs
                    SET status = $2::text,
                        error_code = $3::text,
                        error_message = $4::text,
                        updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    job_id,
                    failed_status,
                    code,
                    str(e),
                )
            _raise_http_for_pricing_error(mapped_exc)
            raise

        row = await jobs_repo.get_job(conn, job_id, user_id)

    scenario_type = job_tags.get("scenario_type") or None
    stage = job_tags.get("stage") or "queued"
    row_tags = _as_dict_loose(row.get("tags")) if row else job_tags

    return LongformJobCreated(
        job_id=job_id,
        status="queued",
        mode=req.mode.value,
        stage=stage,
        scenario_type=scenario_type,
        longform_profile=_safe_str(row_tags.get("longform_profile")),
        camera_angle=_safe_str(row_tags.get("camera_angle")),
        camera_framing=_safe_str(row_tags.get("camera_framing")),
        camera_motion_style=_safe_str(row_tags.get("camera_motion_style")),
        background_mode=_safe_str(row_tags.get("background_mode")),
        pricing=_normalize_pricing_view(_as_dict_loose(row_tags.get("pricing")), {"tags": row_tags}),
        pricing_summary=_normalize_pricing_summary_view(_as_dict_loose(row_tags.get("pricing")), _as_dict_loose(row_tags.get("pricing_summary"))),
    )


@router.get("/jobs/{job_id}", response_model=LongformJobView)
async def get_longform_job(
    job_id: str,
    pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: str = Depends(get_current_user_id),
):
    async with pool.acquire() as conn:
        row = await jobs_repo.get_job(conn, job_id, user_id)
        if not row:
            raise HTTPException(status_code=404, detail="Job not found")

        tags = _as_dict_loose(row.get("tags"))
        directed = _as_dict_loose(tags.get("directed_plan"))
        intent = _as_dict_loose(directed.get("intent"))
        timeline = directed.get("timeline") if isinstance(directed.get("timeline"), dict) else None
        qc = directed.get("qc") if isinstance(directed.get("qc"), dict) else None
        story_beats = directed.get("story_beats") if isinstance(directed.get("story_beats"), list) else []

        final_url = None
        if row["final_storage_path"]:
            az = AzureBlobService(settings.AZURE_STORAGE_CONNECTION_STRING)
            final_url = az.sign_read_url(
                settings.AZURE_FINAL_VIDEO_CONTAINER,
                row["final_storage_path"],
                settings.FINAL_SAS_TTL_SECONDS,
            )

        pricing_view, pricing_summary_view = await _load_latest_pricing_view(conn, str(row["id"]), {"tags": tags})
        run_receipt_view = _build_run_receipt_view(pricing_view, pricing_summary_view)

        return LongformJobView(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            status=row["status"],
            aspect_ratio=row["aspect_ratio"],
            segment_seconds=row["segment_seconds"],
            max_segment_seconds=row["max_segment_seconds"],
            total_segments=row["total_segments"],
            completed_segments=row["completed_segments"],
            final_video_url=final_url,
            final_storage_path=row["final_storage_path"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"].isoformat(),
            updated_at=row["updated_at"].isoformat(),
            voice_gender_mode=row.get("voice_gender_mode"),
            voice_gender=row.get("voice_gender"),
            mode=_safe_str(tags.get("mode")),
            stage=_safe_str(tags.get("stage")),
            scenario_type=_safe_str(tags.get("scenario_type")),
            goal=_safe_str(intent.get("goal")),
            audience=_safe_str(intent.get("audience")),
            tone=list(intent.get("tone") or []),
            style=list(intent.get("style") or []),
            longform_profile=_safe_str(tags.get("longform_profile")),
            camera_angle=_safe_str(tags.get("camera_angle")),
            camera_framing=_safe_str(tags.get("camera_framing")),
            camera_motion_style=_safe_str(tags.get("camera_motion_style")),
            background_mode=_safe_str(tags.get("background_mode")),
            story_beats=story_beats,
            timeline=timeline,
            qc_score=qc.get("score") if isinstance(qc, dict) else None,
            qc_decision=qc.get("decision") if isinstance(qc, dict) else None,
            qc=qc,
            pricing=pricing_view,
            pricing_summary=pricing_summary_view,
            run_receipt=run_receipt_view,
            runReceipt=run_receipt_view,
        )


@router.get("/jobs/{job_id}/segments", response_model=list[LongformSegmentView])
async def list_job_segments(
    job_id: str,
    pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: str = Depends(get_current_user_id),
):
    async with pool.acquire() as conn:
        job = await jobs_repo.get_job(conn, job_id, user_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        tags = _as_dict_loose(job.get("tags"))
        rows = await segs_repo.list_segments_for_job(conn, job_id)

        out: list[LongformSegmentView] = []
        for r in rows:
            extra = _enrich_segment_view_from_job_tags(r, tags)
            out.append(
                LongformSegmentView(
                    id=str(r["id"]),
                    segment_index=r["segment_index"],
                    status=r["status"],
                    duration_sec=r["duration_sec"],
                    audio_url=r["audio_url"],
                    fusion_job_id=str(r["fusion_job_id"]) if r["fusion_job_id"] else None,
                    segment_video_url=r["segment_video_url"],
                    error_code=r["error_code"],
                    error_message=r["error_message"],
                    beat_id=extra.get("beat_id"),
                    shot_id=extra.get("shot_id"),
                    shot_type=extra.get("shot_type"),
                    render_route=extra.get("render_route"),
                    title=extra.get("title"),
                )
            )
        return out
