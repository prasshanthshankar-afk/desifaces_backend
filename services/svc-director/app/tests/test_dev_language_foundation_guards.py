"""Foundation guardrails: source/translation isolation and Audio locale authority."""
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]

class FoundationContracts(unittest.TestCase):
    def test_source_is_not_replaced_with_hindi(self):
        source=(ROOT/"app/experience_compiler.py").read_text()
        compile(source,"experience_compiler.py","exec")
        for code in ("canonical_source_must_be_english",
                     "director_source_contains_hindi_not_english",
                     "hindi_hitl_approval_required_before_story_creation"):
            self.assertIn(code,source)
        self.assertLess(source.index("hindi_hitl_approval_required_before_story_creation"),
                        source.index("graph = await super().compile("))
    def test_audio_approved_locale_is_authoritative(self):
        audio=(ROOT/"app/audio_execution.py").read_text()
        compile(audio,"audio_execution.py","exec")
        self.assertIn('settings["target_locale"] = canonical_approved.get("target_locale")',audio)
        self.assertIn('settings["translate"] = False',audio)
        self.assertIn('approved_localization_snapshot_mismatch',audio)
        self.assertIn('approved_text if use_approved_localization else context.text',audio)

if __name__ == "__main__": unittest.main()
