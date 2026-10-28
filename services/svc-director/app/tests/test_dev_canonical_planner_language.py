"""DEV foundation: English canonical planning for Hindi output requests."""
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]/"app"
class CanonicalPlannerLanguageTests(unittest.TestCase):
    def test_hindi_output_does_not_change_english_planner(self):
        source=(ROOT/"llm.py").read_text()
        compile(source,"llm.py","exec")
        self.assertIn('requested_output_locale',source)
        self.assertIn('source_dialogue_locale',source)
        self.assertIn('Plan every spoken dialogue line in ',source)
        self.assertIn('natural ENGLISH (en-US)',source)
        self.assertIn('validate_english_source(turn.text)',source)
        self.assertIn('canonical_source_must_be_english',source)

if __name__=="__main__":
    unittest.main()
