from pathlib import Path


def test_audio_image_packages_top_level_gender_translation_alias():
    dockerfile = Path("app/Dockerfile").read_text(encoding="utf-8")
    assert "COPY services/shared/llm/gender_translation.py /app/gender_translation.py" in dockerfile


def test_tts_service_uses_packaged_top_level_gender_translation_contract():
    source = Path("app/app/services/tts_service.py").read_text(encoding="utf-8")
    assert "from gender_translation import (" in source
    assert "GenderTranslationError" in source
    assert "normalize_gender" in source
    assert "translate_with_gender" in source
