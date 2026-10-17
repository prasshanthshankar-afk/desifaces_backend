"""Metadata-backed translation registry must fail closed by default."""
import importlib.util
import sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
p=ROOT/"services/svc-audio/app/app/repos/translation_capability_repo.py"
spec=importlib.util.spec_from_file_location("translation_repo_test",p)
m=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class Pool:
    def __init__(self,meta=None): self.meta=meta or {};self.calls=[]
    async def fetch(self,q,*args):
        self.calls.append((q,args))
        return [{"locale":"en-IN","language_code":"en","meta_json":{}},
                {"locale":"hi-IN","language_code":"hi","meta_json":self.meta}]

def config(enabled=True,approved=True):
    return {"translation_routing_v1":{"enabled":enabled,"capabilities":[{
      "provider":"sarvam","model":"mayura:v1","source_locale":"en-IN",
      "target_locale":"hi-IN","provider_source_code":"en-IN",
      "provider_target_code":"hi-IN","enabled":True,"approved":approved,"priority":10}]}}

@pytest.mark.asyncio
async def test_approved_metadata_route():
    p=Pool(config()); result=await m.list_approved_translation_routes(p,source_locale="en",target_locale="hi-IN")
    assert len(result)==1 and result[0]["provider_code"]=="sarvam"
    assert p.calls[0][1]==("en","hi-IN")
    assert "public.tts_locales" in p.calls[0][0]

@pytest.mark.asyncio
@pytest.mark.parametrize("meta",[{},config(False),config(True,False)])
async def test_unapproved_or_missing_fails_closed(meta):
    assert await m.list_approved_translation_routes(Pool(meta),source_locale="en",target_locale="hi-IN")==[]

@pytest.mark.asyncio
@pytest.mark.parametrize("src,tgt",[("","hi-IN"),("en-IN",""),("en-IN","en-IN")])
async def test_invalid_direction_no_query(src,tgt):
    p=Pool()
    assert await m.list_approved_translation_routes(p,source_locale=src,target_locale=tgt)==[]
    assert not p.calls
