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


def _attest(monkeypatch, *, name="desifaces-dev"):
    import io
    import json
    class MetadataResponse(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
    class Opener:
        def open(self, request, timeout=0):
            assert request.get_header("Metadata") == "true"
            return MetadataResponse(json.dumps({
                "name": name,
                "resourceGroupName": "rg-desifaces-dev",
                "subscriptionId": "e70463a7-4016-4cf5-9291-b640b0f0f5cb",
            }).encode())
    monkeypatch.setattr(m.urllib.request, "build_opener", lambda *_: Opener())


def test_allowlisted_dev_requires_attestation(monkeypatch):
    monkeypatch.setenv("DF_DEV_PROVIDER_TRANSLATION_ENABLED", "true")
    monkeypatch.setenv("DF_RUNTIME_ENV", "dev")
    monkeypatch.setenv("DF_DEV_TRANSLATION_USER_IDS", UID)
    _attest(monkeypatch)
    assert m.selected_for_dev(UID)
    assert m.selected_for_dev("00000000-0000-4000-8000-000000000002") is False


def test_production_denied(monkeypatch):
    monkeypatch.setenv("DF_DEV_PROVIDER_TRANSLATION_ENABLED", "true")
    monkeypatch.setenv("DF_RUNTIME_ENV", "production")
    with pytest.raises(m.DevTranslationRejected, match="dev_environment_required"):
        m.selected_for_dev(UID)


def test_other_azure_host_denied_even_with_env_override(monkeypatch):
    monkeypatch.setenv("DF_DEV_PROVIDER_TRANSLATION_ENABLED", "true")
    monkeypatch.setenv("DF_RUNTIME_ENV", "dev")
    monkeypatch.setenv("DF_DEV_TRANSLATION_HOST_ATTESTED", "true")
    _attest(monkeypatch, name="desifaces-gpu")
    with pytest.raises(m.DevTranslationRejected, match="dev_host_attestation_required"):
        m.selected_for_dev(UID)


def test_unreachable_metadata_fails_closed(monkeypatch):
    monkeypatch.setenv("DF_DEV_PROVIDER_TRANSLATION_ENABLED", "true")
    monkeypatch.setenv("DF_RUNTIME_ENV", "dev")
    monkeypatch.setenv("DF_DEV_TRANSLATION_USER_IDS", UID)
    def unavailable(*args):
        raise TimeoutError("unavailable")
    monkeypatch.setattr(m.urllib.request, "build_opener", unavailable)
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


def test_full_audio_script_reaches_clarification_and_independent_review():
    root = Path(__file__).resolve().parents[1]
    orchestrator = (root / "services/svc-audio/app/app/services/tts_orchestrator.py").read_text()
    service = (root / "services/svc-audio/app/app/services/tts_service.py").read_text()
    translation = (root / "services/svc-audio/app/app/services/dev_provider_translation.py").read_text()
    assert 'full_audio_script=payload.get("full_audio_script")' in orchestrator
    assert '"full_audio_script": full_audio_script' in service
    assert translation.count('"full_audio_script": full_script') == 2
    assert '"speaker_relationship": original_context.get("speaker_relationship"' in translation
    assert '"emotion": original_context.get("emotion"' in translation


def test_director_reconstructs_and_audio_api_accepts_canonical_script():
    root = Path(__file__).resolve().parents[1]
    director = (root / "services/svc-director/app/app/audio_execution.py").read_text()
    runtime = (root / "services/svc-director/app/app/audio_execution_runtime.py").read_text()
    api = (root / "services/svc-audio/app/app/api/routes/tts_jobs.py").read_text()
    assert "ORDER BY dt.sequence_no, dt.turn_id" in director
    assert 'stage_metadata["full_audio_script"] = transcript' in director
    assert '("full_audio_script", 16000)' in runtime
    assert 'full_audio_script: Optional[str] = Field(default=None, max_length=16000)' in api
    assert '"full_audio_script": req.full_audio_script' in api


def test_full_script_cannot_authorize_scene_summaries():
    root = Path(__file__).resolve().parents[1]
    source = (root / "services/svc-audio/app/app/services/dev_provider_translation.py").read_text()
    assert "narrative_instead_of_dialogue" in source
    assert "meaning_expansion_not_dialogue" in source
    assert "translation_expands_into_scene_summary" in source
    assert "brand_name_not_preserved" in source
    assert "NEVER describe the speaker" in source
    assert "REJECT if candidate narrates or summarizes" in source
