from pathlib import Path


def test_openai_image_transient_retry_is_part_of_dev_baseline():
    source = Path(__file__).resolve().parents[1] / "services" / "providers" / "image_provider.py"
    text = source.read_text(encoding="utf-8")
    assert "OPENAI_IMAGE_TRANSIENT_RETRY_V1_DEV_SYNC" in text
    assert "status=429" in text
    for status in (500, 502, 503, 504):
        assert f"status={status}" in text
    assert "_openai_call_with_retry" in text
    assert "attempts = 3" in text
