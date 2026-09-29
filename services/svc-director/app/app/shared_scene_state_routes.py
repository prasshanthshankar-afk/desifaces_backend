from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from .security import DirectorAuthContext, get_director_auth


router = APIRouter()

SourceMode = Literal["generate", "upload"]
_SHARED_SCENE_CONTRACT_VERSION = 1


class SharedSceneSourceModeIn(BaseModel):
    mode: SourceMode


class SharedSceneGroupPhotoSpecIn(BaseModel):
    scene_country_code: str | None = Field(default=None, max_length=16)
    scene_region_code: str | None = Field(default=None, max_length=160)
    context_code: str | None = Field(default=None, max_length=160)
    background: str | None = Field(default=None, max_length=1600)
    prompt: str = Field(min_length=1, max_length=8000)
    variant_count: int = 2
    aspect_ratio: Literal["16:9"] = "16:9"

    @model_validator(mode="after")
    def validate_variants(self):
        if int(self.variant_count) not in {1, 2, 4, 6, 8}:
            raise ValueError("shared_scene_group_photo_variant_count_unsupported")
        self.variant_count = int(self.variant_count)
        return self


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value or {})
    except Exception:
        return {}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _profile_from_row(row) -> dict[str, Any]:
    metadata = _dict(row["metadata_json"])
    persona = _dict(row["persona_json"])
    explicit = _dict(metadata.get("explicit_face_constraints"))
    return {
        "participant_id": str(row["participant_id"]),
        "display_name": _clean(row["display_name"]) or "Speaker",
        "gender_presentation": _clean(
            explicit.get("gender")
            or explicit.get("gender_presentation")
            or persona.get("gender_presentation")
            or persona.get("gender")
        ).lower() or None,
        "age_presentation": _clean(
            explicit.get("age")
            or persona.get("age_presentation")
            or persona.get("age")
        ) or None,
        "country_code": _clean(
            explicit.get("country_code")
            or persona.get("country_code")
        ).upper() or None,
        "region_code": _clean(
            explicit.get("region_code")
            or persona.get("region_code")
        ) or None,
    }


