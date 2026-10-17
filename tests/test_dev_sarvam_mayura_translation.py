"""Offline contract tests; no real Sarvam API calls or live DEV writes."""
import importlib.util
from pathlib import Path

import httpx
import pytest

PATH = Path(__file__).resolve().parents[1] / "services/shared/llm/dev_sarvam_mayura_translation.py"
spec = importlib.util.spec_from_file_location("dev_sarvam_mayura_translation", PATH)
module = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = module
spec.loader.exec_module(module)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("DF_DEV_SARVAM_PROVIDER_TRANSLATION_ENABLED", "true")
    monkeypatch.setenv("DF_RUNTIME_ENV", "dev")
    monkeypatch.setenv("SARVAM_API_KEY", "test-only-fake-key")


@pytest.mark.asyncio
async def test_provider_payload_uses_only_current_line(enabled):
    async def handler(request):
        assert request.url.path == "/translate"
        import json
        body = json.loads(request.content)
        assert body["input"] == "I have!"
        assert body["model"] == "mayura:v1"
        assert body["mode"] == "modern-colloquial"
        assert body["output_script"] == "spoken-form-in-native"
        assert body["source_language_code"] == "en-IN"
        assert body["target_language_code"] == "hi-IN"
        return httpx.Response(200, json={"translated_text": "हाँ, मैंने देखी हैं!"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await module.translate_mayura_dev(
            text="I have!", source_locale="en-IN", target_locale="hi-IN",
            speaker_gender="female", client=client,
        )
        assert result.provider == "sarvam"
        assert result.text == "हाँ, मैंने देखी हैं!"


@pytest.mark.asyncio
@pytest.mark.parametrize("change,error", [
    ({"source_locale":"fr-FR"}, "unsupported_translation_locale"),
    ({"source_locale":"hi-IN","target_locale":"ta-IN"}, "unsupported_mayura_translation_direction"),
    ({"text":"x"*1001}, "invalid_source_length"),
    ({"speaker_gender":"unknown"}, "unsupported_speaker_gender"),
])
async def test_invalid_inputs_fail_closed(enabled, change, error):
    args = dict(text="Hello", source_locale="en-IN", target_locale="hi-IN", speaker_gender="male")
    args.update(change)
    with pytest.raises(module.MayuraTranslationError, match=error):
        await module.translate_mayura_dev(**args)


@pytest.mark.asyncio
async def test_flag_off_fail_closed(monkeypatch, enabled):
    monkeypatch.setenv("DF_DEV_SARVAM_PROVIDER_TRANSLATION_ENABLED", "false")
    with pytest.raises(module.MayuraTranslationError, match="flag_off"):
        await module.translate_mayura_dev(text="Hello", source_locale="en-IN", target_locale="hi-IN", speaker_gender="male")


@pytest.mark.asyncio
async def test_production_environment_denied(monkeypatch, enabled):
    monkeypatch.setenv("DF_RUNTIME_ENV", "production")
    with pytest.raises(module.MayuraTranslationError, match="dev_environment_required"):
        await module.translate_mayura_dev(text="Hello", source_locale="en-IN", target_locale="hi-IN", speaker_gender="male")


@pytest.mark.asyncio
async def test_provider_failure_not_silently_fallback(enabled):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(429))) as client:
        with pytest.raises(module.MayuraTranslationError, match="mayura_http_429"):
            await module.translate_mayura_dev(text="Hello", source_locale="en-IN", target_locale="hi-IN", speaker_gender="male", client=client)
