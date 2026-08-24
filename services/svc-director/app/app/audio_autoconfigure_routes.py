from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from .config import settings
from .security import DirectorAuthContext, get_director_auth

router = APIRouter()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value or {})
    except Exception:
        return {}


def _language_code(locale: Any) -> str:
    value = _clean(locale).replace("_", "-")
    return value.split("-", 1)[0].casefold() if value else ""


def _region_code(locale: Any) -> str:
    parts = _clean(locale).replace("_", "-").split("-")
    for part in reversed(parts[1:]):
        if len(part) == 2 and part.isalpha():
            return part.upper()
    return ""


def _forward_auth(request: Request) -> dict[str, str]:
    value = _clean(request.headers.get("authorization"))
    if not value:
        raise HTTPException(status_code=401, detail="authorization_header_required")
    return {"Authorization": value}


async def _catalog(request: Request) -> list[dict[str, Any]]:
    headers = _forward_auth(request)
    base_url = settings.DF_AUDIO_BASE_URL.rstrip("/")
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30.0) as client:
        locales_response = await client.get(
            "/api/audio/catalog/locales",
            params={"end_to_end_only": "true", "enabled_only": "true"},
        )
    if locales_response.status_code != 200:
        raise HTTPException(status_code=424, detail={
            "code": "audio_catalog_unavailable",
            "message": "Audio choices are temporarily unavailable. Please try again.",
            "recoverable": True,
            "action": "retry",
        })
    payload = locales_response.json()
    return [
        dict(item)
        for item in list(payload.get("items") or [])
        if isinstance(item, dict) and _clean(item.get("locale")) and _clean(item.get("default_voice"))
    ]


