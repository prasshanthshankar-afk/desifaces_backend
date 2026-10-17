"""Offline tests: base language must not silently pick an arbitrary region."""
import importlib.util
import sys
from pathlib import Path
import pytest

p=Path(__file__).resolve().parents[1]/"services/svc-audio/app/app/services/dev_provider_translation.py"
spec=importlib.util.spec_from_file_location("dev_provider_locale_resolution",p)
m=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=m
spec.loader.exec_module(m)


class Pool:
    def __init__(self, rows):
        self.rows=rows
    async def fetch(self, query, *args):
        assert "JOIN public.tts_locales" in query
        assert "src.is_enabled" in query
        assert "c.is_approved" in query
        assert args == ("en", "hi-IN")
        return self.rows

def row(locale):
    return dict(source_locale=locale,target_locale="hi-IN",
                provider_code="sarvam",model_code="mayura:v1",
                provider_source_code="en-IN",provider_target_code="hi-IN")

@pytest.mark.asyncio
async def test_ambiguous_english_region_is_rejected():
    with pytest.raises(m.DevTranslationRejected,match="ambiguous_source_locale"):
        await m.translate_approved(Pool([row("en-IN"),row("en-US")]),
            text="Hello",source_locale="en",target_locale="hi-IN",
            speaker_gender="female",original_context={})

@pytest.mark.asyncio
async def test_unique_base_locale_reaches_credential_gate(monkeypatch):
    monkeypatch.delenv("SARVAM_API_KEY",raising=False)
    with pytest.raises(m.DevTranslationRejected,match="sarvam_credential_missing"):
        await m.translate_approved(Pool([row("en-IN")]),
            text="Hello",source_locale="en",target_locale="hi-IN",
            speaker_gender="female",original_context={})
