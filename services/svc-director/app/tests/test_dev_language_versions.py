"""Offline checks: language-version creation must never modify source assets."""
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
class VersioningContract(unittest.TestCase):
    def test_isolated_tables(self):
        sql=(ROOT.parents[3]/"migrations/2026_10_24_dev_story_language_versions.sql").read_text()
        self.assertIn("UNIQUE (source_workflow_id,target_locale,version_no)",sql)
        self.assertIn("PRIMARY KEY(language_version_id,source_turn_id)",sql)
        self.assertIn("ON DELETE RESTRICT",sql)
        self.assertNotIn("UPDATE public.v3_studio_workflows",sql)
    def test_routes_are_owner_scoped_and_preserve_source(self):
        p=ROOT/"app/language_version_routes.py"
        source=p.read_text()
        compile(source,str(p),"exec")
        self.assertIn("account_id=$2",source)
        self.assertIn("source_conversation_not_completed",source)
        self.assertIn("user_confirmed",source)
        self.assertIn("source_workflow_unchanged",source)
        self.assertNotIn("UPDATE public.v3_dialogue_turns",source)
        self.assertNotIn("UPDATE public.v3_participants",source)
        self.assertNotIn("UPDATE public.v3_studio_stage_runs",source)
    def test_routes_registered(self):
        source=(ROOT/"app/studio_routes_runtime.py").read_text()
        self.assertIn("router.include_router(_language_version_router)",source)

if __name__=="__main__":
    unittest.main()
