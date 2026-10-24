"""DEV Hindi pre-Face review: static regression guards, no provider calls."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class HindiApprovedTextContract(unittest.TestCase):
    def test_same_hitl_accepts_localized_turns(self):
        main = (ROOT / "app/main.py").read_text()
        graph = (ROOT.parents[1] / "shared/python/desifaces_shared/v3/director_graph.py").read_text()
        self.assertIn("localized_turns: list[dict[str, str]]", main)
        self.assertIn('"localized_turns": body.localized_turns', main)
        self.assertIn("hindi_hitl_dialogue_required", graph)
        self.assertIn("hindi_hitl_dialogue_mismatch", graph)
        self.assertIn("_approved_hitl_localization", graph)

    def test_canonical_english_stays_intact(self):
        compiler = (ROOT / "app/experience_compiler.py").read_text()
        canonical = (ROOT / "app/compiler.py").read_text()
        self.assertIn('approved_hitl_compilation_mismatch', canonical)
        self.assertIn('"approved_localization": {', canonical)
        self.assertIn('await self._store.add_dialogue_turn(conn, turn=turn)', canonical)
        self.assertLess(canonical.index('await self._store.add_dialogue_turn(conn, turn=turn)'), canonical.index('await tx.commit()'))
        self.assertNotIn("UPDATE public.v3_dialogue_turns", compiler)
        self.assertNotIn("SET text_value=", compiler)
        self.assertIn("approved_hitl_canonical_turn_mismatch", compiler)

    def test_audio_uses_exact_approved_text(self):
        audio = (ROOT / "app/audio_execution.py").read_text()
        self.assertIn('dt.metadata_json AS dialogue_metadata', audio)
        self.assertIn('stage_metadata["approved_localization"] = canonical_approved', audio)
        self.assertIn('approved_text if use_approved_localization else context.text', audio)
        self.assertIn('translate = False', audio)

if __name__ == "__main__":
    unittest.main()