def _approved_people(workflow_meta: dict[str, Any], live_speakers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshot = workflow_meta.get("shared_scene_people_snapshot")
    return [dict(item) for item in snapshot] if isinstance(snapshot, list) and snapshot else list(live_speakers)


def _group_photo_generation_input(
    *,
    workflow_meta: dict[str, Any],
    live_speakers: list[dict[str, Any]],
) -> dict[str, Any] | None:
    spec = _dict(workflow_meta.get("shared_scene_group_photo_spec"))
    if not spec:
        return None

    speakers = _approved_people(workflow_meta, live_speakers)
    if len(speakers) != 2:
        return None

    participant_lines: list[str] = []
    subjects: list[dict[str, Any]] = []
    for index, speaker in enumerate(speakers, start=1):
        parts = [
            f"AUTHORITATIVE Speaker {index} — {_clean(speaker.get('display_name')) or 'Speaker'}",
            f"gender presentation: {_clean(speaker.get('gender_presentation')).lower()}" if _clean(speaker.get("gender_presentation")) else "",
            f"age: {_clean(speaker.get('age_presentation'))}" if _clean(speaker.get("age_presentation")) else "",
            f"country code: {_clean(speaker.get('country_code')).upper()}" if _clean(speaker.get("country_code")) else "",
            f"region code: {_clean(speaker.get('region_code'))}" if _clean(speaker.get("region_code")) else "",
        ]
        participant_lines.append(", ".join(item for item in parts if item))
        subjects.append({
            "gender": _clean(speaker.get("gender_presentation")).lower() or None,
            "relationship_role": "conversation participant",
        })

    setting_lines = [
        f"Scene country code: {_clean(spec.get('scene_country_code')).upper()}" if _clean(spec.get("scene_country_code")) else "",
        f"Scene region code: {_clean(spec.get('scene_region_code'))}" if _clean(spec.get("scene_region_code")) else "",
        f"Scene context code: {_clean(spec.get('context_code'))}" if _clean(spec.get("context_code")) else "",
        f"Background/environment: {_clean(spec.get('background'))}" if _clean(spec.get("background")) else "",
    ]

    # svc-face CreatorPlatformRequest.user_prompt is capped at 1500 chars.
    # Keep authoritative approved profiles + scene controls intact and trim only
    # the editable Director description so Director can never emit a payload that
    # Face rejects during pricing.
    fixed_prompt_parts = [
        "CURRENT APPROVED SPEAKER PROFILES — these override any conflicting text in the editable description:",
        *participant_lines,
        *setting_lines,
    ]
    fixed_prompt = "\n".join(item for item in fixed_prompt_parts if item)
    editable_prompt = _clean(spec.get("prompt"))
    max_face_prompt_chars = 1500
    remaining = max(0, max_face_prompt_chars - len(fixed_prompt) - (1 if fixed_prompt and editable_prompt else 0))
    if len(editable_prompt) > remaining:
        editable_prompt = editable_prompt[:remaining].rstrip()
    enriched_prompt = "\n".join(
        item for item in [fixed_prompt, editable_prompt] if item
    )
    if not enriched_prompt:
        raise HTTPException(status_code=422, detail="shared_scene_group_photo_prompt_empty")
    if len(enriched_prompt) > max_face_prompt_chars:
        # This would mean the authoritative fixed contract itself exceeded Face's
        # schema and must be corrected server-side rather than pushed to clients.
        raise HTTPException(status_code=500, detail="shared_scene_group_photo_face_prompt_contract_exceeded")

    return {
        "mode": "text-to-image",
        "language": "en",
        "user_prompt": enriched_prompt,
        "prompt": enriched_prompt,
        "subject_composition_code": "two_people",
        "subjects": subjects,
        "region_code": _clean(spec.get("scene_region_code")) or None,
        "context_code": _clean(spec.get("context_code")) or None,
        "aspect_ratio": "16:9",
        "num_variants": int(spec.get("variant_count") or 2),
    }


async def load_shared_scene_state(conn, *, workflow_id: UUID, account_id: UUID) -> dict[str, Any]:
    workflow = await conn.fetchrow(
        """
        select workflow_id,story_id,project_id,state,current_stage,final_media_id,metadata_json,updated_at
        from public.v3_studio_workflows
        where workflow_id=$1 and account_id=$2
        """,
        workflow_id,
        account_id,
    )
    if not workflow:
        raise HTTPException(status_code=404, detail="studio_workflow_not_found")

    workflow_meta = _dict(workflow["metadata_json"])
    if _clean(workflow_meta.get("conversation_mode")).lower() != "shared_scene":
        raise HTTPException(status_code=409, detail="shared_scene_state_wrong_workflow_mode")

    speaker_rows = await conn.fetch(
        """
        select distinct on (p.participant_id)
               p.participant_id,p.display_name,p.metadata_json,p.persona_json
        from public.v3_studio_stage_runs s
        join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
        join public.v3_participants p on p.participant_id=dt.speaker_participant_id
        where s.workflow_id=$1
          and s.stage_type='audio'
          and s.scope_type='dialogue_turn'
          and dt.speaker_participant_id is not null
        order by p.participant_id,s.created_at
        """,
        workflow_id,
    )
    speakers = [_profile_from_row(row) for row in speaker_rows]
    speaker_ids = {item["participant_id"] for item in speakers}
    profiles_complete = len(speakers) >= 2 and all(bool(item["gender_presentation"]) for item in speakers)

    fusion_rows = await conn.fetch(
        """
        select stage_run_id,state,scene_id,metadata_json
        from public.v3_studio_stage_runs
        where workflow_id=$1 and stage_type='fusion' and scope_type='scene'
        order by created_at,stage_run_id
        """,
        workflow_id,
    )
    fusion = fusion_rows[0] if fusion_rows else None
    fusion_meta = _dict(fusion["metadata_json"]) if fusion else {}
    targets = _dict(fusion_meta.get("speaker_targets"))
    mapped_count = len([participant_id for participant_id in speaker_ids if participant_id in targets])
    draft_media_id = _clean(
        fusion_meta.get("shared_scene_draft_media_id")
        or fusion_meta.get("shared_scene_media_id")
    ) or None
    approved_media_id = _clean(fusion_meta.get("shared_scene_media_id")) or None
    group_photo_approved = bool(
        approved_media_id
        and speakers
        and mapped_count == len(speakers)
    )

    audio_rows = await conn.fetch(
        """
        select state,count(*)::int as count
        from public.v3_studio_stage_runs
        where workflow_id=$1 and stage_type='audio' and scope_type='dialogue_turn'
        group by state
        """,
        workflow_id,
    )
    audio_counts = {str(row["state"]): int(row["count"]) for row in audio_rows}
    audio_total = sum(audio_counts.values())
    audio_approved = int(audio_counts.get("approved", 0))
    audio_done = audio_total > 0 and audio_approved == audio_total

    people_approved = bool(workflow_meta.get("shared_scene_people_approved"))
    source_mode = _clean(workflow_meta.get("shared_scene_source_mode")).lower() or None
    if source_mode not in {None, "generate", "upload"}:
        source_mode = None

    fusion_state = _clean(fusion["state"]).lower() if fusion else ""
    video_settings_saved = bool(fusion_meta.get("shared_scene_video_settings_version"))
    final_ready = bool(workflow["final_media_id"]) or fusion_state == "approved"

    if not people_approved:
        phase = "people"
        next_action = "complete_and_approve_people"
        allowed_actions = ["save_speaker_profile", "approve_people"] if profiles_complete else ["save_speaker_profile"]
    elif not source_mode:
        phase = "group_photo_source"
        next_action = "choose_group_photo_source"
        allowed_actions = ["choose_upload"]
        if len(speakers) == 2:
            allowed_actions.insert(0, "choose_generate")
    elif not draft_media_id:
        phase = "group_photo_prepare"
        next_action = "generate_group_photo" if source_mode == "generate" else "upload_group_photo"
        allowed_actions = [next_action, "change_group_photo_source"]
    elif mapped_count < len(speakers):
        phase = "group_photo_map"
        next_action = "map_group_photo_speakers"
        allowed_actions = ["map_group_photo_speakers", "replace_group_photo"]
    elif not group_photo_approved:
        phase = "group_photo_approve"
        next_action = "approve_group_photo"
        allowed_actions = ["approve_group_photo", "replace_group_photo"]
    elif not audio_done:
        phase = "audio"
        next_action = "prepare_audio"
        allowed_actions = ["prepare_audio"]
    elif fusion_state != "approved":
        phase = "video"
        next_action = "save_video_direction" if not video_settings_saved else "price_or_generate_video"
        allowed_actions = [next_action]
    else:
        phase = "final"
        next_action = "review_final" if final_ready else "assemble_final"
        allowed_actions = [next_action]

    snapshot = workflow_meta.get("shared_scene_people_snapshot")
    state_version = int(workflow_meta.get("shared_scene_state_version") or 0)

    return {
        "contract_version": _SHARED_SCENE_CONTRACT_VERSION,
        "state_version": state_version,
        "workflow_id": str(workflow["workflow_id"]),
        "story_id": str(workflow["story_id"]) if workflow["story_id"] else None,
        "workflow_state": _clean(workflow["state"]),
        "current_stage": _clean(workflow["current_stage"]),
        "phase": phase,
        "next_action": next_action,
        "allowed_actions": allowed_actions,
        "people": {
            "approved": people_approved,
            "profiles_complete": profiles_complete,
            "speaker_count": len(speakers),
            "speakers": speakers,
            "approved_snapshot": snapshot if isinstance(snapshot, list) else None,
            "snapshot_status": (
                "ready"
                if isinstance(snapshot, list) and snapshot
                else ("legacy_missing" if people_approved else "not_applicable")
            ),
        },
        "group_photo": {
            "source_mode": source_mode,
            "generate_supported": len(speakers) == 2,
            "generation_spec": _dict(workflow_meta.get("shared_scene_group_photo_spec")) or None,
            "generation_input": _group_photo_generation_input(
                workflow_meta=workflow_meta,
                live_speakers=speakers,
            ),
            "draft_media_id": draft_media_id,
            "approved_media_id": approved_media_id,
            "mapped_count": mapped_count,
            "required_mapped_count": len(speakers),
            "approved": group_photo_approved,
        },
        "audio": {
            "approved": audio_approved,
            "total": audio_total,
            "states": audio_counts,
            "complete": audio_done,
        },
        "video": {
            "stage_run_id": str(fusion["stage_run_id"]) if fusion else None,
            "state": fusion_state or None,
            "settings_saved": video_settings_saved,
        },
        "final": {
            "ready": final_ready,
            "media_id": str(workflow["final_media_id"]) if workflow["final_media_id"] else None,
        },
    }


@router.get("/api/director/studio-workflows/{workflow_id}/shared-scene-state")
async def get_shared_scene_state(
    workflow_id: UUID,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    async with request.app.state.business_pool.acquire() as conn:
        return await load_shared_scene_state(
            conn,
            workflow_id=workflow_id,
            account_id=auth.account_id,
        )


@router.put("/api/director/studio-workflows/{workflow_id}/shared-scene-source")
async def set_shared_scene_source_mode(
    workflow_id: UUID,
    body: SharedSceneSourceModeIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    pool = request.app.state.business_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            state = await load_shared_scene_state(
                conn,
                workflow_id=workflow_id,
                account_id=auth.account_id,
            )
            if not state["people"]["approved"]:
                raise HTTPException(status_code=409, detail="shared_scene_source_requires_people_approval")
            current_mode = state["group_photo"]["source_mode"]
            if state["group_photo"]["approved_media_id"]:
                raise HTTPException(status_code=409, detail="shared_scene_source_locked_after_group_photo_approval")
            if (
                state["group_photo"]["draft_media_id"]
                and current_mode
                and current_mode != body.mode
            ):
                raise HTTPException(status_code=409, detail="shared_scene_source_locked_after_photo_selection")
            if body.mode == "generate" and not state["group_photo"]["generate_supported"]:
                raise HTTPException(status_code=422, detail="shared_scene_generate_requires_exactly_two_speakers")

            row = await conn.fetchrow(
                """
                select metadata_json
                from public.v3_studio_workflows
                where workflow_id=$1 and account_id=$2
                for update
                """,
                workflow_id,
                auth.account_id,
            )
            if not row:
                raise HTTPException(status_code=404, detail="studio_workflow_not_found")

            metadata = _dict(row["metadata_json"])
            changed = False

            # Compatibility repair for shared-scene workflows approved before the
            # durable speaker-snapshot contract existed. The first explicit
            # post-approval source command atomically freezes the exact current
            # approved profiles before any group-photo work can continue.
            snapshot = metadata.get("shared_scene_people_snapshot")
            if not (isinstance(snapshot, list) and snapshot):
                if not state["people"]["profiles_complete"]:
                    raise HTTPException(status_code=409, detail="shared_scene_people_snapshot_repair_requires_complete_profiles")
                metadata["shared_scene_people_snapshot"] = sorted(
                    (dict(item) for item in state["people"]["speakers"]),
                    key=lambda item: str(item.get("participant_id") or ""),
                )
                changed = True

            current = _clean(metadata.get("shared_scene_source_mode")).lower()
            if current != body.mode:
                metadata["shared_scene_source_mode"] = body.mode
                changed = True

            if changed:
                metadata["shared_scene_state_version"] = int(metadata.get("shared_scene_state_version") or 0) + 1
                await conn.execute(
                    """
                    update public.v3_studio_workflows
                    set metadata_json=$2::jsonb,updated_at=now()
                    where workflow_id=$1 and account_id=$3
                    """,
                    workflow_id,
                    json.dumps(metadata, ensure_ascii=False),
                    auth.account_id,
                )

        return await load_shared_scene_state(
            conn,
            workflow_id=workflow_id,
            account_id=auth.account_id,
        )


@router.put("/api/director/studio-workflows/{workflow_id}/shared-scene-group-photo-spec")
async def set_shared_scene_group_photo_spec(
    workflow_id: UUID,
    body: SharedSceneGroupPhotoSpecIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    pool = request.app.state.business_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            state = await load_shared_scene_state(
                conn,
                workflow_id=workflow_id,
                account_id=auth.account_id,
            )
            if not state["people"]["approved"]:
                raise HTTPException(status_code=409, detail="shared_scene_group_photo_spec_requires_people_approval")
            if state["group_photo"]["source_mode"] != "generate":
                raise HTTPException(status_code=409, detail="shared_scene_group_photo_spec_requires_generate_source")
            if not state["group_photo"]["generate_supported"]:
                raise HTTPException(status_code=422, detail="shared_scene_generate_requires_exactly_two_speakers")
            if state["group_photo"]["draft_media_id"]:
                raise HTTPException(status_code=409, detail="shared_scene_group_photo_spec_locked_after_photo_selection")

            row = await conn.fetchrow(
                """
                select metadata_json
                from public.v3_studio_workflows
                where workflow_id=$1 and account_id=$2
                for update
                """,
                workflow_id,
                auth.account_id,
            )
            if not row:
                raise HTTPException(status_code=404, detail="studio_workflow_not_found")

            metadata = _dict(row["metadata_json"])
            changed = False

            snapshot = metadata.get("shared_scene_people_snapshot")
            if not (isinstance(snapshot, list) and snapshot):
                if not state["people"]["profiles_complete"]:
                    raise HTTPException(status_code=409, detail="shared_scene_people_snapshot_repair_requires_complete_profiles")
                metadata["shared_scene_people_snapshot"] = sorted(
                    (dict(item) for item in state["people"]["speakers"]),
                    key=lambda item: str(item.get("participant_id") or ""),
                )
                changed = True

            spec = {
                "scene_country_code": _clean(body.scene_country_code).upper() or None,
                "scene_region_code": _clean(body.scene_region_code) or None,
                "context_code": _clean(body.context_code) or None,
                "background": _clean(body.background) or None,
                "prompt": _clean(body.prompt),
                "variant_count": int(body.variant_count),
                "aspect_ratio": "16:9",
            }
            if _dict(metadata.get("shared_scene_group_photo_spec")) != spec:
                metadata["shared_scene_group_photo_spec"] = spec
                changed = True

            if changed:
                metadata["shared_scene_state_version"] = int(metadata.get("shared_scene_state_version") or 0) + 1
                await conn.execute(
                    """
                    update public.v3_studio_workflows
                    set metadata_json=$2::jsonb,updated_at=now()
                    where workflow_id=$1 and account_id=$3
                    """,
                    workflow_id,
                    json.dumps(metadata, ensure_ascii=False),
                    auth.account_id,
                )

        return await load_shared_scene_state(
            conn,
            workflow_id=workflow_id,
            account_id=auth.account_id,
        )


__all__ = [
    "SharedSceneGroupPhotoSpecIn",
    "SharedSceneSourceModeIn",
    "load_shared_scene_state",
    "router",
]
