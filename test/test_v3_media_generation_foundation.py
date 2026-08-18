from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from df_contracts.v3.domain import (
    EntityState,
    GenerationJob,
    GenerationKind,
    GenerationRequest,
    JobState,
    MediaAsset,
    MediaKind,
    MediaRole,
    SafetyState,
)
from desifaces_shared.v3.generation_store import (
    InvalidJobTransition,
    validate_job_transition,
)
from desifaces_shared.v3.media_store import (
    normalize_media_kind,
    normalize_media_role,
)


def now():
    return datetime.now(timezone.utc)


def test_media_asset_contract_has_explicit_lifecycle_and_lineage() -> None:
    account_id = uuid4()
    user_id = uuid4()
    source_id = uuid4()
    job_id = uuid4()
    asset = MediaAsset(
        account_id=account_id,
        owner_user_id=user_id,
        kind=MediaKind.VIDEO,
        role=MediaRole.FINAL,
        lifecycle_state=EntityState.ACTIVE,
        mime_type="video/mp4",
        storage_uri="az://video-output-v3/final/example.mp4",
        sha256="a" * 64,
        size_bytes=1234,
        duration_ms=5000,
        source_media_ids=(source_id,),
        parent_job_id=job_id,
        created_at=now(),
    )

    assert asset.account_id == account_id
    assert asset.role is MediaRole.FINAL
    assert asset.lifecycle_state is EntityState.ACTIVE
    assert asset.source_media_ids == (source_id,)
    assert asset.parent_job_id == job_id
    assert asset.storage_uri.startswith("az://")


def test_media_kind_and_role_compatibility_normalization() -> None:
    assert normalize_media_kind("face_image", "image/jpeg") is MediaKind.IMAGE
    assert normalize_media_kind("audio_master", "audio/mpeg") is MediaKind.AUDIO
    assert normalize_media_kind("anything", "video/mp4") is MediaKind.VIDEO
    assert normalize_media_role(None, kind="upload") is MediaRole.SOURCE
    assert normalize_media_role(None, kind="thumbnail") is MediaRole.THUMBNAIL
    assert normalize_media_role(None, kind="video") is MediaRole.FINAL
    assert normalize_media_role(None, kind="working_render") is MediaRole.INTERMEDIATE


def test_generation_request_supports_participants_and_media() -> None:
    request = GenerationRequest(
        account_id=uuid4(),
        requested_by_user_id=uuid4(),
        kind=GenerationKind.FUSION,
        participant_ids=(uuid4(), uuid4(), uuid4()),
        source_media_ids=(uuid4(), uuid4()),
        parameters={"scene": {"active_speaker": 1}},
        safety_state=SafetyState.ALLOWED,
        created_at=now(),
    )
    assert len(request.participant_ids) == 3
    assert len(request.source_media_ids) == 2
    assert request.kind is GenerationKind.FUSION


def test_generation_job_contract_supports_parent_child_execution() -> None:
    parent = uuid4()
    job = GenerationJob(
        generation_id=uuid4(),
        parent_job_id=parent,
        job_type="participant_render",
        state=JobState.QUEUED,
        attempt_count=1,
        max_attempts=3,
        created_at=now(),
        updated_at=now(),
    )
    assert job.parent_job_id == parent
    assert job.job_type == "participant_render"
    assert job.attempt_count == 1


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (JobState.SUBMITTED, JobState.QUEUED),
        (JobState.SUBMITTED, JobState.RUNNING),
        (JobState.QUEUED, JobState.RUNNING),
        (JobState.RUNNING, JobState.SUCCEEDED),
        (JobState.RUNNING, JobState.QUEUED),
        (JobState.BLOCKED, JobState.QUEUED),
    ],
)
def test_generation_state_machine_accepts_allowed_transitions(old: JobState, new: JobState) -> None:
    assert validate_job_transition(old, new) == (old, new)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (JobState.SUCCEEDED, JobState.RUNNING),
        (JobState.FAILED, JobState.QUEUED),
        (JobState.CANCELED, JobState.RUNNING),
        (JobState.EXPIRED, JobState.QUEUED),
        (JobState.QUEUED, JobState.SUCCEEDED),
    ],
)
def test_generation_state_machine_rejects_terminal_resurrection_and_illegal_skips(old: JobState, new: JobState) -> None:
    with pytest.raises(InvalidJobTransition):
        validate_job_transition(old, new)
