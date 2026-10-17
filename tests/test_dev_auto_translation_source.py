"""Offline safety contract for UI source_language='auto'."""
import importlib.util
import sys
from pathlib import Path
import httpx
import pytest

SOURCE = Path(__file__).resolve().parents[1] / "services/svc-audio/app/app/services/dev_provider_translation.py"
spec = importlib.util.spec_from_file_location("dev_provider_translation", SOURCE)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


@pytest.mark.asyncio
@pytest.mark.parametrize("detected,expected", [
    ("en", "en"), ("es", "es"), ("ar", "ar"), ("hi", "hi"),
])
async def test_auto_detects_language_without_hardcoded_english(monkeypatch, detected, expected):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
    async def handler(request):
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, json={
            "output": [{"content": [{"text": '{"language_code":"' + detected + '"}'}]}],
        })
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: client)
    assert await module._detect_auto_source_language("Example text") == expected
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("detected", [None, "auto", "en-IN", "unknown", "EN", ""])
async def test_auto_detection_fails_closed(monkeypatch, detected):
    import json
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
    async def handler(request):
        return httpx.Response(200, json={
            "output": [{"content": [{"text": json.dumps({"language_code": detected})}]}],
        })
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(module.DevTranslationRejected, match="auto_source_language_ambiguous"):
        await module._detect_auto_source_language("Example text")
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_detection_requires_credential(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(module.DevTranslationRejected, match="auto_source_detection_credential_missing"):
        await module._detect_auto_source_language("Example")
