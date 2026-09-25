from __future__ import annotations

import json
import os
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from .security import DirectorAuthContext, get_director_auth


router = APIRouter()
_PREVIEWABLE_STATES = frozenset({"pending", "ready", "failed", "rejected"})


def _omnihuman_shared_scene_enabled() -> bool:
    """Launch guard for the enhanced-motion provider.

    Natural motion remains available to controlled DEV/beta runs only. The normal
    shared-scene path must not inherit OmniHuman latency unless explicitly enabled.
    """
    return str(os.getenv("DF_OMNIHUMAN_SHARED_SCENE_ENABLED", "0") or "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


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


_DEFAULT_NATURAL_MOTION_PROMPT = (
    "Static medium-wide camera. Natural multi-person conversation. Animate only the active speaker. "
    "Use believable conversational body language with visible but controlled hand gestures, subtle torso shifts, "
    "realistic breathing, gentle head movement, expressive eyes and micro-expressions. Preserve the listener, "
    "identity, clothing, furniture, lighting and background. No camera cuts, no reframing, no exaggerated gestures, "
    "no extra fingers, no body warping, and no background deformation."
)


class SharedSceneVideoSettingsIn(BaseModel):
    motion_mode: Literal["natural_motion", "precise_lipsync"] = "precise_lipsync"
    video_prompt: str | None = Field(default=None, max_length=2400)


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
    "/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/shared-scene-video-settings"
)
async def set_shared_scene_video_settings(
    workflow_id: UUID,
    stage_run_id: UUID,
    body: SharedSceneVideoSettingsIn,
    request: Request,
    auth: DirectorAuthContext = Depends(get_director_auth),
):
    pool = request.app.state.business_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            stage = await conn.fetchrow(
                """
                select s.stage_run_id,s.state,s.metadata_json
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
                raise HTTPException(status_code=409, detail="shared_scene_video_settings_require_shared_scene")
            if not str(metadata.get("shared_scene_media_id") or "").strip():
                raise HTTPException(status_code=409, detail="shared_scene_video_settings_require_group_photo")
            if not isinstance(metadata.get("speaker_targets"), dict) or len(metadata["speaker_targets"]) < 2:
                raise HTTPException(status_code=409, detail="shared_scene_video_settings_require_speaker_mapping")

            motion_mode = body.motion_mode
            if motion_mode == "natural_motion" and not _omnihuman_shared_scene_enabled():
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "natural_motion_temporarily_unavailable",
                        "message": "Natural motion is temporarily unavailable while desifaces improves render performance. Use Precise lip-sync.",
                        "recoverable": True,
                        "action": "choose_precise_lipsync",
                    },
                )
            provider = "omnihuman_v15" if motion_mode == "natural_motion" else "sync3"
            prompt = str(body.video_prompt or "").strip()
            if motion_mode == "natural_motion" and not prompt:
                prompt = _DEFAULT_NATURAL_MOTION_PROMPT
            if motion_mode == "precise_lipsync":
                prompt = ""

            metadata["shared_scene_video_settings_version"] = 1
            metadata["shared_scene_motion_mode"] = motion_mode
            metadata["shared_scene_video_provider"] = provider
            metadata["shared_scene_video_prompt"] = prompt

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
        "motion_mode": motion_mode,
        "provider": provider,
        "video_prompt": prompt,
        "persisted": True,
        "pricing_must_refresh": True,
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
                  and (account_id=$3 or account_id is null)
                  and (project_id is null or project_id=$4)
                  and kind in ('image','face_image','face_source_image','source_image')
                  and lifecycle_state='active'
                """,
                body.shared_scene_media_id,
                auth.user_id,
                auth.account_id,
                stage["project_id"],
            )
            if not media:
                raise HTTPException(
                    status_code=422,
                    detail="shared_scene_media_not_owned_active_image",
                )

            # Face Studio's legacy MediaAssetsRepo writes canonical user ownership
            # but does not populate the V3 account/project lineage columns. When a
            # same-user active Face asset is explicitly selected for this workflow,
            # adopt the missing lineage here before persisting the shared-scene
            # contract. Assets already scoped to another account/project remain
            # rejected by the query above.
            if media["account_id"] is None:
                await conn.execute(
                    """
                    update public.media_assets
                    set account_id=$2,
                        project_id=coalesce(project_id,$3),
                        updated_at=now()
                    where id=$1 and user_id=$4 and account_id is null
                    """,
                    body.shared_scene_media_id,
                    auth.account_id,
                    stage["project_id"],
                    auth.user_id,
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
        "generation_ready": True,
        "fusion_provider": "sync3",
    }


__all__ = [
    "NormalizedSpeakerBox",
    "NormalizedSpeakerPoint",
    "SharedSceneConversationIn",
    "SharedSceneVideoSettingsIn",
    "SharedSceneSpeakerTarget",
    "router",
]
