#!/usr/bin/env python3
from pathlib import Path
import importlib.util

root = Path(__file__).resolve().parents[1]

planner = (root / "services/svc-audio/app/app/services/tts_resolution_planner.py").read_text()
assert "requested_voice_not_eligible_for_any_model" in planner
assert "effective_requested_voice = None" in planner
assert "requested_gender=request.requested_gender" in planner

prompt = (root / "services/svc-face/app/app/services/creator_prompt_service.py").read_text()
assert "FACE_VARIANT_GENDER_METADATA_V1" in prompt
assert "_infer_gender_from_prompt" in prompt
assert 'request_dict["gender"] = gender' in prompt

orch = (root / "services/svc-face/app/app/services/creator_orchestrator.py").read_text()
assert "FACE_VARIANT_TECHNICAL_GENDER_V1" in orch
assert 'technical["gender"] = gender' in orch

route = (root / "services/svc-fusion-extension/app/app/api/routes/longform.py").read_text()
assert "VIDEO_DIRECTION_CONTRACT_V1" in route
assert "apply_video_direction" in route
assert "body['provider_hint'] = 'kling'" in route
assert "body['provider_hint'] = 'veed_fabric'" in route

helper_path = root / "services/svc-fusion-extension/app/app/services/video_direction_contract.py"
spec = importlib.util.spec_from_file_location("video_direction_contract_test", helper_path)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(mod)

defaults = mod.normalize_video_direction({})
assert defaults == {
    "performance_style": "natural",
    "emotion": "auto",
    "scene_motion": "auto",
    "hand_motion": "auto",
    "body_motion": "auto",
    "camera_motion": "auto",
    "delivery_energy": "normal",
}
body, tags, opts = mod.apply_video_direction(
    {},
    {"video_direction": {"emotion": "warm", "scene_motion": "ambient", "hand_motion": "subtle"}},
    {"provider_hint": "kling"},
)
assert opts["provider_hint"] == "kling"
assert body["background_mode"] == "movement_based"
assert tags["emotion"] == "warm"
assert tags["hand_motion"] == "subtle"
assert "original image context" in opts["motion_prompt"]

print("V3_WEB_E2E_BACKEND_CLOSEOUT_TEST=PASS")
