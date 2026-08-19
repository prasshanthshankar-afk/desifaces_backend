from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from df_contracts.v3.director import CreativeBrief, PlannedParticipant


def test_explicit_face_constraints_are_allowlisted_and_name_scoped():
    from app.compiler import _explicit_face_constraints

    brief = CreativeBrief(
        text="Create two characters",
        participant_hints=(
            {
                "display_name": "Ananya",
                "age": 35,
                "gender": "female",
                "email": "must-not-persist@example.com",
                "billing_account_id": str(uuid4()),
            },
            {"display_name": "Ravi", "age": 65, "gender": "male"},
        ),
    )
    assert _explicit_face_constraints(brief, "Ananya") == {"age": 35, "gender": "female"}
    assert _explicit_face_constraints(brief, "Ravi") == {"age": 65, "gender": "male"}
    assert _explicit_face_constraints(brief, "Unknown") == {}


def test_face_compiler_consumes_only_durable_explicit_constraints():
    from app.face_execution import FaceStageContext, compile_context_face_input

    participant = PlannedParticipant(
        participant_id=uuid4(),
        display_name="Ananya",
        role="daughter",
        preferred_locale="en-IN",
        visual_direction={"expression": "composed", "hair": "shoulder-length dark hair"},
    )
    context = FaceStageContext(
        workflow_id=uuid4(),
        stage_run_id=uuid4(),
        participant_id=participant.participant_id,
        display_name="Ananya",
        planned_participant=participant,
        stage_state="pending",
        metadata={},
        participant_metadata={
            "explicit_face_constraints": {
                "age": 35,
                "gender": "female",
                "email": "ignored@example.com",
            }
        },
    )
    payload = compile_context_face_input(context)
    assert payload["gender"] == "female"
    assert "35" in payload["user_prompt"]
    assert "ignored@example.com" not in payload["user_prompt"]


def test_rejected_successful_attempt_is_never_reactivated(monkeypatch):
    import app.face_execution as module

    stage_run_id = uuid4()
    participant_id = uuid4()
    media_id = uuid4()
    attempt_id = uuid4()
    context = module.FaceStageContext(
        workflow_id=uuid4(),
        stage_run_id=stage_run_id,
        participant_id=participant_id,
        display_name="Ananya",
        planned_participant=PlannedParticipant(
            participant_id=participant_id, display_name="Ananya"
        ),
        stage_state="rejected",
        metadata={"compatibility_face_job_id": "job-old"},
        participant_metadata={},
    )

    async def fake_load(*args, **kwargs):
        return context

    async def fake_latest(*args, **kwargs):
        return {
            "attempt_id": attempt_id,
            "attempt_no": 1,
            "attempt_kind": "initial",
            "state": "succeeded",
            "provider_job_ref": "job-old",
            "media_id": media_id,
        }

    monkeypatch.setattr(module, "load_face_stage_context", fake_load)
    monkeypatch.setattr(module, "_latest_attempt", fake_latest)

    class Conn:
        async def fetchrow(self, *args, **kwargs):
            return None

    class Acquire:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    service = module.ParticipantFaceExecutionService(face_base_url="http://face")

    async def fake_status(*args, **kwargs):
        return {
            "status": "succeeded",
            "variants": [
                {
                    "media_asset_id": str(media_id),
                    "image_url": "https://example.invalid/rejected.png",
                    "face_profile_id": "profile-1",
                    "prompt_used": "prompt",
                }
            ],
        }

    class BinderMustNotRun:
        async def bind_generated_face(self, *args, **kwargs):
            raise AssertionError("rejected successful output must never be rebound")

    service._status_once = fake_status
    service.binder = BinderMustNotRun()

    async def run():
        result = await service.sync(
            Pool(),
            account_id=uuid4(),
            workflow_id=context.workflow_id,
            stage_run_id=stage_run_id,
            headers={"Authorization": "Bearer test"},
        )
        assert result["stage_state"] == "rejected"
        assert result["media_asset_id"] == str(media_id)
        assert result["review_decision"] == "rejected"

    asyncio.run(run())


def test_stage_attempt_migration_has_atomic_retry_guards():
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "2026_08_19_v3_studio_stage_attempts.sql"
    ).read_text(encoding="utf-8")
    assert "UNIQUE(stage_run_id, attempt_no)" in migration
    assert "attempt_kind IN ('initial','retry','regenerate')" in migration
    assert "state IN ('dispatching','queued','running','succeeded','failed','canceled')" in migration
    assert "uq_v3_studio_stage_attempt_provider_job" in migration
