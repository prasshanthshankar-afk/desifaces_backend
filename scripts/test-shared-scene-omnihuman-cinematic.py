#!/usr/bin/env python3
from pathlib import Path

root = Path(__file__).resolve().parents[1]
director = root / "services/svc-director/app/app"
fusion = root / "services/svc-fusion/app/app"
extension = root / "services/svc-fusion-extension/app/app"

compiler = (director / "fusion_input_performance.py").read_text(encoding="utf-8")
execution = (director / "fusion_execution.py").read_text(encoding="utf-8")
routes = (director / "shared_scene_routes.py").read_text(encoding="utf-8")
state_routes = (director / "shared_scene_state_routes.py").read_text(encoding="utf-8")
adapter = (fusion / "services/providers/omnihuman_adapter.py").read_text(encoding="utf-8")
fusion_dockerfile = (root / "services/svc-fusion/app/Dockerfile").read_text(encoding="utf-8")
pricing = (extension / "api/routes/v3_scene_pricing.py").read_text(encoding="utf-8")
coordinator = (extension / "workers/v3_scene_coordinator.py").read_text(encoding="utf-8")
stitch = (extension / "api/routes/v3_scene_stitch.py").read_text(encoding="utf-8")
compose = (root / "docker-compose.yml").read_text(encoding="utf-8")

models = (fusion / "domain/models.py").read_text(encoding="utf-8")
validators = (fusion / "domain/validators.py").read_text(encoding="utf-8")
orchestrator = (fusion / "services/fusion_orchestrator.py").read_text(encoding="utf-8")
fusion_routes = (fusion / "api/routes/fusion_jobs.py").read_text(encoding="utf-8")
fusion_config = (fusion / "config.py").read_text(encoding="utf-8")
fusion_client = (extension / "http_clients/fusion_client.py").read_text(encoding="utf-8")

# Stable, version-neutral internal provider identity.
for runtime_source in (
    compiler,
    routes,
    adapter,
    pricing,
    models,
    validators,
    orchestrator,
    fusion_routes,
    fusion_config,
    fusion_client,
    compose,
):
    assert "omnihuman_v15" not in runtime_source
    assert "fal-ai/bytedance/omnihuman/v1.5" not in runtime_source

assert 'provider_name = "omnihuman"' in adapter
assert 'default="omnihuman"' in fusion_config
assert 'DF_OMNIHUMAN_MODEL_ID is required' in compose

# Frozen shared-scene generation provider.
assert 'provider_name = "omnihuman"' in compiler
assert 'provider_name = "kling"' not in compiler
assert 'provider_name = "sync3"' not in compiler
assert '"provider_hint": "omnihuman"' in compiler
assert '"execution_provider_family": "omnihuman"' in compiler
assert 'metadata["shared_scene_provider"] = "omnihuman"' in routes
assert '"fusion_provider": "omnihuman"' in routes

# Deterministic speaker separation through OmniHuman mask_url + SAM2.
for marker in (
    '"active_speaker_coordinates": active_coordinates',
    '"listener_speaker_coordinates": listener_coordinates',
    '"shared_scene_dimensions"',
    '"speaker_mask_cache_key"',
):
    assert marker in compiler, marker

for marker in (
    '"mask_url"',
    '"fal-ai/sam2/image"',
    '{"label": 1',
    '{"label": 0',
    '"apply_mask": False',
    "OMNIHUMAN_SPEAKER_MASK_ACTIVE_NOT_WHITE",
    "OMNIHUMAN_SPEAKER_MASK_LISTENER_NOT_BLACK",
):
    assert marker in adapter, marker

# Production-quality shared-scene quality controls.
for marker in (
    '"speaker_mask_strategy": "protect_listener"',
    '"lipsync_quality_mode": "strict"',
    '"background_motion_mode": "ambient"',
    '"ambient_motion_plan": _ambient_motion_plan(context)',
    "Lip-sync is strict",
    "first audible speech phoneme",
    "do not freeze the world",
    "background geometry",
):
    assert marker in compiler, marker

assert "scene_setting: dict[str, Any]" in execution
assert "sc.setting_json" in execution
assert 'scene_setting=_as_dict(stage["setting_json"])' in execution

for marker in (
    'DF_OMNIHUMAN_SPEAKER_MASK_STRATEGY", "protect_listener"',
    'DF_OMNIHUMAN_SHARED_AUDIO_NORMALIZATION", "1"',
    'DF_OMNIHUMAN_SHARED_AUDIO_NORMALIZATION_REQUIRED", "1"',
    "_normalize_shared_scene_audio_to_fal",
    "silenceremove=",
    "loudnorm=I=-18:LRA=7:TP=-1.5",
    "aresample=48000:async=1:first_pts=0",
    'strategy == "protect_listener"',
    "positive, negative = listener, active",
    "mask = ImageOps.invert(mask)",
):
    assert marker in adapter, marker

assert "ffmpeg" in fusion_dockerfile
assert "small distant non-speaking background people" in state_routes
assert "never add another foreground subject" in state_routes

# Product-facing video styles and Creative Director camera planning.
for marker in (
    '"static_video"',
    '"cinematic_video"',
    '"director_choice"',
    '"push_in"',
    '"push_out"',
    '"arc_left"',
    '"arc_right"',
    '"angle_shift_low_to_eye"',
    '"angle_shift_high_to_eye"',
    "creative_director_scene_direction",
    "creative_director_contextual_policy_v1",
):
    assert marker in compiler, marker

