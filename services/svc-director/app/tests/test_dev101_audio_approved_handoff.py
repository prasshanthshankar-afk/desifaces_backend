import unittest
from types import SimpleNamespace
from uuid import uuid4

from app.audio_execution_runtime import (
    compile_context_audio_input,
    ParticipantAudioBridgeError,
)

ORIGINAL = "We are opening our cafe."
APPROVED = "हम अपना कैफ़े खोल रहे हैं।"


def context(
    *,
    target="hi-IN",
    approved=True,
    voice_locale=None,
    valid_snapshot=True,
):
    turn_id = uuid4()
    voice_locale = (
        target if voice_locale is None else voice_locale
    )

    metadata = {
        "conversation_settings": {
            "target_locale": target,
            "source_language": "en-US",
            "translate": True,
        }
    }

    if approved:
        metadata["approved_localization"] = {
            "approved_by_user": True,
            "dialogue_turn_id": (
                str(turn_id) if valid_snapshot else str(uuid4())
            ),
            "source_text": ORIGINAL,
            "target_locale": target,
            "text": APPROVED,
        }

    return SimpleNamespace(
        workflow_id=uuid4(),
        stage_run_id=uuid4(),
        account_id=uuid4(),
        owner_user_id=uuid4(),
        project_id=uuid4(),
        story_id=uuid4(),
        dialogue_turn_id=turn_id,
        scene_id=uuid4(),
        participant_id=uuid4(),
        text=ORIGINAL,
        target_locale="en-US",
        emotion_code=None,
        delivery={},
        participant_persona={"gender": "female"},
        participant_metadata={},
        voice_profile_ref="fixture-voice",
        voice_locale=voice_locale,
        stage_metadata=metadata,
    )


class ApprovedAudioHandoffTests(unittest.TestCase):

    def test_approved_text_is_spoken_without_translation(self):
        result = compile_context_audio_input(context())

        self.assertEqual(result["text"], APPROVED)
        self.assertEqual(result["target_locale"], "hi-IN")
        self.assertEqual(result["source_language"], "hi-IN")
        self.assertIs(result["translate"], False)

    def test_other_locale_uses_same_rule(self):
        result = compile_context_audio_input(
            context(target="fr-CA")
        )
        self.assertEqual(result["text"], APPROVED)
        self.assertEqual(result["target_locale"], "fr-CA")
        self.assertIs(result["translate"], False)

    def test_unapproved_source_preserves_legacy_translation(self):
        result = compile_context_audio_input(
            context(approved=False)
        )
        self.assertEqual(result["text"], ORIGINAL)
        self.assertEqual(result["source_language"], "en-US")
        self.assertIs(result["translate"], True)

    def test_wrong_voice_locale_blocked(self):
        with self.assertRaisesRegex(
            ParticipantAudioBridgeError,
            "approved_localization_voice_locale_mismatch",
        ):
            compile_context_audio_input(
                context(voice_locale="fr-CA")
            )

    def test_stale_turn_snapshot_blocked(self):
        with self.assertRaisesRegex(
            ParticipantAudioBridgeError,
            "approved_localization_snapshot_mismatch",
        ):
            compile_context_audio_input(
                context(valid_snapshot=False)
            )


if __name__ == "__main__":
    unittest.main()
