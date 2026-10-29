"""Guard the Director approval snapshot at the actual compile handoff."""
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "shared/python/desifaces_shared/v3/director_graph.py"

class HindiCompileHandoff(unittest.TestCase):
    def test_snapshot_sent_only_to_compiler(self):
        source=SRC.read_text()
        compile(source,str(SRC),"exec")
        planning=source.split("async def plan_story(",1)[1].split("async def critique_story(",1)[0]
        compilation=source.split("async def compile_story(",1)[1].split("def after_plan(",1)[0]
        self.assertNotIn('"_approved_hitl_localization"',planning)
        self.assertIn('"_approved_hitl_localization"',compilation)
        self.assertIn('"target_locale": state.get("approved_target_locale")',compilation)
        self.assertIn('"turns": list(state.get("approved_localized_turns") or [])',compilation)
        self.assertLess(compilation.index('"_approved_hitl_localization"'),compilation.index('workspace = build_story_workspace'))

if __name__=="__main__":
    unittest.main()
