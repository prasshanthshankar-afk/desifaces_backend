from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # services/svc-fusion-extension
REPO_ROOT = ROOT.parents[1]

pricing_path = ROOT / "app/app/services/premium_actual_seconds_pricing.py"
spec = importlib.util.spec_from_file_location("premium_actual_seconds_pricing", pricing_path)
assert spec and spec.loader
pricing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pricing)

assert pricing.PREMIUM_CREDITS_PER_SECOND == 15
assert pricing.PREMIUM_MIN_BILLABLE_SECONDS == 10
assert pricing.premium_billable_seconds(7) == 10
assert pricing.premium_billable_seconds(10) == 10
assert pricing.premium_billable_seconds(56) == 56
assert pricing.premium_billable_seconds(91) == 91
assert pricing.premium_billable_seconds(56) * pricing.PREMIUM_CREDITS_PER_SECOND == 840
assert pricing.premium_billable_seconds(91) * pricing.PREMIUM_CREDITS_PER_SECOND == 1365

orchestrator = (ROOT / "app/app/services/longform_orchestrator.py").read_text()
assert "PREMIUM_ACTUAL_SECONDS_PREVIEW_V1" in orchestrator
assert "PREMIUM_ACTUAL_SECONDS_RESERVE_V1" in orchestrator
assert "PREMIUM_ACTUAL_SECONDS_COMMIT_V1" in orchestrator
assert '"pricing_strategy": "premium_actual_seconds"' in orchestrator
assert 'unit_type="second"' in orchestrator
assert "PRICING_CONFIRMATION_UNITS_MISMATCH" in orchestrator

route = (ROOT / "app/app/api/routes/longform.py").read_text()
assert 'initial_status="pricing_pending"' in route  # preserve prior financial gate
assert "LONGFORM_PROGRESS_V1" in route
assert "progress=progress_view" in route
# Provider routing is explicitly outside this change.
assert "body['provider_hint'] = 'kling'" in route
assert "body['provider_hint'] = 'veed_fabric'" in route

models = (ROOT / "app/app/domain/models.py").read_text()
assert "progress: Dict[str, Any]" in models

config = (ROOT / "app/app/config.py").read_text()
assert "MAX_INFLIGHT_SEGMENTS_PER_JOB: int = Field(default=8)" in config
assert "STITCH_WORKER_BATCH_SIZE: int = Field(default=4)" in config

worker = (ROOT / "app/app/workers/longform_worker.py").read_text()
assert "await asyncio.gather" in worker
assert "fetch_next_segments" in worker

stitch = (ROOT / "app/app/workers/stitch_worker.py").read_text()
assert "_download_segments_parallel" in stitch
assert "await asyncio.gather" in stitch
assert "status = 'stitching_running'" in stitch
assert "STITCH_WORKER_BATCH_SIZE" in stitch

face = (REPO_ROOT / "services/svc-face/app/app/services/creator_orchestrator.py").read_text()
assert "FACE_PROGRESS_DETAIL_V1" in face
# Existing Face variant parallelism must remain in place.
assert "await asyncio.gather" in face
assert "_face_variant_concurrency()" in face

migration = REPO_ROOT / "migrations/2026_09_03_talking_video_premium_actual_seconds.sql"
text = migration.read_text()
assert "LONGFORM_TALK_PREMIUM_SECOND" in text
assert "default_unit_credits" in text
assert "15" in text
assert "platform_neutral" in text

print("V3_VIDEO_PRICING_PROGRESS_PERFORMANCE_TEST=PASS")
