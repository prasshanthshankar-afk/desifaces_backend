"""DEV provider-first guard tests; no provider/network/DB side effects."""
import importlib.util
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "services/svc-audio/app/app/services/dev_provider_translation.py"
spec = importlib.util.spec_from_file_location("dev_provider_translation", PATH)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

UID = "00000000-0000-4000-8000-000000000001"


def test_flag_off_is_legacy(monkeypatch):
    monkeypatch.delenv("DF_DEV_PROVIDER_TRANSLATION_ENABLED", raising=False)
    assert m.selected_for_dev(UID) is False


def test_allowlisted_dev_requires_attestation(monkeypatch):
    monkeypatch.setenv("DF_DEV_PROVIDER_TRANSLATION_ENABLED", "true")
    monkeypatch.setenv("DF_RUNTIME_ENV", "dev")
    monkeypatch.setenv("DF_DEV_TRANSLATION_HOST_ATTESTED", "true")
    monkeypatch.setenv("DF_DEV_TRANSLATION_USER_IDS", UID)
    assert m.selected_for_dev(UID)
    assert m.selected_for_dev("00000000-0000-4000-8000-000000000002") is False


def test_production_denied(monkeypatch):
    monkeypatch.setenv("DF_DEV_PROVIDER_TRANSLATION_ENABLED", "true")
    monkeypatch.setenv("DF_RUNTIME_ENV", "production")
    with pytest.raises(m.DevTranslationRejected, match="dev_environment_required"):
        m.selected_for_dev(UID)


def test_unattested_host_denied(monkeypatch):
    monkeypatch.setenv("DF_DEV_PROVIDER_TRANSLATION_ENABLED", "true")
    monkeypatch.setenv("DF_RUNTIME_ENV", "dev")
    monkeypatch.delenv("DF_DEV_TRANSLATION_HOST_ATTESTED", raising=False)
    monkeypatch.setattr(m.socket, "gethostname", lambda: "other-host")
    with pytest.raises(m.DevTranslationRejected, match="dev_host_attestation_required"):
        m.selected_for_dev(UID)


@pytest.mark.asyncio
async def test_no_approved_route_fails_before_provider_call(monkeypatch):
    class Pool:
        async def fetch(self, query, *args):
            assert "public.tts_locales" in query
            return []
    with pytest.raises(m.DevTranslationRejected, match="no_approved_translation_direction"):
        await m.translate_approved(
            Pool(), text="Hello", source_locale="en-IN",
            target_locale="hi-IN", speaker_gender="male",
            original_context={},
        )


def test_live_callers_pass_authenticated_user_identity():
    root = Path(__file__).resolve().parents[1]
    orch = (root / "services/svc-audio/app/app/services/tts_orchestrator.py").read_text()
    svc = (root / "services/svc-audio/app/app/services/tts_service.py").read_text()
    assert "authenticated_user_id=str(user_id)" in orch
    assert "provider_first = selected_for_dev(authenticated_user_id)" in svc
    assert "elif resolved_speaker_gender" in svc

def test_provider_first_runtime_uses_bounded_review_gate():
    root = Path(__file__).resolve().parents[1]
    provider = (root / "services/svc-audio/app/app/services/dev_provider_translation.py").read_text()
    assert "contextual_translation_with_review(" in provider
    assert "max_repairs=2" in provider
    assert "return approved.text, provider, model" in provider
    assert "TRANSLATION_REVIEW_REQUIRED" in provider
    # Synthesis is only called after translation finishes in the TTS service.
    svc = (root / "services/svc-audio/app/app/services/tts_service.py").read_text()
    assert svc.index("await translate_approved(") < svc.index("await self.provider_executor.synthesize(")
