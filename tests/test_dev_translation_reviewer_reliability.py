"""Isolated DEV reviewer JSON/retry contracts. No real API, DB, TTS, or credits."""
import json
import pytest
import httpx
from app.services.dev_provider_translation import translate_approved, DevTranslationRejected


class Pool:
    async def fetch(self, query, *args):
        assert "public.tts_locales" in query
        route = {"source_locale": "en-US", "target_locale": "hi-IN",
                 "provider": "sarvam", "model": "mayura:v1",
                 "provider_source_code": "en-IN", "provider_target_code": "hi-IN",
                 "approved": True, "enabled": True}
        return [
            {"locale": "en-US", "language_code": "en", "meta_json": {}},
            {"locale": "hi-IN", "language_code": "hi",
             "meta_json": {"translation_routing_v1":
                           {"enabled": True, "capabilities": [route]}}},
        ]


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code
        self.request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("upstream failure", request=self.request,
                                         response=httpx.Response(self.status_code,
                                                                  request=self.request))
    def json(self):
        return self.body


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["malformed", "incomplete", "http503"])
async def test_reviewer_transient_failure_retried_then_accepts(monkeypatch, failure):
    from app.services import dev_provider_translation as module
    from app.services import dev_translation_provider_adapters as adapter
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-only")
    monkeypatch.setenv("SARVAM_API_KEY", "fake-test-only")
    calls = []
    async def provider(*args, **kwargs):
        return "हमारा कैफ़े अगले शुक्रवार खुलेगा।"
    monkeypatch.setattr(adapter, "translate_via_configured_provider", provider)
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, url, **kwargs):
            assert url.endswith("/responses")
            assert kwargs["json"]["text"]["format"]["type"] == "json_object"
            assert kwargs["json"]["max_output_tokens"] >= 600
            calls.append(kwargs["json"])
            if len(calls) == 1:
                if failure == "malformed":
                    return FakeResponse({"output_text": "{broken"})
                if failure == "incomplete":
                    return FakeResponse({"status": "incomplete", "output_text": ""})
                return FakeResponse({}, 503)
            if len(calls) == 2:
                return FakeResponse({"output_text": json.dumps({"meaning": "The cafe opens next Friday."})})
            if len(calls) == 3:
                return FakeResponse({"output_text": json.dumps({"approved": True, "reason": "natural Hindi"})})
            return FakeResponse({"output_text": json.dumps({"approved": True, "reason": "meaning preserved"})})
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: Client())
    translated, provider, model = await translate_approved(
        Pool(), text="Our cafe opens next Friday.", source_locale="en-US",
        target_locale="hi-IN", speaker_gender="female",
        original_context={"full_audio_script": "Maya: Our cafe opens next Friday."})
    assert translated == "हमारा कैफ़े अगले शुक्रवार खुलेगा।"
    assert (provider, model) == ("sarvam", "mayura:v1")
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_reviewer_twice_invalid_never_approves(monkeypatch):
    from app.services import dev_provider_translation as module
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-only")
    monkeypatch.setenv("SARVAM_API_KEY", "fake-test-only")
    calls = []
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, url, **kwargs):
            calls.append(1)
            return FakeResponse({"output_text": "{invalid"})
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: Client())
    with pytest.raises(DevTranslationRejected, match="TRANSLATION_REVIEW_REQUIRED"):
        await translate_approved(
            Pool(), text="Our cafe opens next Friday.", source_locale="en-US",
            target_locale="hi-IN", speaker_gender="female", original_context={})
    assert len(calls) == 2
