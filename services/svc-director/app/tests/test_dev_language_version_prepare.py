"""DEV language-version preparation contract, no DB or media writes."""
import unittest
from pathlib import Path


class LanguageVersionPreparationTests(unittest.TestCase):
    def test_read_only_source_preparation(self):
        p = Path(__file__).resolve().parents[1] / "app" / "language_version_prepare_routes.py"
        source = p.read_text()
        compile(source, str(p), "exec")
        self.assertIn("sc.story_id=$1", source)
        self.assertIn("workflow_id=$1 and account_id=$2", source)
        self.assertIn("source_conversation_not_completed", source)
        self.assertIn('"source_unchanged": True', source)
        self.assertIn('"new_generation_started": False', source)
        self.assertNotIn("update public.", source.lower())
        self.assertNotIn("insert into public.", source.lower())
        self.assertNotIn("delete from public.", source.lower())

    def test_registered_for_existing_director(self):
        p = Path(__file__).resolve().parents[1] / "app" / "studio_routes_runtime.py"
        source = p.read_text()
        self.assertIn("router.include_router(_language_version_prepare_router)", source)


if __name__ == "__main__":
    unittest.main()
