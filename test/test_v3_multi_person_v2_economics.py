from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_shared_multi_person_policy_is_v2() -> None:
    source = (
        ROOT
        / "services/shared/python/desifaces_shared/pricing/multi_person.py"
    ).read_text(encoding="utf-8")
    assert 'PRICING_POLICY = "multi_person_workload_v2"' in source


def test_audio_multi_person_metadata_is_v2() -> None:
    source = (
        ROOT
        / "services/svc-audio/app/app/services/multi_person_pricing_policy.py"
    ).read_text(encoding="utf-8")
    assert '"pricing_policy": "multi_person_workload_v2"' in source
    assert '"participant_scaling": "aggregate_natural_usage"' in source


def test_face_multi_person_keeps_t2i_and_i2i_commercial_identity_separate() -> None:
    source = (
        ROOT
        / "services/svc-face/app/app/services/multi_person_pricing_policy.py"
    ).read_text(encoding="utf-8")
    assert '"FACE_MULTI_PERSON_I2I"' in source
    assert 'base_variant == "FACE_I2I"' in source
    assert 'base_action.endswith(".i2i")' in source
    assert '"sku_code": target_code' in source
    assert '"variant_code": target_code' in source


def test_multi_person_v2_migration_has_canonical_dev_economics() -> None:
    source = (
        ROOT / "migrations/2026_10_06_multi_person_v2_economics_alignment.sql"
    ).read_text(encoding="utf-8")

    for marker in (
        "IMG_STD_RUN' AND status='active'",
        "FACE_EDIT_PREMIUM_RUN' AND status='active'",
        "AUDIO_TTS_1K_CHARS' AND status='active'",
        "FUSION_TALK_MIN' AND status='active'",
        "'FACE_MULTI_PERSON'",
        "'FACE_MULTI_PERSON_I2I'",
        "'AUDIO_MULTI_PERSON'",
        "'FUSION_MULTI_PERSON'",
        "'GROUP_TALK_PREMIUM_SECOND'",
        "default_unit_credits=41",
        "default_unit_credits=56",
        "default_unit_credits=16",
        "default_unit_credits=1730",
        "0.22000000",
        "0.30000000",
        "0.10000000",
        "13.50000000",
        "0.16000000",
        "'multi_person_workload_v2'",
    ):
        assert marker in source
