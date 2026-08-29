from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import asyncpg
import httpx

from .config import settings
from .privacy import redact_sensitive_text
from .schemas import AssistantContextLocator

# IDs, customer-authored prose, media references and identity/payment fields stay
# out of model context. The Assistant receives workflow facts, not raw customer data.
_SENSITIVE_KEY_FRAGMENTS = (
    "account_id", "project_id", "story_id", "scene_id", "participant_id", "turn_id",
    "media_id", "url", "uri", "token", "secret", "password", "email", "phone",
    "address", "card", "payment_method", "customer_id", "receipt", "provider_request",
    "dob", "birth", "passport", "license", "ssn", "government_id",
)
_ALLOWED_GENERATION_KEY_FRAGMENTS = (
    "state", "status", "stage", "retry", "error_code", "failure_code", "locale",
    "gender", "voice", "credits", "cost", "amount", "currency", "duration", "progress",
)
_ALLOWED_PRICING_KEY_FRAGMENTS = (
    "plan", "tier", "credits", "cost", "amount", "currency", "price", "unit", "quantity",
    "premium", "discount", "balance", "available", "required", "afford", "reserved",
    "used", "included", "wallet", "promo", "billing",
)
_SCREEN_STUDIOS: dict[str, tuple[str, ...]] = {
    "face_studio": ("face",),
    "audio_studio": ("audio", "tts"),
    "fusion_studio": ("fusion", "video", "longform"),
    "story_fusion": ("fusion", "video", "longform"),
    "media_library": ("face", "audio", "tts", "fusion", "video", "longform"),
    "dashboard": ("face", "audio", "tts", "fusion", "video", "longform"),
}


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_sensitive_text(value).text[:1000]
    return str(value)[:500]


