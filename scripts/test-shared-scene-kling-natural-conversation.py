#!/usr/bin/env python3
from pathlib import Path

root = Path(__file__).resolve().parents[1]
director = root / "services/svc-director/app/app"
extension = root / "services/svc-fusion-extension/app/app"

compiler = (director / "fusion_input_performance.py").read_text(encoding="utf-8")
parent_pricing = (director / "fusion_execution_parent_pricing.py").read_text(encoding="utf-8")
scene_pricing = (extension / "api/routes/v3_scene_pricing.py").read_text(encoding="utf-8")

# Single-provider natural conversation execution.
assert 'provider_name = "kling"' in compiler
assert 'provider_name = "sync3"' not in compiler
assert 'metadata.get("shared_scene_video_prompt")' in compiler
assert '_shared_scene_performance_prompt' in compiler
assert 'is the only person speaking' in compiler
assert 'keep the mouth closed' in compiler
assert 'hand gestures, subtle upper-body movement' in compiler
assert '"quality_tier": "premium"' in compiler
assert '"longform_profile": "talking_video"' in compiler
assert '"provider_hint": "kling"' in compiler
assert '"execution_provider_family": "kling_avatar"' in compiler
assert '"prompt": performance_prompt' in compiler

# Shared-scene uses the explicit +20% group premium actual-seconds contract;
# non-shared remains unchanged.
for marker in (
    'premium_billable_seconds',
    '_GROUP_VARIANT_CODE = "GROUP_TALKING_VIDEO_PREMIUM_SECOND"',
    '_GROUP_LEAF_SKU_CODE = "GROUP_TALK_PREMIUM_SECOND"',
    '_GROUP_CREDITS_PER_SECOND = 18',
    '_GROUP_SURCHARGE_PCT = 20',
    '_SHARED_SCENE_PROVIDER = "kling"',
    '"unit_type": "second"',
):
    assert marker in scene_pricing, marker

for marker in (
    '_LEGACY_SERVICE_NAME = "svc-fusion"',
    '_LEGACY_SERVICE_ACTION = "fusion.video.generate"',
    '_LEGACY_VARIANT_CODE = "FUSION_TALKING_VIDEO"',
    '_LEGACY_LEAF_SKU_CODE = "FUSION_TALK_MIN"',
    '_LEGACY_PROVIDER = "veed_fabric"',
    '"unit_type": "minute"',
):
    assert marker in scene_pricing, marker

# Parent pricing must fail closed on unit drift.
assert 'expected_unit = (' in parent_pricing
assert '"second"' in parent_pricing
assert '"minute"' in parent_pricing
assert 'fusion_parent_pricing_unit_must_be_' in parent_pricing

print("SHARED_SCENE_KLING_NATURAL_CONVERSATION_SOURCE=PASS")
print("SHARED_SCENE_KLING_GROUP_PREMIUM_20PCT_PRICING=PASS")
print("NON_SHARED_FUSION_CONTRACT_PRESERVED=PASS")
migration = root / "migrations/2026_10_03_group_conversation_premium_actual_seconds.sql"
text = migration.read_text(encoding="utf-8")
assert "'credits_per_second', 18" in text
assert "'multi_person_surcharge_pct', 20" in text
assert "pb.channel IN ('web', 'mobile')" in text
print("DATABASE_MIGRATION=SCOPED_GROUP_PRICING_ONLY")
print("PRODUCTION_TOUCH=NONE")
