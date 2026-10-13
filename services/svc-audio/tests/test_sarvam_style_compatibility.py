import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from app.services.tts_intent_policy import relax_native_style_only_for_eligible_sarvam_voice as relax


class SarvamStyleCompatibilityTests(unittest.TestCase):
    def test_only_prevalidated_sarvam_voices_relax_product_intents(self):
        for intent in ('conversational', 'narration', ' Conversational '):
            self.assertTrue(relax(intent, sarvam_voice_eligible=True))
            self.assertFalse(relax(intent, sarvam_voice_eligible=False))

    def test_native_styles_and_empty_style_are_not_relaxed(self):
        for intent in ('angry', 'cheerful', 'character', None, ''):
            self.assertFalse(relax(intent, sarvam_voice_eligible=True))


if __name__ == '__main__':
    unittest.main()