def _safe_allowlisted_dict(value: Any, *, allowed_fragments: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 3:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            low = key_text.lower()
            if any(fragment in low for fragment in _SENSITIVE_KEY_FRAGMENTS):
                continue
            if not any(fragment in low for fragment in allowed_fragments):
                continue
            result[key_text[:100]] = _safe_allowlisted_dict(
                item,
                allowed_fragments=allowed_fragments,
                depth=depth + 1,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _safe_allowlisted_dict(item, allowed_fragments=allowed_fragments, depth=depth + 1)
            for item in value[:30]
        ]
    return _safe_scalar(value)


def _safe_generation(items: list[dict] | tuple[dict, ...] | None) -> list[dict]:
    safe: list[dict] = []
    for item in list(items or ())[:50]:
        if not isinstance(item, dict):
            continue
        projected = _safe_allowlisted_dict(
            item,
            allowed_fragments=_ALLOWED_GENERATION_KEY_FRAGMENTS,
        )
        if projected:
            safe.append(projected)
    return safe


def _safe_pricing(value: Any) -> dict[str, Any]:
    projected = _safe_allowlisted_dict(value or {}, allowed_fragments=_ALLOWED_PRICING_KEY_FRAGMENTS)
    return projected if isinstance(projected, dict) else {}


def project_safe_story_context(raw: dict[str, Any], locator: AssistantContextLocator) -> dict[str, Any]:
    participant_aliases: dict[str, str] = {}
    participants = []
    for index, item in enumerate(list(raw.get("participant_context") or ())[:20], start=1):
        if not isinstance(item, dict):
            continue
        pid = str(item.get("participant_id") or "")
        alias = f"Participant {index}"
        if pid:
            participant_aliases[pid] = alias
        participants.append({
            "alias": alias,
            "kind": _safe_scalar(item.get("kind")),
            "locale": _safe_scalar(item.get("locale")),
        })

    scenes = []
    for item in list(raw.get("scene_context") or ())[:50]:
        if not isinstance(item, dict):
            continue
        scenes.append({
            "sequence": item.get("sequence"),
            "state": _safe_scalar(item.get("state")),
        })

    dialogue = []
    for item in list(raw.get("dialogue_context") or ())[:100]:
        if not isinstance(item, dict):
            continue
        speaker_id = str(item.get("speaker_participant_id") or "")
        raw_text = item.get("text")
        dialogue.append({
            "sequence": item.get("sequence"),
            "kind": _safe_scalar(item.get("kind")),
            "speaker": participant_aliases.get(
                speaker_id,
                "Narrator" if not speaker_id else "Participant",
            ),
            "locale": _safe_scalar(item.get("locale")),
            "emotion": _safe_scalar(item.get("emotion")),
            "has_text": bool(str(raw_text or "").strip()),
            "text_length": len(str(raw_text or "")) if raw_text is not None else 0,
        })

    return {
        "surface": locator.surface,
        "screen": locator.screen,
        "creation_type": _safe_scalar(raw.get("creation_type")),
        "context_scope": _safe_scalar(raw.get("context_scope")),
        "participants": participants,
        "scenes": scenes,
        "dialogue": dialogue,
        "generation": _safe_generation(raw.get("generation_context")),
        "pricing": _safe_pricing(raw.get("pricing_context")),
        "allowed_actions": [
            str(action)[:100]
            for action in list(raw.get("allowed_assistant_actions") or ())[:30]
        ],
    }


def _job_is_relevant(screen: str, job: dict[str, Any]) -> bool:
    allowed = _SCREEN_STUDIOS.get(screen)
    if not allowed:
        return False
    studio = str(job.get("studio") or "").strip().lower()
    return studio in allowed or screen in {"dashboard", "media_library"}


def _safe_live_job(item: dict[str, Any], index: int) -> dict[str, Any]:
    studio = str(item.get("studio") or "generation").strip().lower()[:40]
    people_mode = str(item.get("people_mode") or "single_or_unspecified")[:40]
    if studio in {"longform", "fusion", "video"}:
        kind = "video"
    elif studio in {"tts", "audio"}:
        kind = "audio"
    elif studio == "face":
        kind = "face"
    else:
        kind = "generation"
    descriptor = "multi-person " if people_mode == "multi_person" else ""
    return {
        "alias": f"Recent {descriptor}{kind} generation {index}",
        "kind": kind,
        "people_mode": people_mode,
        "status": _safe_scalar(item.get("status")),
        "stage": _safe_scalar(item.get("stage")),
        "failure_code": _safe_scalar(item.get("failure_code")),
        "retryable": bool(item.get("retryable")),
        "progress": _safe_scalar(item.get("progress")),
        "created_at": _safe_scalar(item.get("created_at")),
        "updated_at": _safe_scalar(item.get("updated_at")),
        "final_output_available": bool(item.get("final_output_available")),
    }


def project_safe_live_context(
    home: dict[str, Any],
    studio_jobs: list[dict[str, Any]],
    longform_jobs: list[dict[str, Any]],
    locator: AssistantContextLocator,
) -> dict[str, Any]:
    recent_final_videos: list[dict[str, Any]] = []
    for index, item in enumerate(list(home.get("video_carousel") or ())[:8], start=1):
        if not isinstance(item, dict):
            continue
        recent_final_videos.append({
            "alias": f"Recent final video {index}",
            "kind": "video",
            "status": _safe_scalar(item.get("status") or "ready"),
            "created_at": _safe_scalar(item.get("created_at")),
            "final_output_available": True,
        })

    all_jobs = [
        item for item in [*longform_jobs, *studio_jobs]
        if isinstance(item, dict) and _job_is_relevant(locator.screen, item)
    ]
    all_jobs.sort(
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )
    generation = [
        _safe_live_job(item, index)
        for index, item in enumerate(all_jobs[:12], start=1)
    ]

    return {
        "surface": locator.surface,
        "screen": locator.screen,
        "context_scope": "live_user_application_state",
        "live_context_available": bool(home or generation),
        "dashboard": {
            "recent_face_count": len(list(home.get("face_carousel") or ())[:50]),
            "recent_final_video_count": len(recent_final_videos),
            "recent_final_videos": recent_final_videos,
        },
        "generation": generation,
        "pricing": {
            "plan": _safe_pricing(home.get("plan")),
            "credits": _safe_pricing(home.get("credits")),
            "summary": _safe_pricing(home.get("pricing_summary")),
            "runway": _safe_pricing(home.get("runway_summary")),
        },
        "allowed_actions": ["check_price"] if locator.screen in {"dashboard", "pricing"} else [],
    }


class ContextResolver:
    def __init__(self, client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
        self._client = client
        self._pool = pool

    async def _fetch_dashboard_home(self, *, token: str) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{settings.DF_DASHBOARD_BASE_URL}/api/dashboard/home",
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code in {401, 403}:
                response.raise_for_status()
            if response.status_code >= 400:
                return {}
            raw = response.json()
            return raw if isinstance(raw, dict) else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def _fetch_recent_studio_jobs(self, user_id: UUID) -> list[dict[str, Any]]:
        query = r"""
        select
            coalesce(j->>'studio', j->>'service', j->>'job_type', j->>'kind', '') as studio,
            coalesce(j->>'status', j->>'state', '') as status,
            coalesce(j->>'stage', j->>'current_stage', '') as stage,
            coalesce(j->>'failure_code', j->>'error_code', '') as failure_code,
            coalesce(j->>'progress', j->>'progress_percent', '') as progress,
            coalesce(j->>'created_at', '') as created_at,
            coalesce(j->>'updated_at', '') as updated_at,
            case
                when lower(j::text) ~ '(multi[_ -]?person|multiperson|multi[_ -]?face|couple|group)'
                then 'multi_person'
                else 'single_or_unspecified'
            end as people_mode,
            case
                when lower(coalesce(j->>'retryable', 'false')) = 'true' then true
                else false
            end as retryable,
            false as final_output_available
        from (
            select to_jsonb(sj) as j
            from public.studio_jobs sj
            where sj.user_id = $1
            order by coalesce(sj.updated_at, sj.created_at) desc
            limit 20
        ) recent
        """
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(query, user_id)
            return [dict(row) for row in rows]
        except Exception:
            return []

    async def _fetch_recent_longform_jobs(self, user_id: UUID) -> list[dict[str, Any]]:
        query = r"""
        select
            'longform'::text as studio,
            coalesce(j->>'status', j->>'state', j->>'job_status', '') as status,
            coalesce(j->>'stage', j->>'current_stage', j->>'phase', '') as stage,
            coalesce(j->>'failure_code', j->>'error_code', '') as failure_code,
            coalesce(j->>'progress', j->>'progress_percent', '') as progress,
            coalesce(j->>'created_at', '') as created_at,
            coalesce(j->>'updated_at', j->>'completed_at', '') as updated_at,
            case
                when lower(j::text) ~ '(multi[_ -]?person|multiperson|multi[_ -]?face|couple|group)'
                then 'multi_person'
                else 'single_or_unspecified'
            end as people_mode,
            case
                when lower(coalesce(j->>'retryable', 'false')) = 'true' then true
                else false
            end as retryable,
            (
                coalesce(j->>'final_video_url', '') <> ''
                or coalesce(j->>'final_storage_path', '') <> ''
            ) as final_output_available
        from (
            select to_jsonb(lj) as j
            from public.longform_jobs lj
            where lj.user_id = $1
            order by coalesce(lj.updated_at, lj.created_at) desc
            limit 16
        ) recent
        """
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(query, user_id)
            return [dict(row) for row in rows]
        except Exception:
            return []

    async def resolve(
        self,
        locator: AssistantContextLocator,
        *,
        token: str,
        user_id: UUID,
    ) -> dict[str, Any]:
        if locator.story_id is not None:
            url = f"{settings.DF_DIRECTOR_BASE_URL}/api/director/stories/{locator.story_id}/assistant-context"
            params = {}
            if locator.scene_id is not None:
                params["scene_id"] = str(locator.scene_id)
            if locator.participant_id is not None:
                params["participant_id"] = str(locator.participant_id)

            response = await self._client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code == 404:
                return {
                    "surface": locator.surface,
                    "screen": locator.screen,
                    "context_status": "not_found",
                }
            response.raise_for_status()
            raw = response.json()
            if not isinstance(raw, dict):
                return {"surface": locator.surface, "screen": locator.screen}
            return project_safe_story_context(raw, locator)

        home, studio_jobs, longform_jobs = await asyncio.gather(
            self._fetch_dashboard_home(token=token),
            self._fetch_recent_studio_jobs(user_id),
            self._fetch_recent_longform_jobs(user_id),
        )
        return project_safe_live_context(home, studio_jobs, longform_jobs, locator)
