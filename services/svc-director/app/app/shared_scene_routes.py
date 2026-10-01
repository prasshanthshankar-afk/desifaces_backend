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


class NormalizedSpeakerPoint(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class SharedSceneSpeakerTarget(BaseModel):
    participant_id: UUID
    point: NormalizedSpeakerPoint | None = None
    box: NormalizedSpeakerBox | None = None

    @model_validator(mode="after")
    def exactly_one_target(self):
        if (self.point is None) == (self.box is None):
            raise ValueError("shared_scene_speaker_requires_exactly_one_point_or_box")
        return self


class SharedSceneVideoSettingsIn(BaseModel):
    motion_mode: str = Field(min_length=1, max_length=64)
    video_prompt: str | None = Field(default=None, max_length=2400)

    @model_validator(mode="after")
    def validate_motion_mode(self):
        mode = str(self.motion_mode or "").strip().lower()
        if mode not in {"precise_lipsync", "natural_motion"}:
            raise ValueError("unsupported_shared_scene_motion_mode")
        self.motion_mode = mode
        prompt = str(self.video_prompt or "").strip()
        if mode == "natural_motion" and not prompt:
            raise ValueError("natural_motion_video_prompt_required")
        self.video_prompt = prompt or None
        return self


class SharedSceneDraftIn(BaseModel):
    shared_scene_media_id: UUID
    image_width: int | None = Field(default=None, ge=64, le=16384)
    image_height: int | None = Field(default=None, ge=64, le=16384)
    speaker_targets: list[SharedSceneSpeakerTarget] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_dimensions_and_unique_speakers(self):
        if (self.image_width is None) != (self.image_height is None):
            raise ValueError("shared_scene_draft_dimensions_must_be_complete")
        ids = [item.participant_id for item in self.speaker_targets]
        if len(ids) != len(set(ids)):
            raise ValueError("shared_scene_speaker_targets_must_be_unique")
        return self


class SharedSceneConversationIn(BaseModel):
    shared_scene_media_id: UUID
    image_width: int = Field(ge=64, le=16384)
    image_height: int = Field(ge=64, le=16384)
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
    "/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/shared-scene-draft"
)
async def set_shared_scene_draft(
    workflow_id: UUID,
    stage_run_id: UUID,
    body: SharedSceneDraftIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    """Persist the selected group-photo draft and partial speaker mapping.

    A draft never grants Fusion generation authority. Only the final shared-scene
    approval route may set shared_scene_media_id and approved input lineage.
    """

    pool = request.app.state.business_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            stage = await conn.fetchrow(
                """
                select s.stage_run_id,s.state,s.scene_id,s.metadata_json,
                       w.project_id,w.metadata_json as workflow_metadata
                from public.v3_studio_stage_runs s
                join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
                where s.stage_run_id=$1 and s.workflow_id=$2 and w.account_id=$3
                  and s.stage_type='fusion' and s.scope_type='scene'
                for update of s,w
                """,
                stage_run_id,
                workflow_id,
                auth.account_id,
            )
            if not stage:
                raise HTTPException(status_code=404, detail="fusion_scene_stage_not_found")

            state = str(stage["state"] or "").strip().lower()
            if state not in _PREVIEWABLE_STATES:
                raise HTTPException(status_code=409, detail=f"shared_scene_draft_locked_for_stage:{state}")

            media = await conn.fetchrow(
                """
                select id,user_id,account_id,project_id,kind,lifecycle_state,meta_json
                from public.media_assets
                where id=$1 and user_id=$2
                  and (account_id is null or account_id=$3)
                  and (project_id is null or project_id=$4)
                  and kind in ('image','source_image','face_image','face_source_image')
                  and lifecycle_state='active'
                for update
                """,
                body.shared_scene_media_id,
                auth.user_id,
                auth.account_id,
                stage["project_id"],
            )
            if not media:
                raise HTTPException(status_code=422, detail="shared_scene_media_not_owned_active_face_image")

            validation = _metadata(_metadata(media["meta_json"]).get("shared_scene_validation"))
            if not validation or not bool(validation.get("allow")) or str(validation.get("status") or "").upper() == "FAIL":
                raise HTTPException(status_code=422, detail="shared_scene_media_validation_required")

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
                raise HTTPException(status_code=422, detail="shared_scene_target_not_scene_participant")

            metadata = _metadata(stage["metadata_json"])
            metadata["conversation_mode"] = "shared_scene"
            metadata["shared_scene_contract_version"] = 1
            metadata["shared_scene_draft_media_id"] = str(body.shared_scene_media_id)
            if body.image_width is not None and body.image_height is not None:
                metadata["shared_scene_dimensions"] = {
                    "width": body.image_width,
                    "height": body.image_height,
                }
            metadata["speaker_targets"] = {
                str(item.participant_id): (
                    {"point": item.point.model_dump(mode="json")}
                    if item.point is not None
                    else {"box": item.box.model_dump(mode="json")}
                )
                for item in body.speaker_targets
            }
            metadata["shared_scene_target_source"] = "user_draft"

            await conn.execute(
                """
                update public.v3_studio_stage_runs
                set metadata_json=$2::jsonb,updated_at=now()
                where stage_run_id=$1
                """,
                stage_run_id,
                json.dumps(metadata, ensure_ascii=False),
            )

            workflow_metadata = _metadata(stage["workflow_metadata"])
            workflow_metadata["shared_scene_state_version"] = int(
                workflow_metadata.get("shared_scene_state_version") or 0
            ) + 1
            await conn.execute(
                """
                update public.v3_studio_workflows
                set metadata_json=$2::jsonb,updated_at=now()
                where workflow_id=$1 and account_id=$3
                """,
                workflow_id,
                json.dumps(workflow_metadata, ensure_ascii=False),
                auth.account_id,
            )

    return {
        "workflow_id": str(workflow_id),
        "stage_run_id": str(stage_run_id),
        "shared_scene_draft_media_id": str(body.shared_scene_media_id),
        "mapped_speaker_count": len(body.speaker_targets),
        "persisted": True,
        "approved": False,
    }


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
                select id,user_id,account_id,project_id,kind,lifecycle_state,meta_json
                from public.media_assets
                where id=$1 and user_id=$2
                  and (account_id is null or account_id=$3)
                  and (project_id is null or project_id=$4)
                  and kind in ('image','source_image','face_image','face_source_image')
                  and lifecycle_state='active'
                for update
                """,
                body.shared_scene_media_id,
                auth.user_id,
                auth.account_id,
                stage["project_id"],
            )
            if not media:
                raise HTTPException(
                    status_code=422,
                    detail="shared_scene_media_not_owned_active_face_image",
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

            media_meta = _metadata(media["meta_json"])
            validation = _metadata(media_meta.get("shared_scene_validation"))
            if not validation:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "shared_scene_media_validation_required",
                        "message": "This group photo must pass content-safety and quality checks before it can be used.",
                        "recoverable": True,
                        "action": "validate_group_photo",
                    },
                )
            if not bool(validation.get("allow")) or str(validation.get("status") or "").upper() == "FAIL":
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "shared_scene_media_validation_failed",
                        "message": str(validation.get("summary") or "This group photo did not pass the required safety and quality checks."),
                        "recoverable": True,
                        "action": "choose_or_create_another_group_photo",
                    },
                )
            if int(validation.get("expected_speakers") or 0) != len(speaking_ids):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "shared_scene_media_speaker_count_validation_mismatch",
                        "message": "The photo validation no longer matches the number of speakers in this conversation. Validate the photo again.",
                        "recoverable": True,
                        "action": "validate_group_photo",
                    },
                )

            # Legacy/generated Face assets created before account/project lineage
            # propagation may be user-owned and fully validated while these two
            # columns are NULL. Adopt only missing lineage here, where the
            # authenticated account and workflow project are authoritative.
            # Existing non-NULL lineage is never overwritten; mismatches are
            # rejected by the SELECT predicate above.
            await conn.execute(
                """
                update public.media_assets
                set account_id=coalesce(account_id,$2),
                    project_id=coalesce(project_id,$3),
                    updated_at=now()
                where id=$1
                """,
                body.shared_scene_media_id,
                auth.account_id,
                stage["project_id"],
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
            metadata["shared_scene_dimensions"] = {"width": body.image_width, "height": body.image_height}
            metadata["speaker_targets"] = {
                str(item.participant_id): (
                    {"point": item.point.model_dump(mode="json")}
                    if item.point is not None
                    else {"box": item.box.model_dump(mode="json")}
                )
                for item in body.speaker_targets
            }
            metadata["shared_scene_target_source"] = "user_confirmed"
            metadata["shared_scene_validation"] = {
                "status": str(validation.get("status") or "PASS").upper(),
                "expected_speakers": int(validation.get("expected_speakers") or len(speaking_ids)),
                "validated_at": validation.get("validated_at"),
                "contract_version": int(validation.get("contract_version") or 1),
            }

            await conn.execute(
                """
                update public.v3_studio_stage_runs
                set metadata_json=$2::jsonb,updated_at=now()
                where stage_run_id=$1
                """,
                stage_run_id,
                json.dumps(metadata, ensure_ascii=False),
            )

            # Persist the authoritative group photo as canonical Fusion input
            # lineage. The media itself is not produced by another Studio stage,
            # therefore source_stage_run_id is intentionally NULL.
            await conn.execute(
                """
                insert into public.v3_studio_stage_inputs(
                    stage_run_id,media_id,input_role,source_stage_run_id
                )
                values($1,$2,'approved_shared_scene_image',null)
                on conflict(stage_run_id,media_id,input_role) do nothing
                """,
                stage_run_id,
                body.shared_scene_media_id,
            )

    return {
        "workflow_id": str(workflow_id),
        "stage_run_id": str(stage_run_id),
        "scene_id": str(stage["scene_id"]),
        "conversation_mode": "shared_scene",
        "shared_scene_media_id": str(body.shared_scene_media_id),
        "shared_scene_dimensions": {"width": body.image_width, "height": body.image_height},
        "speaker_count": len(body.speaker_targets),
        "speaker_targets": {
            str(item.participant_id): (
                {"point": item.point.model_dump(mode="json")}
                if item.point is not None
                else {"box": item.box.model_dump(mode="json")}
            )
            for item in body.speaker_targets
        },
        "persisted": True,
        "configuration_ready": True,
        "generation_ready": len(body.speaker_targets) == 2,
        "video_supported": len(body.speaker_targets) == 2,
        "video_max_people": 2,
        "fusion_provider": "sync3",
    }


@router.put(
    "/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/shared-scene-video-settings"
)
async def set_shared_scene_video_settings(
    workflow_id: UUID,
    stage_run_id: UUID,
    body: SharedSceneVideoSettingsIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    """Persist the explicit motion choice that gates shared-scene video pricing.

    This route is control-plane only. It does not generate media or charge the
    account. Settings may change only while the Fusion scene is still in a
    previewable/retryable state.
    """

    pool = request.app.state.business_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            stage = await conn.fetchrow(
                """
                select s.stage_run_id,s.state,s.metadata_json,w.account_id
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
                    detail=f"shared_scene_video_settings_locked_for_stage:{state}",
                )

            metadata = _metadata(stage["metadata_json"])
            if str(metadata.get("conversation_mode") or "").strip().lower() != "shared_scene":
                raise HTTPException(
                    status_code=409,
                    detail="shared_scene_video_settings_wrong_conversation_mode",
                )
            if not str(metadata.get("shared_scene_media_id") or "").strip():
                raise HTTPException(
                    status_code=409,
                    detail="shared_scene_video_settings_requires_approved_group_photo",
                )

            speaker_targets = _metadata(metadata.get("speaker_targets"))
            if len(speaker_targets) != 2:
                raise HTTPException(
                    status_code=422,
                    detail="shared_scene_video_requires_exactly_two_speakers",
                )

            metadata["shared_scene_motion_mode"] = body.motion_mode
            metadata["shared_scene_video_prompt"] = body.video_prompt
            metadata["shared_scene_video_settings_version"] = 1

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
        "motion_mode": body.motion_mode,
        "video_prompt": body.video_prompt,
        "shared_scene_video_settings_version": 1,
        "pricing_ready": True,
        "persisted": True,
    }


__all__ = [
    "NormalizedSpeakerBox",
    "NormalizedSpeakerPoint",
    "SharedSceneConversationIn",
    "SharedSceneDraftIn",
    "SharedSceneVideoSettingsIn",
    "SharedSceneSpeakerTarget",
    "router",
]
