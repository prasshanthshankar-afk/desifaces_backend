from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "app" / "app"


def test_shared_scene_uses_existing_premium_actual_seconds_contract():
    source = (ROOT / "api" / "routes" / "v3_scene_pricing.py").read_text(encoding="utf-8")

    assert "premium_billable_seconds" in source
    assert '_GROUP_VARIANT_CODE = "GROUP_TALKING_VIDEO_PREMIUM_SECOND"' in source
    assert '_GROUP_LEAF_SKU_CODE = "GROUP_TALK_PREMIUM_SECOND"' in source
    assert '_GROUP_CREDITS_PER_SECOND = 18' in source
    assert '_GROUP_SURCHARGE_PCT = 20' in source
    assert '"multi_person_surcharge_pct": _GROUP_SURCHARGE_PCT' in source
    assert '"provider": _SHARED_SCENE_PROVIDER' in source
    assert '_SHARED_SCENE_PROVIDER = "omnihuman_v15"' in source
    assert '"unit_type": "second"' in source
    assert '"quality_tier": "premium"' in source


def test_non_shared_scene_keeps_existing_minute_contract():
    source = (ROOT / "api" / "routes" / "v3_scene_pricing.py").read_text(encoding="utf-8")

    assert '_LEGACY_SERVICE_NAME = "svc-fusion"' in source
    assert '_LEGACY_SERVICE_ACTION = "fusion.video.generate"' in source
    assert '_LEGACY_VARIANT_CODE = "FUSION_MULTI_PERSON"' in source
    assert '_LEGACY_LEAF_SKU_CODE = "FUSION_MULTI_PERSON"' in source
    assert '_LEGACY_PROVIDER = "provider-neutral"' in source
    assert '"unit_type": "minute"' in source


def test_shared_scene_pricing_selection_is_conversation_mode_scoped():
    source = (ROOT / "api" / "routes" / "v3_scene_pricing.py").read_text(encoding="utf-8")

    assert "def _scene_pricing_contract" in source
    assert 'get("conversation_mode")' in source
    assert '== "shared_scene"' in source
    assert 'return {' in source
    assert '_LEGACY_PROVIDER = "provider-neutral"' in source
    assert '_SHARED_SCENE_PROVIDER = "omnihuman_v15"' in source


def test_group_conversation_pricing_migration_is_explicit_20_percent_and_cross_channel():
    migration = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "2026_10_03_group_conversation_premium_actual_seconds.sql"
    ).read_text(encoding="utf-8")

    assert "'GROUP_TALK_PREMIUM_SECOND'" in migration
    assert "'GROUP_TALKING_VIDEO_PREMIUM_SECOND'" in migration
    assert "'base_credits_per_second', 15" in migration
    assert "'credits_per_second', 18" in migration
    assert "'multi_person_surcharge_pct', 20" in migration
    assert "unit_credits_override" in migration
    assert "18," in migration
    assert "pb.channel IN ('web', 'mobile')" in migration
    assert "LONGFORM_TALK_PREMIUM_SECOND" not in migration
