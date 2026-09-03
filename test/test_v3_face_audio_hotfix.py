from __future__ import annotations

from pathlib import Path

from desifaces_shared.pricing.multi_person import (
    FACE_MULTI_PERSON,
    participant_count,
    select_multi_person_pricing,
)

ROOT = Path(__file__).resolve().parents[1]


def test_face_multi_person_is_premium_per_character_not_cast_multiplied() -> None:
    face2 = select_multi_person_pricing(
        studio="face", participant_count_value=2, natural_units=1
    )
    face5 = select_multi_person_pricing(
        studio="face", participant_count_value=5, natural_units=1
    )

    assert face2 is not None and face5 is not None
    assert face2.sku_code == face5.sku_code == FACE_MULTI_PERSON
    assert face2.billable_units == face5.billable_units == 1
    assert face2.variant_params == face5.variant_params == {"num_edits": "1"}
    assert face2.metadata["participant_scaling"] == "per_character_natural_usage"
    assert face5.metadata["participant_count"] == 5


def test_director_face_pricing_context_overrides_normalized_single_subject_count() -> None:
    payload = {
        "subject_composition_code": "single_person",
        "subjects": [{}],
        "num_variants": 1,
        "pricing_context": {
            "multi_person": True,
            "pricing_scope": "director_participant_identity",
            "participant_count_in_sku": False,
        },
    }

    assert participant_count(payload) == 2


def test_plain_single_person_payload_remains_single_person() -> None:
    payload = {
        "subject_composition_code": "single_person",
        "subjects": [{}],
        "num_variants": 1,
        "pricing_context": {},
    }

    assert participant_count(payload) == 1


def test_director_face_adapter_preserves_single_person_image_but_marks_pricing_multi_person() -> None:
    from app.face_pricing_context_runtime import _director_face_pricing_context

    original = {
        "subject_composition_code": "single_person",
        "num_variants": 1,
        "user_prompt": "identity portrait",
    }
    resolved = _director_face_pricing_context(original)

    assert original.get("pricing_context") is None
    assert resolved["subject_composition_code"] == "single_person"
    assert resolved["pricing_context"]["multi_person"] is True
    assert resolved["pricing_context"]["pricing_scope"] == "director_participant_identity"
    assert resolved["pricing_context"]["participant_count_in_sku"] is False


def test_face_request_contract_preserves_internal_pricing_context() -> None:
    source = (ROOT / "services/svc-face/app/app/domain/models.py").read_text(encoding="utf-8")
    assert 'pricing_context: Dict[str, Any] = Field(default_factory=dict)' in source

    main_source = (ROOT / "services/svc-face/app/app/main.py").read_text(encoding="utf-8")
    assert "install_multi_person_pricing_policy()" in main_source


def test_audio_worker_recovers_stale_running_claims_without_new_job_id() -> None:
    repo_source = (
        ROOT / "services/svc-audio/app/app/repos/tts_jobs_repo.py"
    ).read_text(encoding="utf-8")
    worker_source = (
        ROOT / "services/svc-audio/app/app/workers/audio_worker.py"
    ).read_text(encoding="utf-8")

    assert "async def requeue_stale_running_jobs" in repo_source
    assert "status = 'running'" in repo_source
    assert "attempt_count < $3::int" in repo_source
    assert "'worker_recovery_reason', 'stale_running_lease'" in repo_source
    assert "await self._recover_stale_jobs()" in worker_source
    assert 'DF_AUDIO_WORKER_STALE_SECONDS' in worker_source
    assert 'DF_AUDIO_WORKER_MAX_ATTEMPTS' in worker_source


def test_multi_person_catalog_contains_face_premium_sku() -> None:
    migration = (
        ROOT / "migrations/2026_08_30_multi_person_premium_pricing.sql"
    ).read_text(encoding="utf-8")
    assert "'FACE_MULTI_PERSON'" in migration
    assert "'FACE_EDIT_PREMIUM_RUN'" in migration
    assert "'num_edits', 1.25" in migration
