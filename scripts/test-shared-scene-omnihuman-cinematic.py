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
    "omnihuman.speaker_mask_listener_exclusion_repair",
    "split_x",
    "split_y",
):
    assert marker in adapter, marker

# Production-quality shared-scene quality controls.
for marker in (
    '"speaker_mask_strategy": "active_only"',
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
    "_normalize_shared_scene_audio_to_fal",
    "silenceremove=",
    "Preserve the approved dialogue head verbatim",
    "loudnorm=I=-18:LRA=7:TP=-1.5",
    "aresample=48000:async=1:first_pts=0",
    "DF_OMNIHUMAN_DIALOGUE_HEAD_PAD_MS",
    "DF_OMNIHUMAN_DIALOGUE_TAIL_PAD_MS",
    "adelay=",
    "apad=pad_dur=",
    'self.shared_scene_mask_strategy = "active_only"',
    "only the person in the white area speaks",
    "mask = ImageOps.invert(mask)",
):
    assert marker in adapter, marker

for marker in (
    "DF_OMNIHUMAN_SPEAKER_MASK_STRATEGY:",
    "DF_OMNIHUMAN_SHARED_AUDIO_NORMALIZATION:",
    "DF_OMNIHUMAN_SHARED_AUDIO_NORMALIZATION_REQUIRED:",
):
    assert compose.count(marker) >= 2, marker

assert "ffmpeg" in fusion_dockerfile
assert "protect_listener" not in adapter
assert "stop_periods=1" not in adapter
assert "start_periods=1:start_duration=0.12:start_threshold=-52dB" not in adapter
assert adapter.count('"areverse,"') >= 2
assert "distant non-speaking background people" in state_routes
assert "foreground subject or make background people visually prominent" in state_routes

assert 'w.metadata_json as workflow_metadata' in routes
assert 'metadata["shared_scene_source_mode"] = source_mode' in routes
assert '" This is a user-uploaded source photo: animate only people and environmental elements already visible "' in compiler
assert "Do not invent new background people, objects, signage, architecture, or scenery." in compiler

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

for marker in (
    '"quality_contract"',
    '"speaker_mask_strategy"',
    '"lipsync_quality_mode"',
    '"background_motion_mode"',
    '"ambient_motion_planned"',
    '"shared_audio_normalization_required"',
):
    assert marker in execution, marker

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

# Stitch is exactly-once per attempt. Shared-scene assembly preserves the
# provider-authored dialogue timeline and applies transition treatment to video only.
assert '"stitched_media_id": stitched_media_id' in coordinator
assert 'phase": "pricing_commit"' in coordinator
assert "Retry path: reuse the already assembled media" in coordinator
assert "if not stitched_media_id:" in coordinator
assert 'stitch_mode = "shared_dialogue" if conversation_mode == "shared_scene" else None' in coordinator
assert 'if str(conversation_mode or "").strip().lower() == "shared_scene":' in stitch
assert 'return "shared_dialogue"' in stitch

stitch_service = (
    extension / "services/stitch_service.py"
).read_text(encoding="utf-8")
for marker in (
    'DF_SHARED_SCENE_TRANSITION_SECONDS',
    'mode == "shared_dialogue"',
    'audio_edge_fade=not shared_dialogue',
    'VIDEO-ONLY fade-out/fade-in',
    '"shared_dialogue_concat.txt"',
    '"-c", "copy"',
    'audio_transition=none',
    'def _probe_video_dimensions',
    '_fit_pad_filter(target_width, target_height)',
    'channel_layouts=stereo',
    '"-ac", "2"',
):
    assert marker in stitch_service, marker

shared_dialogue_block = stitch_service.split('if mode == "shared_dialogue":', 1)[1].split(
    'if mode in {"xfade", "fade"}:', 1
)[0]
assert "_xfade_pair(" not in shared_dialogue_block
assert "acrossfade" not in shared_dialogue_block

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
    "DF_OMNIHUMAN_DIALOGUE_HEAD_PAD_MS:",
    "DF_OMNIHUMAN_DIALOGUE_TAIL_PAD_MS:",
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
print("ACTIVE_SPEAKER_MASK_CONTRACT=PASS")
print("SHARED_SCENE_AUDIO_NORMALIZATION_CONTRACT=PASS")
print("DIALOGUE_HEAD_TAIL_HANDLES_CONTRACT=PASS")
print("SHARED_DIALOGUE_VISUAL_ONLY_TRANSITION_CONTRACT=PASS")
print("SHARED_DIALOGUE_AUDIO_IMMUTABILITY_CONTRACT=PASS")
print("PRE_GENERATION_QUALITY_OBSERVABILITY=PASS")
print("GROUP_CONVERSATION_20PCT_PRICING_PRESERVED=PASS")
print("RESERVATION_OWNERSHIP_CAS=PASS")
print("STITCH_ONCE_PRICING_RETRY_ONLY=PASS")
print("NON_SHARED_FUSION_CONTRACT_PRESERVED=PASS")
print("OMNIHUMAN_COGS=PASS")
