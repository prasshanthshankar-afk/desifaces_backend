from pathlib import Path


def _source() -> str:
    return Path("app/api/routes/canonical_audio.py").read_text(encoding="utf-8")


def test_director_canonical_output_route_is_owned_by_audio_service():
    text = _source()
    assert '@router.get("/jobs/{job_id}/canonical-output"' in text
    assert 'media_id: str' in text
    assert 'audio_url: str' in text


def test_canonical_registration_uses_durable_storage_path_not_sas_url_as_storage_ref():
    text = _source()
    assert 'meta.get("storage_path")' in text
    assert 'storage_ref = _storage_ref_from_artifact' in text


def test_canonical_registration_is_project_and_account_authorized():
    text = _source()
    assert 'public.v3_projects' in text
    assert 'public.pricing_billing_account_members' in text
    assert "bam.status = 'active'" in text


def test_canonical_registration_is_idempotent_and_records_lineage():
    text = _source()
    assert "source_audio_artifact_id" in text
    assert "source_audio_job_id" in text
    assert "on conflict (user_id, sha256) where sha256 is not null" in text.lower()


def test_director_resume_read_url_contract_is_present():
    text = _source()
    assert '@router.get("/assets/{media_id}/read-url"' in text
    assert 'source_audio_url' in text
