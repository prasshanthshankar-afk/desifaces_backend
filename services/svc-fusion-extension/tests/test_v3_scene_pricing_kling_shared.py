from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "app" / "app"


def test_shared_scene_uses_existing_premium_actual_seconds_contract():
    source = (ROOT / "api" / "routes" / "v3_scene_pricing.py").read_text(encoding="utf-8")

    assert "PREMIUM_ACTUAL_SECONDS_VARIANT" in source
    assert "PREMIUM_ACTUAL_SECONDS_SKU" in source
    assert "PREMIUM_ACTUAL_SECONDS_ACTION" in source
    assert "premium_billable_seconds" in source
    assert '"provider": _SHARED_SCENE_PROVIDER' in source
    assert '_SHARED_SCENE_PROVIDER = "kling"' in source
    assert '"unit_type": "second"' in source
    assert '"quality_tier": "premium"' in source


def test_non_shared_scene_keeps_existing_minute_contract():
    source = (ROOT / "api" / "routes" / "v3_scene_pricing.py").read_text(encoding="utf-8")

    assert '_LEGACY_SERVICE_NAME = "svc-fusion"' in source
    assert '_LEGACY_SERVICE_ACTION = "fusion.video.generate"' in source
    assert '_LEGACY_VARIANT_CODE = "FUSION_TALKING_VIDEO"' in source
    assert '_LEGACY_LEAF_SKU_CODE = "FUSION_TALK_MIN"' in source
    assert '_LEGACY_PROVIDER = "veed_fabric"' in source
    assert '"unit_type": "minute"' in source


def test_shared_scene_pricing_selection_is_conversation_mode_scoped():
    source = (ROOT / "api" / "routes" / "v3_scene_pricing.py").read_text(encoding="utf-8")

    assert "def _scene_pricing_contract" in source
    assert 'get("conversation_mode")' in source
    assert '== "shared_scene"' in source
    assert 'return {' in source
    assert '"provider_neutral"' not in source  # route uses seeded variant; no bespoke rate table
