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


def _participant_gender(persona: dict[str, Any], metadata: dict[str, Any]) -> str:
    constraints = _as_dict(metadata.get("explicit_face_constraints"))
    value = (
        constraints.get("gender")
        or constraints.get("gender_presentation")
        or persona.get("gender")
        or persona.get("gender_presentation")
    )
    return _gender(value)


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

    persona = _as_dict(context["persona_json"])
    metadata = _as_dict(context["metadata_json"])
    expected_gender = _participant_gender(persona, metadata)

    catalog_voice = await _load_catalog_voice(
        request=request,
        locale=voice_locale,
        voice_id=voice_id,
    )
    catalog_gender = _gender(catalog_voice.get("gender"))
    if (
        expected_gender in {"male", "female"}
        and catalog_gender in {"male", "female"}
        and expected_gender != catalog_gender
    ):
        raise HTTPException(status_code=422, detail="audio_voice_gender_mismatch")

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
                select voice_profile_ref,voice_locale
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
            changing = current_voice != voice_id or current_locale != voice_locale
            if changing and int(locked or 0) > 0:
                raise HTTPException(
                    status_code=409,
                    detail="participant_voice_locked_after_audio_generation",
                )

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
                json.dumps(
                    {
                        "audio_voice_selection_source": "user",
                        "audio_voice_gender": catalog_gender,
                        "audio_voice_locale": voice_locale,
                    }
                ),
            )

            # Important: do not rewrite v3_dialogue_turns.locale here. That field
            # is the canonical language of the authored dialogue. The participant
            # voice_locale is the target speech locale and the runtime bridge
            # requests translation explicitly when source and target languages
            # differ.

    return {
        "workflow_id": str(workflow_id),
        "participant_id": str(participant_id),
        "display_name": _clean(context["display_name"]) or "Character",
        "voice_id": voice_id,
        "voice_locale": voice_locale,
        "voice_gender": catalog_gender,
        "voice_display_name": _clean(catalog_voice.get("display_name")) or voice_id,
        "applies_to": "all_dialogue_turns_for_participant",
    }


__all__ = ["router"]
