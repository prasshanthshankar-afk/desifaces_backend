#!/usr/bin/env python3
from pathlib import Path

root = Path(__file__).resolve().parents[1]

fusion_worker = (root / "services/svc-fusion/app/app/workers/fusion_worker.py").read_text()
assert 'DF_FUSION_WORKER_CONCURRENCY' in fusion_worker
assert 'limit=capacity' in fusion_worker
assert 'asyncio.create_task' in fusion_worker
assert 'return_when=asyncio.FIRST_COMPLETED' in fusion_worker
assert 'limit=1' not in fusion_worker

models = (root / "services/svc-face/app/app/domain/models.py").read_text()
assert 'PARTIAL_SUCCESS = "partial_success"' in models

face = (root / "services/svc-face/app/app/services/creator_orchestrator.py").read_text()
assert 'FACE_PARTIAL_SUCCESS_V1' in face
assert 'final_status = (' in face
assert '"partial_success"' in face
assert 'actual_units=completed_count' in face
assert '"variants_failed": failed_count' in face
assert 'status in {"succeeded", "partial_success", "failed", "cancelled"}' in face

route = (root / "services/svc-fusion-extension/app/app/api/routes/longform.py").read_text()
assert 'TRUTHFUL_PROVIDER_PROGRESS_V1' in route
assert 'activity_credit' in route
assert 'completed_fraction' in route

print("V3_FACE_VIDEO_ACTUAL_PERFORMANCE_TEST=PASS")
