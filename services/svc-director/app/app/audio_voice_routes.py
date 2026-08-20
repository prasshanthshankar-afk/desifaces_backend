from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .config import settings
from .security import DirectorAuthContext, get_director_auth

router = APIRouter()


class ParticipantVoiceProfileIn(BaseModel):
    voice_id: str = Field(min_length=1, max_length=300)
    voice_locale: str = Field(min_length=2, max_length=64)
    style: str | None = Field(default=None, max_length=120)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


def _gender(value: Any) -> str:
    raw = _clean(value).casefold()
    return {
        "man": "male",
        "male": "male",
        "m": "male",
        "woman": "female",
        "female": "female",
        "f": "female",
        "neutral": "neutral",
        "nonbinary": "neutral",
        "non-binary": "neutral",
    }.get(raw, "unspecified")


def _voice_styles(voice: dict[str, Any]) -> list[str]:
    meta = _as_dict(voice.get("meta_json"))
    raw = (
        voice.get("styles")
        or voice.get("style_list")
        or meta.get("StyleList")
        or meta.get("style_list")
        or meta.get("styles")
        or []
    )
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        values = [_clean(item) for item in raw]
    else:
        values = []
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _forward_auth(request: Request) -> dict[str, str]:
    value = _clean(request.headers.get("authorization"))
    if not value:
        raise HTTPException(status_code=401, detail="authorization_header_required")
    return {"Authorization": value}


async def _load_catalog_voice(
    *,
    request: Request,
    locale: str,
    voice_id: str,
) -> dict[str, Any]:
    headers = _forward_auth(request)
    async with httpx.AsyncClient(
        base_url=settings.DF_AUDIO_BASE_URL.rstrip("/"),
        headers=headers,
        timeout=30.0,
    ) as client:
        response = await client.get(
            "/api/audio/catalog/voices",
            params={"locale": locale},
        )
    if response.status_code != 200:
        raise HTTPException(
            status_code=409,
            detail=f"audio_voice_catalog_failed:{response.status_code}",
        )
    payload = response.json()
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        items = []
    selected = next(
        (
            item
            for item in items
            if isinstance(item, dict)
            and _clean(item.get("voice_name")) == voice_id
        ),
        None,
    )
    if selected is None:
        raise HTTPException(status_code=422, detail="audio_voice_not_available_for_locale")
    return selected


@router.put(
    "/api/director/studio-workflows/{workflow_id}/participants/{participant_id}/voice-profile"
)
async def set_participant_voice_profile(
    workflow_id: UUID,
    participant_id: UUID,
    body: ParticipantVoiceProfileIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    voice_id = _clean(body.voice_id)
    voice_locale = _clean(body.voice_locale)
    requested_style = _clean(body.style) or None
    pool = request.app.state.business_pool

    async with pool.acquire() as conn:
        context = await conn.fetchrow(
            """
            select
              w.workflow_id,w.project_id,w.current_stage,
              p.participant_id,p.display_name,p.voice_profile_ref,p.voice_locale,
              p.persona_json,p.metadata_json
            from public.v3_studio_workflows w
            join public.v3_participants p
              on p.participant_id=$2
             and p.account_id=w.account_id
             and p.project_id=w.project_id
             and p.lifecycle_state='active'
            where w.workflow_id=$1 and w.account_id=$3
              and exists (
                select 1
                from public.v3_studio_stage_runs s
                join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
                where s.workflow_id=w.workflow_id
                  and s.stage_type='audio'
                  and s.scope_type='dialogue_turn'
                  and dt.speaker_participant_id=p.participant_id
              )
            """,
            workflow_id,
            participant_id,
            auth.account_id,
        )
    if not context:
        raise HTTPException(status_code=404, detail="participant_audio_profile_not_found")
    if _clean(context["current_stage"]) != "audio":
        raise HTTPException(status_code=409, detail="participant_voice_configuration_not_current")

    catalog_voice = await _load_catalog_voice(
        request=request,
        locale=voice_locale,
        voice_id=voice_id,
    )
    catalog_gender = _gender(catalog_voice.get("gender"))
    available_styles = _voice_styles(catalog_voice)

    # Voice gender is descriptive catalog metadata, not a restriction. A Story
    # character's Face gender never forces the user's TTS voice choice.
    resolved_style: str | None = None
    if requested_style:
        match = next(
            (value for value in available_styles if value.casefold() == requested_style.casefold()),
            None,
        )
        if available_styles and match is None:
            raise HTTPException(status_code=422, detail="audio_voice_style_not_available")
        if not available_styles and not bool(catalog_voice.get("supports_styles")):
            raise HTTPException(status_code=422, detail="audio_voice_style_not_supported")
        resolved_style = match or requested_style

    async with pool.acquire() as conn:
        async with conn.transaction():
            locked = await conn.fetchval(
                """
                select count(*)
                from public.v3_studio_stage_runs s
                join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
                where s.workflow_id=$1
                  and s.stage_type='audio'
                  and s.scope_type='dialogue_turn'
                  and dt.speaker_participant_id=$2
                  and s.state in ('generating','awaiting_review','approved')
                """,
                workflow_id,
                participant_id,
            )
            current = await conn.fetchrow(
                """
                select voice_profile_ref,voice_locale,metadata_json
                from public.v3_participants
                where participant_id=$1 and account_id=$2
                for update
                """,
                participant_id,
                auth.account_id,
            )
            if not current:
                raise HTTPException(status_code=404, detail="participant_not_found")

            current_voice = _clean(current["voice_profile_ref"])
            current_locale = _clean(current["voice_locale"])
            current_meta = _as_dict(current["metadata_json"])
            current_delivery = _as_dict(current_meta.get("audio_delivery"))
            current_style = _clean(current_delivery.get("style")) or None
            changing = (
                current_voice != voice_id
                or current_locale != voice_locale
                or current_style != resolved_style
            )
            if changing and int(locked or 0) > 0:
                raise HTTPException(
                    status_code=409,
                    detail="participant_voice_locked_after_audio_generation",
                )

            delivery = dict(current_delivery)
            if resolved_style:
                delivery["style"] = resolved_style
            else:
                delivery.pop("style", None)

            metadata_patch = {
                "audio_voice_selection_source": "user",
                "audio_voice_gender": catalog_gender,
                "audio_voice_locale": voice_locale,
                "audio_delivery": delivery,
            }
            await conn.execute(
                """
                update public.v3_participants
                set voice_profile_ref=$3,
                    voice_locale=$4,
                    metadata_json=coalesce(metadata_json,'{}'::jsonb) || $5::jsonb,
                    updated_at=now()
                where participant_id=$1 and account_id=$2
                """,
                participant_id,
                auth.account_id,
                voice_id,
                voice_locale,
                json.dumps(metadata_patch),
            )

            # Do not rewrite v3_dialogue_turns.locale. That field is the canonical
            # authored language. voice_locale is the selected target speech locale;
            # the runtime bridge requests translation when required.

    return {
        "workflow_id": str(workflow_id),
        "participant_id": str(participant_id),
        "display_name": _clean(context["display_name"]) or "Character",
        "voice_id": voice_id,
        "voice_locale": voice_locale,
        "voice_gender": catalog_gender,
        "voice_display_name": _clean(catalog_voice.get("display_name")) or voice_id,
        "style": resolved_style,
        "available_styles": available_styles,
        "applies_to": "all_dialogue_turns_for_participant",
    }


__all__ = ["router"]
