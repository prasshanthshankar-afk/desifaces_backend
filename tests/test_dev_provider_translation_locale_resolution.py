"""Reject ambiguous source locales; accept a uniquely approved DB route."""
import importlib.util
import sys
from pathlib import Path
import pytest

p=Path(__file__).resolve().parents[1]/"services/svc-audio/app/app/services/dev_provider_translation.py"
spec=importlib.util.spec_from_file_location("dev_provider_locale_resolution",p)
m=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=m
spec.loader.exec_module(m)

def capability(source):
    return dict(provider="sarvam",model="mayura:v1",source_locale=source,
      target_locale="hi-IN",provider_source_code="en-IN",provider_target_code="hi-IN",
      enabled=True,approved=True)

class Pool:
    def __init__(self,caps): self.caps=caps
    async def fetch(self,q,*args):
        assert "public.tts_locales" in q and args==("en","hi-IN")
        return [
            {"locale":"en-IN","language_code":"en","meta_json":{}},
            {"locale":"en-US","language_code":"en","meta_json":{}},
            {"locale":"hi-IN","language_code":"hi","meta_json":{
              "translation_routing_v1":{"enabled":True,"capabilities":self.caps}
            }}
        ]

@pytest.mark.asyncio
async def test_ambiguous_source_fails():
    with pytest.raises(m.DevTranslationRejected,match="ambiguous_source_locale"):
        await m.translate_approved(Pool([capability("en-IN"),capability("en-US")]),
            text="Hello",source_locale="en",target_locale="hi-IN",
            speaker_gender="female",original_context={})

@pytest.mark.asyncio
async def test_unique_source_reaches_credential_check(monkeypatch):
    monkeypatch.delenv("SARVAM_API_KEY",raising=False)
    with pytest.raises(m.DevTranslationRejected,match="sarvam_credential_missing"):
        await m.translate_approved(Pool([capability("en-IN")]),
            text="Hello",source_locale="en",target_locale="hi-IN",
            speaker_gender="female",original_context={})
