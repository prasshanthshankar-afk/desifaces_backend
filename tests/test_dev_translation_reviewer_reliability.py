"""Provider-first DEV translation: approved direction, fail-closed provider errors."""
import pytest
from app.services import dev_provider_translation as module
from app.services import dev_translation_provider_adapters as adapters


class Pool:
    async def fetch(self, sql, *args):
        assert "public.tts_locales" in sql
        capability = dict(source_locale="en-US", target_locale="hi-IN",
                          provider="sarvam", model="mayura:v1",
                          provider_source_code="en-IN", provider_target_code="hi-IN",
                          approved=True, enabled=True)
        return [dict(locale="en-US", language_code="en", meta_json={}),
                dict(locale="hi-IN", language_code="hi",
                     meta_json={"translation_routing_v1":
                                {"enabled":True,"capabilities":[capability]}})]


@pytest.mark.asyncio
async def test_approved_route_calls_provider_once_without_ai_review(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY","fake")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls=[]
    async def fake_provider(client, *, route, text, speaker_gender):
        calls.append((route["provider_code"], text, speaker_gender))
        return "हमारा कैफ़े अगले शुक्रवार खुल रहा है!"
    monkeypatch.setattr(adapters,"translate_via_configured_provider",fake_provider)
    result = await module.translate_approved(
        Pool(), text="Our café opens next Friday.", source_locale="en-US",
        target_locale="hi-IN", speaker_gender="female",
        original_context={"full_audio_script":"Maya: Our café opens next Friday."})
    assert result == ("हमारा कैफ़े अगले शुक्रवार खुल रहा है!","sarvam","mayura:v1")
    assert calls == [("sarvam","Our café opens next Friday.","female")]


@pytest.mark.asyncio
@pytest.mark.parametrize("output",["", "   ", None])
async def test_empty_provider_result_rejected(monkeypatch, output):
    monkeypatch.setenv("SARVAM_API_KEY","fake")
    async def fake_provider(*args, **kwargs):
        return output
    monkeypatch.setattr(adapters,"translate_via_configured_provider",fake_provider)
    with pytest.raises(module.DevTranslationRejected,match="empty_provider_translation"):
        await module.translate_approved(Pool(),text="Hello",source_locale="en-US",
                                        target_locale="hi-IN",speaker_gender="male",
                                        original_context={})


@pytest.mark.asyncio
async def test_provider_exception_never_silently_synthesizes(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY","fake")
    async def fake_provider(*args, **kwargs):
        raise adapters.ProviderTranslationError("provider_unavailable")
    monkeypatch.setattr(adapters,"translate_via_configured_provider",fake_provider)
    with pytest.raises(adapters.ProviderTranslationError,match="provider_unavailable"):
        await module.translate_approved(Pool(),text="Hello",source_locale="en-US",
                                        target_locale="hi-IN",speaker_gender="male",
                                        original_context={})
