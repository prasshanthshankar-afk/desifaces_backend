from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .security import DirectorAuthContext, get_director_auth

router = APIRouter()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value or {})
    except Exception:
        return {}


def _normalize_gender(value: Any) -> str | None:
    raw = _clean(value).casefold().replace("_", "-")
    if raw in {"male", "man", "boy", "masculine"}:
        return "male"
    if raw in {"female", "woman", "girl", "feminine"}:
        return "female"
    return None


class FaceProfileIn(BaseModel):
    gender_presentation: str = Field(min_length=1, max_length=40)
    age_presentation: str | None = Field(default=None, max_length=80)


async def _load_preflight(conn, *, workflow_id: UUID, account_id: UUID) -> dict[str, Any]:
    workflow = await conn.fetchrow(
        """
        select workflow_id,story_id,project_id,current_stage,state
        from public.v3_studio_workflows
        where workflow_id=$1 and account_id=$2
        """,
        workflow_id,
        account_id,
    )
    if not workflow:
        raise HTTPException(status_code=404, detail={
            "code": "studio_workflow_not_found",
            "message": "This Story Studio session could not be found.",
            "recoverable": False,
        })

    face_rows = await conn.fetch(
        """
        select s.stage_run_id,s.state,p.participant_id,p.display_name,p.primary_face_media_id,
               p.persona_json,p.metadata_json
        from public.v3_studio_stage_runs s
        join public.v3_participants p on p.participant_id=s.participant_id
        where s.workflow_id=$1 and s.stage_type='face' and s.scope_type='participant'
        order by s.created_at,s.stage_run_id
        """,
        workflow_id,
    )

    face_items: list[dict[str, Any]] = []
    for row in face_rows:
        metadata = _as_dict(row["metadata_json"])
        explicit = _as_dict(metadata.get("explicit_face_constraints"))
        persona = _as_dict(row["persona_json"])
        gender = _normalize_gender(explicit.get("gender") or persona.get("gender_presentation"))
        missing: list[str] = []
        state = _clean(row["state"])
        locked = state == "approved"
        if not locked and not row["primary_face_media_id"] and not gender:
            missing.append("gender_presentation")
        face_items.append({
            "stage_run_id": str(row["stage_run_id"]),
            "participant_id": str(row["participant_id"]),
            "display_name": _clean(row["display_name"]),
            "state": state,
            "locked": locked,
            "has_saved_or_primary_face": bool(row["primary_face_media_id"]),
            "gender_presentation": gender,
            "missing_fields": missing,
            "ready_for_pricing": locked or not missing,
            "actions": (
                ["continue"] if locked else
                (["use_saved_face", "generate_new_face"] if not missing else
                 ["choose_gender_presentation", "use_saved_face"])
            ),
            "user_message": (
                "Face locked and ready for the next Studio." if locked else
                "Ready to check the price for a new Face." if not missing else
                f"Choose how {_clean(row['display_name']) or 'this character'} should be presented, or use a saved Face."
            ),
        })

    audio_rows = await conn.fetch(
        """
        select s.stage_run_id,s.state,s.dialogue_turn_id,dt.speaker_participant_id,
               p.display_name,p.voice_profile_ref,p.voice_locale
        from public.v3_studio_stage_runs s
        join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
        join public.v3_participants p on p.participant_id=dt.speaker_participant_id
        where s.workflow_id=$1 and s.stage_type='audio' and s.scope_type='dialogue_turn'
        order by dt.sequence_no,dt.turn_id
        """,
        workflow_id,
    )
    audio_counts: dict[str, int] = {}
    speaker_map: dict[str, dict[str, Any]] = {}
    for row in audio_rows:
        state = _clean(row["state"])
        audio_counts[state] = audio_counts.get(state, 0) + 1
        participant_id = str(row["speaker_participant_id"])
        speaker_map.setdefault(participant_id, {
            "participant_id": participant_id,
            "display_name": _clean(row["display_name"]),
            "voice_profile_ref": _clean(row["voice_profile_ref"]) or None,
            "voice_locale": _clean(row["voice_locale"]) or None,
        })
    speakers = list(speaker_map.values())
    for speaker in speakers:
        speaker["ready"] = bool(speaker["voice_profile_ref"] and speaker["voice_locale"])
        speaker["user_message"] = (
            "Voice ready." if speaker["ready"] else
            "desifaces needs a language/voice choice before Audio generation."
        )

    fusion_rows = await conn.fetch(
        """
        select stage_run_id,scene_id,state
        from public.v3_studio_stage_runs
        where workflow_id=$1 and stage_type='fusion' and scope_type='scene'
        order by created_at,stage_run_id
        """,
        workflow_id,
    )
    fusion_items = [
        {
            "stage_run_id": str(row["stage_run_id"]),
            "scene_id": str(row["scene_id"]),
            "state": _clean(row["state"]),
            "ready_for_pricing": (
                _clean(workflow["current_stage"]) == "fusion"
                and _clean(row["state"]) in {"pending", "ready", "failed", "rejected"}
            ),
        }
        for row in fusion_rows
    ]

    face_approved = sum(1 for item in face_items if item["state"] == "approved")
    audio_approved = int(audio_counts.get("approved", 0))
    return {
        "workflow_id": str(workflow["workflow_id"]),
        "story_id": str(workflow["story_id"]) if workflow["story_id"] else None,
        "project_id": str(workflow["project_id"]),
        "workflow_state": _clean(workflow["state"]),
        "current_stage": _clean(workflow["current_stage"]),
        "face": {
            "approved": face_approved,
            "total": len(face_items),
            "ready": all(item["ready_for_pricing"] or item["locked"] for item in face_items),
            "items": face_items,
        },
        "audio": {
            "approved": audio_approved,
            "total": len(audio_rows),
            "speakers_ready": all(bool(item["ready"]) for item in speakers) if speakers else False,
            "speakers": speakers,
            "states": audio_counts,
        },
        "fusion": {
            "approved": sum(1 for item in fusion_items if item["state"] == "approved"),
            "total": len(fusion_items),
            "items": fusion_items,
            "ready": bool(fusion_items) and all(item["ready_for_pricing"] or item["state"] == "approved" for item in fusion_items),
        },
    }


