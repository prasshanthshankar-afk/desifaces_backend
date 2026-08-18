from __future__ import annotations

import inspect
from uuid import uuid4

from df_contracts.v3.director import PlannedParticipant


def test_face_compiler_uses_approved_visual_direction_and_explicit_gender_only():
    from app.participant_face import compile_participant_face_studio_input

    participant = PlannedParticipant(
        display_name="Ananya",
        role="daughter",
        persona={"temperament": "thoughtful", "email": "must-not-leak@example.com"},
        continuity={"hair": "shoulder-length dark hair"},
        visual_direction={"expression": "warm confident expression", "lighting": "soft window light"},
    )
    payload = compile_participant_face_studio_input(
        participant=participant,
        participant_hint={"gender": "female", "age": 35},
    )

    assert payload["subject_composition_code"] == "single_person"
    assert payload["num_variants"] == 1
    assert payload["gender"] == "female"
    assert "Ananya" in payload["user_prompt"]
    assert "warm confident expression" in payload["user_prompt"]
    assert "shoulder-length dark hair" in payload["user_prompt"]
    assert "must-not-leak@example.com" not in payload["user_prompt"]
    assert len(payload["user_prompt"]) <= 1500


def test_face_compiler_does_not_infer_gender_from_name_role_or_locale():
    from app.participant_face import compile_participant_face_studio_input

    participant = PlannedParticipant(
        display_name="Ravi",
        role="father",
        preferred_locale="en-IN",
        persona={},
        continuity={},
        visual_direction={"portrait": "natural reference portrait"},
    )
    payload = compile_participant_face_studio_input(
        participant=participant,
        participant_hint={"city": "Chennai"},
    )

    assert "gender" not in payload
    assert "Chennai" not in payload["user_prompt"]
    assert "Do not infer ethnicity" in payload["user_prompt"]


def test_sensitive_key_filter_drops_ids_tokens_and_billing_data():
    from app.participant_face import compile_participant_face_studio_input

    participant = PlannedParticipant(
        display_name="Character A",
        persona={
            "account_id": str(uuid4()),
            "api_token": "secret-token",
            "billing_status": "pro",
            "expression": "calm",
        },
        continuity={},
        visual_direction={},
    )
    payload = compile_participant_face_studio_input(participant=participant)
    prompt = payload["user_prompt"]

    assert "secret-token" not in prompt
    assert "billing_status" not in prompt
    assert "account_id" not in prompt
    assert "expression: calm" in prompt


def test_generated_face_is_candidate_until_hitl_approval():
    from app.participant_face import ParticipantFaceBinder, promote_approved_face_candidate

    bind_source = inspect.getsource(ParticipantFaceBinder.bind_generated_face)
    promote_source = inspect.getsource(promote_approved_face_candidate)

    assert "'reference_face'" in bind_source
    assert 'output_role="face_candidate"' in bind_source
    assert "primary_face_media_id" not in bind_source

    assert "face_promotion_requires_approved_stage" in promote_source
    assert "r.decision='approved'" in promote_source
    assert "o.is_active=true" in promote_source
    assert "'primary_face'" in promote_source
    assert "primary_face_media_id" in promote_source
