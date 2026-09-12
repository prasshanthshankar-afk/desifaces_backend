from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "services/svc-director/app/app/audio_execution_runtime.py"
source = RUNTIME.read_text()


def test_story_audio_numeric_delivery_is_sanitized_before_pricing_and_dispatch():
    assert '_NUMERIC_DELIVERY_FIELDS = ("style_degree", "rate", "pitch", "volume")' in source
    assert "def _finite_audio_number" in source
    assert "math.isfinite(number)" in source
    assert "studio_input.pop(key, None)" in source
    assert "_sanitize_numeric_delivery(studio_input)" in source


def test_qualitative_delivery_direction_is_preserved_as_context_not_numeric_payload():
    assert 'direction = "delivery_direction=" + " | ".join(notes)' in source
    assert "_preserve_qualitative_direction(studio_input, qualitative_notes)" in source
    assert 'qualitative_notes.append(f"{key}: {str(raw).strip()}")' in source


def test_pricing_and_dispatch_share_the_same_runtime_compile_boundary():
    assert "_audio_execution.compile_context_audio_input = compile_context_audio_input" in source
    assert "for key in _NUMERIC_DELIVERY_FIELDS:" in source
