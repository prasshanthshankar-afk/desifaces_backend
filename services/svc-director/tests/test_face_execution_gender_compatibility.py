from uuid import uuid4

from df_contracts.v3.director import PlannedParticipant

from app.face_execution import (
    FaceStageContext,
    compile_context_face_input,
)


def _context(persona):
    participant_id = uuid4()

    participant = PlannedParticipant(
        participant_id=participant_id,
        kind="person",
        display_name="Claire",
        role="character",
        persona=persona,
        continuity={},
        preferred_locale="en-US",
        visual_direction={},
        voice_direction={},
    )

    return FaceStageContext(
        workflow_id=uuid4(),
        stage_run_id=uuid4(),
        participant_id=participant_id,
        display_name="Claire",
        planned_participant=participant,
        stage_state="pending",
        metadata={},
        participant_metadata={},
    )


def test_face_input_recovers_explicit_persona_gender():
    result = compile_context_face_input(
        _context({"gender": "female"})
    )

    assert result["gender"] == "female"


def test_face_input_preserves_gender_presentation_compatibility():
    result = compile_context_face_input(
        _context({"gender_presentation": "woman"})
    )

    assert result["gender"] == "female"
