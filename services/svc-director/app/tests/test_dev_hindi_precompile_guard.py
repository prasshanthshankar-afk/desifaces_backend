"""DEV: rejected Hindi HITL snapshots must fail before story compilation."""
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
class HindiPrecompileChecks(unittest.TestCase):
    def test_validation_precedes_canonical_write(self):
        text=(ROOT/"app/experience_compiler.py").read_text()
        compile(text,"experience_compiler.py","exec")
        assert text.index("approved_hitl_precompile_mismatch") < text.index("graph = await super().compile(")
        assert text.index("approved_hitl_turn_count_mismatch") < text.index("graph = await super().compile(")
        assert text.index("unsupported_hitl_localization_locale") < text.index("graph = await super().compile(")

if __name__=="__main__":
    unittest.main()
