"""Offline contract tests: no model calls or generation."""
import unittest

from app.services.dev_scene_intent_localization import (
    SceneLocalizationError, validate_scene_draft,
)


class SceneIntentContractTests(unittest.TestCase):
    def setUp(self):
        self.original = [
            {"turn_id": "1", "speaker": "Maya", "text": "I can't wait to serve customers."},
            {"turn_id": "2", "speaker": "Arjun", "text": "That's exciting!"},
        ]

    def test_accepts_reviewable_draft_without_meaning_gate(self):
        result = validate_scene_draft(self.original, {"turns": [
            {"turn_id": "1", "speaker": "Maya", "text": "ग्राहकों की सेवा करने के लिए मैं बहुत उत्साहित हूँ।"},
            {"turn_id": "2", "speaker": "Arjun", "text": "ये तो बहुत अच्छी खबर है!"},
        ]})
        self.assertEqual(len(result), 2)

    def test_missing_turn_fails_structural_validation(self):
        with self.assertRaises(SceneLocalizationError):
            validate_scene_draft(self.original, {"turns": [
                {"turn_id": "1", "speaker": "Maya", "text": "X"}
            ]})

    def test_speaker_swap_fails_structural_validation(self):
        with self.assertRaises(SceneLocalizationError):
            validate_scene_draft(self.original, {"turns": [
                {"turn_id": "1", "speaker": "Arjun", "text": "X"},
                {"turn_id": "2", "speaker": "Maya", "text": "Y"},
            ]})

    def test_no_semantic_ai_reviewer(self):
        # Even awkward dialogue is a human editorial choice.
        result = validate_scene_draft(self.original, {"turns": [
            {"turn_id": "1", "speaker": "Maya", "text": "Needs human edit"},
            {"turn_id": "2", "speaker": "Arjun", "text": "Needs human edit"},
        ]})
        self.assertEqual(result[0]["text"], "Needs human edit")


if __name__ == "__main__":
    unittest.main()
