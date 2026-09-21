from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from .security import DirectorAuthContext, get_director_auth


router = APIRouter()
_PREVIEWABLE_STATES = frozenset({"pending", "ready", "failed", "rejected"})


class NormalizedSpeakerBox(BaseModel):
    """Normalized shared-scene target rectangle.

    Coordinates are relative to the original shared scene image and intentionally
    provider-neutral. Fusion Extension may add padding when preparing a provider
    source, but the durable target always identifies the same participant.
    """

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)
    padding_ratio: float = Field(default=0.10, ge=0.0, le=0.30)

    @model_validator(mode="after")
    def inside_canvas(self):
        if self.x + self.width > 1.000001:
            raise ValueError("speaker_box_exceeds_canvas_width")
        if self.y + self.height > 1.000001:
            raise ValueError("speaker_box_exceeds_canvas_height")
        return self


class SharedSceneSpeakerTarget(BaseModel):
    participant_id: UUID
    box: NormalizedSpeakerBox


class SharedSceneConversationIn(BaseModel):
    shared_scene_media_id: UUID
    speaker_targets: Annotated[list[SharedSceneSpeakerTarget], Field(min_length=2, max_length=20)]

    @model_validator(mode="after")
    def unique_speakers(self):
        ids = [item.participant_id for item in self.speaker_targets]
        if len(ids) != len(set(ids)):
            raise ValueError("shared_scene_speaker_targets_must_be_unique")
        return self


def _metadata(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value or {})
    except Exception:
        return {}


@router.put(
    "/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/shared-scene"
)
async def set_shared_scene_conversation(
    workflow_id: UUID,
    stage_run_id: UUID,
    body: SharedSceneConversationIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    """Lock a Fusion scene to one shared image and explicit speaker targets.

    This is an additive control-plane contract. Existing single-person and
    ordered single-speaker-shot Fusion behavior is unchanged unless
    conversation_mode=shared_scene is explicitly persisted here.

    Shared-scene mode intentionally fails closed: every speech participant must
    have exactly one target rectangle before pricing or generation is allowed.
    """

    pool = request.app.state.business_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            stage = await conn.fetchrow(
                """
                select s.stage_run_id,s.state,s.scene_id,s.metadata_json,
                       w.project_id,w.owner_user_id
                from public.v3_studio_stage_runs s
                join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
                where s.stage_run_id=$1 and s.workflow_id=$2 and w.account_id=$3
                  and s.stage_type='fusion' and s.scope_type='scene'
                for update of s
                """,
                stage_run_id,
                workflow_id,
                auth.account_id,
            )
            if not stage:
                raise HTTPException(status_code=404, detail="fusion_scene_stage_not_found")

            state = str(stage["state"] or "").strip().lower()
            if state not in _PREVIEWABLE_STATES:
                raise HTTPException(
                    status_code=409,
                    detail=f"shared_scene_locked_for_stage:{state}",
                )

            media = await conn.fetchrow(
                """
                select id,user_id,account_id,project_id,kind,lifecycle_state
                from public.media_assets
                where id=$1 and user_id=$2 and account_id=$3
                  and project_id=$4 and kind='image' and lifecycle_state='active'
                """,
                body.shared_scene_media_id,
                auth.user_id,
                auth.account_id,
                stage["project_id"],
            )
            if not media:
                raise HTTPException(
                    status_code=422,
                    detail="shared_scene_media_not_owned_active_project_image",
                )

            speech_rows = await conn.fetch(
                """
                select distinct speaker_participant_id
                from public.v3_dialogue_turns
                where scene_id=$1 and turn_kind='speech'
                  and speaker_participant_id is not null
                order by speaker_participant_id
                """,
                stage["scene_id"],
            )
            speaking_ids = {
                UUID(str(row["speaker_participant_id"]))
                for row in speech_rows
                if row["speaker_participant_id"] is not None
            }
            if len(speaking_ids) < 2:
                raise HTTPException(
                    status_code=422,
                    detail="shared_scene_requires_at_least_two_speakers",
                )

            member_rows = await conn.fetch(
                """
                select participant_id
                from public.v3_scene_participants
                where scene_id=$1
                """,
                stage["scene_id"],
            )
            member_ids = {UUID(str(row["participant_id"])) for row in member_rows}
            target_ids = {item.participant_id for item in body.speaker_targets}

            if not target_ids.issubset(member_ids):
                raise HTTPException(
                    status_code=422,
                    detail="shared_scene_target_not_scene_participant",
                )
            if target_ids != speaking_ids:
                missing = sorted(str(value) for value in (speaking_ids - target_ids))
                extra = sorted(str(value) for value in (target_ids - speaking_ids))
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "shared_scene_speaker_target_mismatch",
                        "missing_speaker_participant_ids": missing,
                        "unexpected_participant_ids": extra,
                    },
                )

            metadata = _metadata(stage["metadata_json"])
            metadata["conversation_mode"] = "shared_scene"
            metadata["shared_scene_contract_version"] = 1
            metadata["shared_scene_media_id"] = str(body.shared_scene_media_id)
            metadata["speaker_targets"] = {
                str(item.participant_id): item.box.model_dump(mode="json")
                for item in body.speaker_targets
            }
            metadata["shared_scene_target_source"] = "user_confirmed"

            await conn.execute(
                """
                update public.v3_studio_stage_runs
                set metadata_json=$2::jsonb,updated_at=now()
                where stage_run_id=$1
                """,
                stage_run_id,
                json.dumps(metadata, ensure_ascii=False),
            )

    return {
        "workflow_id": str(workflow_id),
        "stage_run_id": str(stage_run_id),
        "scene_id": str(stage["scene_id"]),
        "conversation_mode": "shared_scene",
        "shared_scene_media_id": str(body.shared_scene_media_id),
        "speaker_count": len(body.speaker_targets),
        "speaker_targets": {
            str(item.participant_id): item.box.model_dump(mode="json")
            for item in body.speaker_targets
        },
        "persisted": True,
        "configuration_ready": True,
        "generation_ready": False,
        "generation_blocker": "shared_scene_media_pipeline_required",
    }


__all__ = [
    "NormalizedSpeakerBox",
    "SharedSceneConversationIn",
    "SharedSceneSpeakerTarget",
    "router",
]
