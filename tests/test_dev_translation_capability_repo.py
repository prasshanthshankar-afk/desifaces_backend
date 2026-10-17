"""Unit contracts: direction-specific DB routing, default-off migration."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "services/svc-audio/app/app/repos/translation_capability_repo.py"
spec = importlib.util.spec_from_file_location("dev_translation_capability_repo", PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class FakePool:
    def __init__(self):
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return [{"provider_code": "sarvam", "model_code": "mayura:v1",
                 "source_locale": "en-IN", "target_locale": "hi-IN",
                 "provider_source_code": "en-IN", "provider_target_code": "hi-IN",
                 "priority": 5}]


@pytest.mark.asyncio
async def test_approved_direction_query():
    pool = FakePool()
    result = await mod.list_approved_translation_routes(
        pool, source_locale="en-IN", target_locale="hi-IN")
    assert result[0]["provider_code"] == "sarvam"
    assert pool.calls[0][1] == ("en-IN", "hi-IN")
    assert "is_approved=true" in pool.calls[0][0]
    assert "is_enabled=true" in pool.calls[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("src,tgt", [("", "hi-IN"), ("en-IN", ""), ("en-IN", "en-IN")])
async def test_invalid_direction_does_not_query(src, tgt):
    pool = FakePool()
    assert await mod.list_approved_translation_routes(
        pool, source_locale=src, target_locale=tgt) == []
    assert pool.calls == []


def test_migration_is_default_off_and_financially_isolated():
    sql = (ROOT / "migrations/2026_10_17_dev_audio_translation_capabilities.sql").read_text()
    assert "is_enabled boolean NOT NULL DEFAULT false" in sql
    assert "is_approved boolean NOT NULL DEFAULT false" in sql
    assert "pricing_" not in sql and "wallet" not in sql and "reservation" not in sql