assert 'metadata["shared_scene_video_style"] = body.motion_mode' in routes
assert 'metadata["shared_scene_camera_mode"] = body.camera_mode' in routes
assert 'metadata["shared_scene_video_settings_version"] = 2' in routes

# Dialogue and emotion are available to performance direction.
assert "to_jsonb(dt) as turn_json" in execution
assert "dialogue_text: str | None" in execution
assert 'getattr(turn, "dialogue_text", None)' in compiler
assert "emotion_code" in compiler

# Customer pricing remains the existing +20% group actual-second contract.
for marker in (
    '_GROUP_VARIANT_CODE = "GROUP_TALKING_VIDEO_PREMIUM_SECOND"',
    '_GROUP_LEAF_SKU_CODE = "GROUP_TALK_PREMIUM_SECOND"',
    '_GROUP_CREDITS_PER_SECOND = 18',
    '_GROUP_SURCHARGE_PCT = 20',
    '_SHARED_SCENE_PROVIDER = "omnihuman"',
    '"unit_type": "second"',
    '"quality_tier": "premium"',
):
    assert marker in pricing, marker

# Non-shared pricing remains unchanged.
for marker in (
    '_LEGACY_SERVICE_NAME = "svc-fusion"',
    '_LEGACY_SERVICE_ACTION = "fusion.video.generate"',
    '_LEGACY_VARIANT_CODE = "FUSION_MULTI_PERSON"',
    '_LEGACY_LEAF_SKU_CODE = "FUSION_MULTI_PERSON"',
    '_LEGACY_PROVIDER = "provider-neutral"',
    '"unit_type": "minute"',
):
    assert marker in pricing, marker

# Reservation ownership is compare-and-set so stale coordinator writes cannot
# replace a newer reservation.
assert "scene_pricing_reservation_superseded" in pricing
assert "_persist_parent_pricing_for_reservation" in pricing
assert "reservation_id=reservation_id" in pricing

# Stitch is exactly-once per attempt and shared-scene assembly remains hard-cut.
assert '"stitched_media_id": stitched_media_id' in coordinator
assert 'phase": "pricing_commit"' in coordinator
assert "Retry path: reuse the already assembled media" in coordinator
assert "if not stitched_media_id:" in coordinator
assert 'stitch_mode = "hard_cut" if conversation_mode == "shared_scene" else None' in coordinator
assert 'if str(conversation_mode or "").strip().lower() == "shared_scene":' in stitch
assert 'return "hard_cut"' in stitch

customer_migration = (
    root / "migrations/2026_10_03_group_conversation_premium_actual_seconds.sql"
).read_text(encoding="utf-8")
for marker in (
    "'base_credits_per_second', 15",
    "'credits_per_second', 18",
    "'multi_person_surcharge_pct', 20",
    "pb.channel IN ('web', 'mobile')",
):
    assert marker in customer_migration, marker

cogs_migration = (
    root / "migrations/2026_10_04_group_conversation_omnihuman_cogs.sql"
).read_text(encoding="utf-8")
assert "'fal_omnihuman_variable'" in cogs_migration
assert "0.16000000" in cogs_migration
assert "'customer_billing_unchanged', true" in cogs_migration

identity_migration = (
    root / "migrations/2026_10_04_omnihuman_provider_identity.sql"
).read_text(encoding="utf-8")
assert "LIKE 'omnihuman_%'" in identity_migration
assert "SET provider='omnihuman'" in identity_migration

for marker in (
    "DF_OMNIHUMAN_MODEL_ID:",
    "DF_OMNIHUMAN_SPEAKER_MASK_MODEL_ID:",
    "DF_OMNIHUMAN_SPEAKER_MASK_STRATEGY:",
    "DF_OMNIHUMAN_SHARED_AUDIO_NORMALIZATION:",
    "DF_OMNIHUMAN_SHARED_AUDIO_NORMALIZATION_REQUIRED:",
    "DF_OMNIHUMAN_UPLOAD_INPUTS_TO_FAL:",
    "FAL_KEY:",
):
    assert compose.count(marker) >= 2, marker

print("OMNIHUMAN_SHARED_SCENE_PROVIDER_FROZEN=PASS")
print("OMNIHUMAN_SPEAKER_MASK_CONTRACT=PASS")
print("STATIC_CINEMATIC_CAMERA_CONTRACT=PASS")
print("DIALOGUE_EMOTION_PERFORMANCE_CONTEXT=PASS")
print("STRICT_LIPSYNC_INPUT_CONTRACT=PASS")
print("AMBIENT_SCENE_MOTION_CONTRACT=PASS")
print("PROTECT_LISTENER_MASK_CONTRACT=PASS")
print("SHARED_SCENE_AUDIO_NORMALIZATION_CONTRACT=PASS")
print("GROUP_CONVERSATION_20PCT_PRICING_PRESERVED=PASS")
print("RESERVATION_OWNERSHIP_CAS=PASS")
print("STITCH_ONCE_PRICING_RETRY_ONLY=PASS")
print("NON_SHARED_FUSION_CONTRACT_PRESERVED=PASS")
print("OMNIHUMAN_COGS=PASS")