@router.get("/api/director/studio-workflows/{workflow_id}/preflight")
async def get_studio_preflight(
    workflow_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    async with request.app.state.business_pool.acquire() as conn:
        return await _load_preflight(conn, workflow_id=workflow_id, account_id=auth.account_id)


@router.put(
    "/api/director/studio-workflows/{workflow_id}/participants/{participant_id}/face-profile"
)
async def set_face_profile(
    workflow_id: UUID,
    participant_id: UUID,
    body: FaceProfileIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    gender = _normalize_gender(body.gender_presentation)
    if not gender:
        raise HTTPException(status_code=422, detail={
            "code": "face_gender_presentation_unsupported",
            "message": "Choose a supported Face presentation before creating a new Face.",
            "recoverable": True,
            "action": "choose_gender_presentation",
            "allowed_values": ["female", "male"],
        })

    async with request.app.state.business_pool.acquire() as conn:
        async with conn.transaction():
            stage = await conn.fetchrow(
                """
                select s.stage_run_id,s.state,p.display_name,p.metadata_json,p.persona_json
                from public.v3_studio_stage_runs s
                join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
                join public.v3_participants p on p.participant_id=s.participant_id
                where s.workflow_id=$1 and s.participant_id=$2 and w.account_id=$3
                  and s.stage_type='face' and s.scope_type='participant'
                order by s.created_at desc limit 1
                for update of s,p
                """,
                workflow_id,
                participant_id,
                auth.account_id,
            )
            if not stage:
                raise HTTPException(status_code=404, detail={
                    "code": "face_participant_not_found",
                    "message": "This character is not part of the current Story Face Studio.",
                    "recoverable": False,
                })
            if _clean(stage["state"]) in {"generating", "awaiting_review", "approved"}:
                raise HTTPException(status_code=409, detail={
                    "code": "face_profile_locked",
                    "message": "This character profile is locked because Face generation or review has already started.",
                    "recoverable": False,
                })

            metadata = _as_dict(stage["metadata_json"])
            persona = _as_dict(stage["persona_json"])
            explicit = _as_dict(metadata.get("explicit_face_constraints"))
            explicit["gender"] = gender
            if body.age_presentation:
                explicit["age"] = _clean(body.age_presentation)
                persona["age"] = _clean(body.age_presentation)
            persona["gender_presentation"] = gender
            metadata["explicit_face_constraints"] = explicit
            provenance = _as_dict(metadata.get("production_provenance"))
            provenance["explicit_face_constraints"] = "user_confirmed_in_story_studio"
            metadata["production_provenance"] = provenance

            await conn.execute(
                """
                update public.v3_participants
                set metadata_json=$2::jsonb,persona_json=$3::jsonb,updated_at=now()
                where participant_id=$1
                """,
                participant_id,
                metadata,
                persona,
            )

        return await _load_preflight(conn, workflow_id=workflow_id, account_id=auth.account_id)


__all__ = ["router"]
