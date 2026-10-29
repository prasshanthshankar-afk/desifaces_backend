"""Hindi new-creation must reach the EXISTING dialogue HITL before compile."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2] / "shared/python/desifaces_shared/v3/director_graph.py"

class HindiReviewRoutingTests(unittest.TestCase):
    def test_review_required_independent_of_default_runtime_flag(self):
        src=ROOT.read_text()
        compile(src,str(ROOT),"exec")
        self.assertIn('constraints.get("requested_output_locale") == "hi-IN"',src)
        for method in ("after_plan","after_critique"):
            section=src.split(f"def {method}(",1)[1].split("\n    def ",1)[0]
            self.assertIn('if _requires_hindi_hitl(state):\n            return "review"',section)
        self.assertIn('hindi_hitl_dialogue_required',src)

if __name__=="__main__":
    unittest.main()
