"""Offline contract tests for globally routed translation adapters."""
import importlib.util
import sys
from pathlib import Path
import httpx
import pytest

path = Path(__file__).resolve().parents[1] / "services/svc-audio/app/app/services/dev_translation_provider_adapters.py"
spec = importlib.util.spec_from_file_location("dev_translation_provider_adapters", path)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


@pytest.mark.asyncio
@pytest.mark.parametrize("target,text", [("es", "Ya lo vi."), ("ar", "لقد رأيته."), ("fr", "Je l'ai vu.")])
async def test_azure_global_locale_routing(monkeypatch, target, text):
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "mock")
    monkeypatch.setenv("AZURE_TRANSLATOR_REGION", "eastus")
    def handler(request):
        assert request.url.params["from"] == "en"
        assert request.url.params["to"] == target
        assert request.headers.get("Ocp-Apim-Subscription-Region") == "eastus"
        return httpx.Response(200, json=[{"translations": [{"to": target, "text": text}]}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await m.translate_via_configured_provider(
            client, route={"provider_code": "azure_translator", "model_code": "text-translation-v3",
            "provider_source_code": "en", "provider_target_code": target},
            text="I saw it.", speaker_gender="female")
        assert result == text


@pytest.mark.asyncio
async def test_sarvam_preserves_configured_provider_locale(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "mock")
    def handler(request):
        import json
        body=json.loads(request.content)
        assert body["target_language_code"] == "od-IN"
        assert body["source_language_code"] == "en-IN"
        return httpx.Response(200, json={"translated_text": "ମୁଁ ଦେଖିଲି।"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result=await m.translate_via_configured_provider(
            client, route={"provider_code":"sarvam", "model_code":"mayura:v1",
            "provider_source_code":"en-IN","provider_target_code":"od-IN"},
            text="I saw it.", speaker_gender="female")
        assert result


@pytest.mark.asyncio
async def test_tts_only_provider_not_used_as_text_translation():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("network must not be called"))) as client:
        with pytest.raises(m.ProviderTranslationError, match="translation_provider_not_implemented"):
            await m.translate_via_configured_provider(
                client, route={"provider_code":"elevenlabs", "model_code":"eleven_multilingual_v2",
                "provider_source_code":"en", "provider_target_code":"es"},
                text="Hi", speaker_gender="female")


@pytest.mark.asyncio
async def test_azure_credential_fail_closed(monkeypatch):
    monkeypatch.delenv("AZURE_TRANSLATOR_KEY", raising=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("network must not be called"))) as client:
        with pytest.raises(m.ProviderTranslationError, match="azure_translator_credential_missing"):
            await m.translate_via_configured_provider(
                client, route={"provider_code":"azure_translator", "model_code":"text-translation-v3",
                "provider_source_code":"en","provider_target_code":"es"},
                text="Hi", speaker_gender="male")


@pytest.mark.asyncio
async def test_azure_wrong_target_rejected(monkeypatch):
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "mock")
    monkeypatch.setenv("AZURE_TRANSLATOR_REGION", "eastus")
    def handler(request):
        return httpx.Response(200, json=[{"translations":[{"to":"es", "text":"Hola"}]}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(m.ProviderTranslationError, match="empty_provider_translation"):
            await m.translate_via_configured_provider(
                client, route={"provider_code":"azure_translator",
                "model_code":"text-translation-v3","provider_source_code":"en",
                "provider_target_code":"ar"}, text="Hello", speaker_gender="female")


@pytest.mark.asyncio
async def test_unsupported_azure_model_rejected_before_network(monkeypatch):
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "mock")
    monkeypatch.setenv("AZURE_TRANSLATOR_REGION", "eastus")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: pytest.fail("network must not be called"))) as client:
        with pytest.raises(m.ProviderTranslationError, match="unsupported_azure_model"):
            await m.translate_via_configured_provider(
                client, route={"provider_code":"azure_translator",
                "model_code":"unapproved","provider_source_code":"en",
                "provider_target_code":"es"}, text="Hello", speaker_gender="male")


@pytest.mark.asyncio
async def test_google_nmt_approved_route(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_TRANSLATION_API_KEY", "mock")
    import json
    def handler(request):
        assert request.url.params["key"] == "mock"
        body = json.loads(request.content)
        assert body == {"q": "Hello", "source": "en", "target": "es",
                        "format": "text", "model": "nmt"}
        return httpx.Response(200, json={
            "data": {"translations": [{"translatedText": "Hola"}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await m.translate_via_configured_provider(
            client, route={"provider_code": "google_cloud_translation",
                           "model_code": "nmt",
                           "provider_source_code": "en",
                           "provider_target_code": "es"},
            text="Hello", speaker_gender="female") == "Hola"


@pytest.mark.asyncio
async def test_google_missing_key_rejected_without_network(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_TRANSLATION_API_KEY", raising=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: pytest.fail("network must not be called"))) as client:
        with pytest.raises(m.ProviderTranslationError,
                           match="google_translation_credential_missing"):
            await m.translate_via_configured_provider(
                client, route={"provider_code": "google_cloud_translation",
                               "model_code": "nmt",
                               "provider_source_code": "en",
                               "provider_target_code": "ar"},
                text="Hello", speaker_gender="male")


@pytest.mark.asyncio
async def test_google_unapproved_model_rejected_without_network(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_TRANSLATION_API_KEY", "mock")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: pytest.fail("network must not be called"))) as client:
        with pytest.raises(m.ProviderTranslationError,
                           match="unsupported_google_model"):
            await m.translate_via_configured_provider(
                client, route={"provider_code": "google_cloud_translation",
                               "model_code": "unapproved",
                               "provider_source_code": "en",
                               "provider_target_code": "es"},
                text="Hello", speaker_gender="female")