def _choose_locale(
    *,
    existing_locale: str,
    participant_locale: str,
    authored_locales: list[str],
    catalog: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    by_code = {_clean(item.get("locale")): item for item in catalog}

    if existing_locale and existing_locale in by_code:
        return by_code[existing_locale], "existing_profile"
    if participant_locale and participant_locale in by_code:
        return by_code[participant_locale], "participant_locale"

    counts = Counter(_clean(value) for value in authored_locales if _clean(value))
    authored = [value for value, _ in counts.most_common()]
    for value in authored:
        if value in by_code:
            return by_code[value], "authored_dialogue_locale"

    # Preserve the authored language. Region is only a deterministic tiebreaker
    # between executable locales of that same language; it never changes language.
    for value in authored + ([participant_locale] if participant_locale else []):
        language = _language_code(value)
        if not language:
            continue
        region = _region_code(value)
        candidates = [
            item for item in catalog
            if _language_code(item.get("locale")) == language
        ]
        if not candidates:
            continue
        candidates.sort(
            key=lambda item: (
                0 if region and _region_code(item.get("locale")) == region else 1,
                _clean(item.get("locale")),
            )
        )
        return candidates[0], "authored_language_default_locale"

    return None, "needs_user_choice"


async def _load_voice_catalogs(
    request: Request,
    *,
    locales: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Fetch each required locale once with bounded parallelism.

    Multi-person stories can have many characters but usually share languages. This
    avoids one serial client setup/request per participant while leaving svc-audio as
    the authoritative catalog owner.
    """
    headers = _forward_auth(request)
    base_url = settings.DF_AUDIO_BASE_URL.rstrip("/")
    semaphore = asyncio.Semaphore(6)
    results: dict[str, list[dict[str, Any]]] = {}

    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=30.0,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=6),
    ) as client:
        async def fetch(locale: str) -> None:
            async with semaphore:
                response = await client.get("/api/audio/catalog/voices", params={"locale": locale})
            if response.status_code != 200:
                results[locale] = []
                return
            results[locale] = [
                item for item in list(response.json().get("items") or []) if isinstance(item, dict)
            ]

        await asyncio.gather(*(fetch(locale) for locale in sorted(locales)))
    return results


def _choose_voice(
    voices: list[dict[str, Any]],
    *,
    preferred_voice: str,
) -> dict[str, Any] | None:
    if preferred_voice:
        existing = next(
            (item for item in voices if _clean(item.get("voice_name")) == preferred_voice),
            None,
        )
        if existing:
            return existing
    return next((item for item in voices if bool(item.get("is_default"))), None) or (
        voices[0] if voices else None
    )


@router.post("/api/director/studio-workflows/{workflow_id}/audio-autoconfigure")
async def autoconfigure_story_audio(
    workflow_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    pool = request.app.state.business_pool
    async with pool.acquire() as conn:
        workflow = await conn.fetchrow(
            """
            select workflow_id,current_stage,project_id
            from public.v3_studio_workflows
            where workflow_id=$1 and account_id=$2
            """,
            workflow_id,
            auth.account_id,
        )
        if not workflow:
            raise HTTPException(status_code=404, detail={
                "code": "studio_workflow_not_found",
                "message": "This Story Studio session could not be found.",
                "recoverable": False,
            })
        if _clean(workflow["current_stage"]) != "audio":
            raise HTTPException(status_code=409, detail={
                "code": "audio_not_ready",
                "message": "Finish and approve the cast before preparing character voices.",
                "recoverable": True,
                "action": "complete_face_cast",
            })

        rows = await conn.fetch(
            """
            select p.participant_id,p.display_name,p.default_locale,p.voice_profile_ref,p.voice_locale,
                   p.metadata_json,dt.locale as authored_locale
            from public.v3_studio_stage_runs s
            join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
            join public.v3_participants p on p.participant_id=dt.speaker_participant_id
            where s.workflow_id=$1 and s.stage_type='audio' and s.scope_type='dialogue_turn'
            order by p.participant_id,dt.sequence_no,dt.turn_id
            """,
            workflow_id,
        )

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        participant_id = str(row["participant_id"])
        metadata = _as_dict(row["metadata_json"])
        delivery = _as_dict(metadata.get("audio_delivery"))
        item = grouped.setdefault(participant_id, {
            "participant_id": participant_id,
            "display_name": _clean(row["display_name"]) or "Character",
            "default_locale": _clean(row["default_locale"]),
            "voice_profile_ref": _clean(row["voice_profile_ref"]),
            "voice_locale": _clean(row["voice_locale"]),
            "style": _clean(delivery.get("style")),
            "metadata": metadata,
            "authored_locales": [],
        })
        if _clean(row["authored_locale"]):
            item["authored_locales"].append(_clean(row["authored_locale"]))

    catalog = await _catalog(request)
    prepared: dict[str, tuple[dict[str, Any] | None, str]] = {}
    required_locales: set[str] = set()
    for participant_id, item in grouped.items():
        locale_item, source = _choose_locale(
            existing_locale=item["voice_locale"],
            participant_locale=item["default_locale"],
            authored_locales=item["authored_locales"],
            catalog=catalog,
        )
        prepared[participant_id] = (locale_item, source)
        if locale_item is not None:
            locale = _clean(locale_item.get("locale"))
            if locale:
                required_locales.add(locale)

    voices_by_locale = await _load_voice_catalogs(request, locales=required_locales)
    results: list[dict[str, Any]] = []

    for participant_id, item in grouped.items():
        locale_item, source = prepared[participant_id]
        if locale_item is None:
            results.append({
                "participant_id": participant_id,
                "display_name": item["display_name"],
                "ready": False,
                "status": "needs_user_choice",
                "style": item["style"] or None,
                "message": f"Choose a language for {item['display_name']}.",
            })
            continue

        locale = _clean(locale_item.get("locale"))
        voice = _choose_voice(
            voices_by_locale.get(locale, []),
            preferred_voice=item["voice_profile_ref"],
        )
        if voice is None:
            results.append({
                "participant_id": participant_id,
                "display_name": item["display_name"],
                "ready": False,
                "status": "needs_user_choice",
                "locale": locale,
                "style": item["style"] or None,
                "message": f"Choose an available voice for {item['display_name']}.",
            })
            continue

        voice_id = _clean(voice.get("voice_name"))
        existing_metadata = _as_dict(item.get("metadata"))
        provenance = _as_dict(existing_metadata.get("production_provenance"))
        provenance["voice_profile"] = (
            "service_resolved_default" if source != "existing_profile" else "user_or_prior_selection"
        )
        metadata_patch = {
            "audio_voice_selection_source": (
                "preserved" if source == "existing_profile" and voice_id == item["voice_profile_ref"]
                else "desifaces_suggested_default"
            ),
            "audio_voice_locale": locale,
            "audio_voice_gender": _clean(voice.get("gender")).casefold() or "unspecified",
            "audio_voice_suggestion_source": source,
            "production_provenance": provenance,
        }

        async with pool.acquire() as conn:
            async with conn.transaction():
                locked = await conn.fetchval(
                    """
                    select count(*)
                    from public.v3_studio_stage_runs s
                    join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
                    where s.workflow_id=$1 and s.stage_type='audio' and s.scope_type='dialogue_turn'
                      and dt.speaker_participant_id=$2
                      and s.state in ('generating','awaiting_review','approved')
                    """,
                    workflow_id,
                    UUID(participant_id),
                )
                if int(locked or 0) == 0:
                    await conn.execute(
                        """
                        update public.v3_participants
                        set voice_profile_ref=$3,voice_locale=$4,
                            metadata_json=coalesce(metadata_json,'{}'::jsonb) || $5::jsonb,
                            updated_at=now()
                        where participant_id=$1 and account_id=$2
                        """,
                        UUID(participant_id),
                        auth.account_id,
                        voice_id,
                        locale,
                        json.dumps(metadata_patch, ensure_ascii=False),
                    )

        results.append({
            "participant_id": participant_id,
            "display_name": item["display_name"],
            "ready": True,
            "status": "preserved" if source == "existing_profile" else "suggested",
            "locale": locale,
            "language": _clean(locale_item.get("display_name")) or locale,
            "native_name": _clean(locale_item.get("native_name")) or None,
            "voice_id": voice_id,
            "voice_display_name": _clean(voice.get("display_name")) or voice_id,
            "voice_gender": _clean(voice.get("gender")) or None,
            "style": item["style"] or None,
            "message": (
                "Existing voice preserved." if source == "existing_profile"
                else "desifaces selected a compatible default voice. You can change it before generation."
            ),
        })

    return {
        "workflow_id": str(workflow_id),
        "ready": bool(results) and all(bool(item.get("ready")) for item in results),
        "characters": results,
    }


__all__ = ["router"]
