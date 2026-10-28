import unittest
from pathlib import Path
from app.localization_source_contract import validate_english_source

class SourceSeparationTests(unittest.TestCase):
    def test_english_source_accepted(self):
        validate_english_source("I can't wait to serve customers when the café opens.")
    def test_hindi_under_english_source_rejected(self):
        with self.assertRaisesRegex(ValueError, "canonical_english_source_locale_mismatch"):
            validate_english_source("मेरा कैफ़े अगले शुक्रवार खुलेगा।")
    def test_empty_rejected(self):
        with self.assertRaisesRegex(ValueError, "canonical_english_source_missing"):
            validate_english_source("")
    def test_contract_wired(self):
        root=Path(__file__).resolve().parents[1] / "app"
        for p in ["experience_compiler.py","scene_localization_preview_routes.py"]:
            self.assertIn("validate_english_source(", (root/p).read_text())

if __name__ == "__main__":
    unittest.main()
