#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
COMPOSE=(bash "$ROOT/scripts/v3-compose.sh")
WORKFLOW_ID="${WORKFLOW_ID:-a58bd7bf-b958-4bfe-9855-0d964d500b04}"

hold() { echo "V3 PARALLEL RUNTIME: HOLD: $*" >&2; exit 1; }
section() { echo; echo "===== $* ====="; }

section "1. SOURCE + STATIC CONTRACT"
BRANCH="$(git branch --show-current)"
[[ "$BRANCH" == "feature/v3-multiperson-core-20260818" ]] || hold "wrong branch: $BRANCH"
[[ -z "$(git status --porcelain)" ]] || hold "working tree must be clean"

git --no-pager log -1 --oneline
python3 -m py_compile \
  services/svc-director/app/app/fusion_execution_parallel_dispatch.py \
  services/svc-director/app/app/fusion_execution_background_read.py \
  services/svc-director/app/app/fusion_execution_runtime.py \
  services/svc-director/app/app/fusion_input_performance.py \
  services/svc-fusion/app/app/workers/fusion_worker.py \
  services/svc-fusion/app/app/repos/fusion_jobs_repo.py \
  services/svc-fusion/app/app/repos/provider_runs_repo.py \
  services/svc-audio/app/app/workers/audio_worker.py \
  services/svc-audio/app/app/repos/tts_jobs_repo.py \
  services/svc-audio/app/app/repos/provider_runs_repo.py \
  services/svc-face/app/app/workers/face_worker.py \
  services/svc-face/app/app/repos/face_jobs_repo.py \
  services/svc-face/app/app/repos/provider_runs_repo.py \
  services/svc-fusion-extension/app/app/workers/v3_scene_coordinator.py \
  services/svc-fusion-extension/app/app/workers/stitch_worker.py

python3 - <<'PY'
from pathlib import Path
checks = {
    "director_parallel": (
        "services/svc-director/app/app/fusion_execution_parallel_dispatch.py",
        ["asyncio.gather", "DF_DIRECTOR_FUSION_DISPATCH_CONCURRENCY", "dispatch_spread_ms", '"execution_mode": "parallel"'],
    ),
    "fusion_input_parallel": (
        "services/svc-director/app/app/fusion_input_performance.py",
        ["asyncio.gather", "unique_faces", "unique_audio", "DF_DIRECTOR_FUSION_INPUT_CONCURRENCY", '"32"'],
    ),
    "background_read_sync": (
        "services/svc-director/app/app/fusion_execution_background_read.py",
        ["BackgroundFinalizedParallelSceneFusionExecutionService", "DF_V3_FUSION_BACKGROUND_COORDINATOR_ENABLED", "background_finalization"],
    ),
    "background_scene_coordinator": (
        "services/svc-fusion-extension/app/app/workers/v3_scene_coordinator.py",
        ["DF_V3_SCENE_COORDINATOR_ENABLED", "pg_try_advisory_lock", "stitch_scene", "commit_scene_pricing", "stitch_ms"],
    ),
    "provider_timing": (
        "services/svc-fusion/app/app/repos/provider_runs_repo.py",
        ["provider_submitted_at", "provider_first_processing_at", "provider_terminal_at", "provider_last_status_at"],
    ),
    "fusion_worker_parallel": (
        "services/svc-fusion/app/app/workers/fusion_worker.py",
        ["DF_FUSION_WORKER_CONCURRENCY", "asyncio.gather", '"32"'],
    ),
    "audio_worker_parallel": (
        "services/svc-audio/app/app/workers/audio_worker.py",
        ["DF_AUDIO_WORKER_CONCURRENCY", "asyncio.gather", '"32"'],
    ),
    "face_worker_parallel": (
        "services/svc-face/app/app/workers/face_worker.py",
        ["DF_FACE_WORKER_CONCURRENCY", "asyncio.gather", '"16"'],
    ),
}
for name, (path, needles) in checks.items():
    text = Path(path).read_text()
    missing = [needle for needle in needles if needle not in text]
    if missing:
        raise SystemExit(f"{name}=FAIL missing={missing}")
    print(f"{name}=PASS")
for path in (
    "services/svc-fusion/app/app/repos/fusion_jobs_repo.py",
    "services/svc-audio/app/app/repos/tts_jobs_repo.py",
    "services/svc-face/app/app/repos/face_jobs_repo.py",
):
    text = Path(path).read_text()
    if "worker_claimed_at" not in text:
        raise SystemExit(f"worker_claim_timing=FAIL path={path}")
print("worker_claim_timing=PASS")
PY

echo "STATIC_CONTRACT = PASS"

section "2. BUILD ONLY V3 PERFORMANCE SERVICES"
"${COMPOSE[@]}" build \
  svc-director \
  svc-director-worker \
  svc-fusion-worker \
  svc-audio-worker \
  svc-face-worker \
  svc-fusion-extension-stitch-worker

section "3. CUT OVER ONLY V3 PERFORMANCE SERVICES"
"${COMPOSE[@]}" up -d --no-deps \
  svc-director \
  svc-director-worker \
  svc-fusion-worker \
  svc-audio-worker \
  svc-face-worker \
  svc-fusion-extension-stitch-worker

section "4. DIRECTOR READINESS"
READY=0
for _ in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:18011/api/health >/tmp/v3-director-health.json 2>/dev/null; then
    READY=1
    break
  fi
  sleep 1
done
[[ "$READY" == "1" ]] || hold "svc-director did not become ready"
cat /tmp/v3-director-health.json | jq '{ok,service,execution_mode,runtime_ready,configuration_error}'

section "5. RUNTIME PARALLELISM + BACKGROUND FINALIZATION"
"${COMPOSE[@]}" exec -T svc-director python - <<'PY'
import os
from app import fusion_execution_runtime as _runtime
from app.fusion_execution_background_read import BackgroundFinalizedParallelSceneFusionExecutionService, _background_enabled
from app.fusion_execution_parallel_dispatch import _dispatch_limit
from app.fusion_input_performance import compile_children_performant, _input_concurrency
from app import fusion_execution
from app import fusion_execution_parent_pricing
from app import fusion_execution_parallel_dispatch
installed = fusion_execution.SceneFusionExecutionService
print(f"director_input_concurrency={_input_concurrency()}")
print(f"director_dispatch_concurrency={_dispatch_limit()}")
print(f"installed_service={installed.__name__}")
print(f"input_compiler={fusion_execution_parallel_dispatch._compile_children.__name__}")
print(f"status_concurrency={BackgroundFinalizedParallelSceneFusionExecutionService.status_concurrency}")
print(f"child_pricing_concurrency={BackgroundFinalizedParallelSceneFusionExecutionService.child_pricing_concurrency}")
print(f"pricing_concurrency={BackgroundFinalizedParallelSceneFusionExecutionService.pricing_concurrency}")
print(f"background_read_sync={_background_enabled()}")
assert _input_concurrency() >= 28
assert _dispatch_limit() >= 28
assert installed is BackgroundFinalizedParallelSceneFusionExecutionService
assert fusion_execution_parallel_dispatch._compile_children is compile_children_performant
assert fusion_execution_parent_pricing._compile_children is compile_children_performant
assert BackgroundFinalizedParallelSceneFusionExecutionService.status_concurrency >= 28
assert BackgroundFinalizedParallelSceneFusionExecutionService.child_pricing_concurrency >= 28
assert BackgroundFinalizedParallelSceneFusionExecutionService.pricing_concurrency >= 28
assert _background_enabled() is True
print("director_parallel_background_runtime=PASS")
PY

"${COMPOSE[@]}" exec -T svc-fusion-extension-stitch-worker python - <<'PY'
import os
from app.workers.v3_scene_coordinator import _enabled, _status_concurrency, _batch_size
from app.config import settings
print(f"scene_coordinator_enabled={_enabled()}")
print(f"scene_coordinator_status_concurrency={_status_concurrency()}")
print(f"scene_coordinator_batch_size={_batch_size()}")
print(f"scene_video_output_container={settings.AZURE_VIDEO_OUTPUT_CONTAINER}")
assert _enabled() is True
assert _status_concurrency() >= 28
assert str(settings.AZURE_VIDEO_OUTPUT_CONTAINER or '').strip()
print("background_scene_coordinator_runtime=PASS")
PY

"${COMPOSE[@]}" exec -T svc-fusion-worker python - <<'PY'
from app.workers.fusion_worker import _worker_concurrency
value = _worker_concurrency()
print(f"fusion_worker_concurrency={value}")
assert value >= 28
PY

"${COMPOSE[@]}" exec -T svc-audio-worker python - <<'PY'
from app.workers.audio_worker import _worker_concurrency
value = _worker_concurrency(1)
print(f"audio_worker_concurrency={value}")
assert value >= 28
PY

"${COMPOSE[@]}" exec -T svc-face-worker python - <<'PY'
from app.workers.face_worker import _worker_concurrency
value = _worker_concurrency()
print(f"face_worker_concurrency={value}")
assert value >= 2
PY

section "6. TELEMETRY CONTRACT IN RUNTIME IMAGES"
for svc in svc-fusion-worker svc-audio-worker svc-face-worker; do
  "${COMPOSE[@]}" exec -T "$svc" python - <<'PY'
from pathlib import Path
paths = list(Path('/app').rglob('provider_runs_repo.py'))
assert paths, 'provider_runs_repo.py missing'
text = paths[0].read_text()
for key in ('provider_submitted_at','provider_first_processing_at','provider_terminal_at','provider_last_status_at'):
    assert key in text, key
print('provider_lifecycle_timing=PASS')
PY
done

section "7. CLOSED STORY MUST REMAIN CLOSED"
POSTGRES_DB="$(awk -F= '$1=="POSTGRES_DB"{sub(/^[^=]*=/,""); print; exit}' infra/.env)"
POSTGRES_USER="$(awk -F= '$1=="POSTGRES_USER"{sub(/^[^=]*=/,""); print; exit}' infra/.env)"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || hold "refusing non-V3 DB: $POSTGRES_DB"
STATE="$("${COMPOSE[@]}" exec -T desifaces-db psql -At -F '|' -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select state,current_stage,coalesce(final_media_id::text,'') from public.v3_studio_workflows where workflow_id='$WORKFLOW_ID'::uuid")"
echo "closed_workflow=$STATE"
IFS='|' read -r WF_STATE WF_STAGE WF_MEDIA <<< "$STATE"
[[ "$WF_STATE" == "completed" ]] || hold "certified story no longer completed"
[[ -n "$WF_MEDIA" ]] || hold "certified story final media missing"

section "8. NO BILLABLE WORK PERFORMED"
echo "This gate called no pricing preview/reserve/commit endpoint and created no Face/Audio/Fusion generation job."

echo
echo "============================================================"
echo " V3 PARALLEL + BACKGROUND RUNTIME = PASS"
echo " Fusion input resolution >= 28"
echo " Director Fusion fan-out >= 28"
echo " Director sync is read-only during generation"
echo " Server-side scene coordinator = ENABLED"
echo " Server-side stitch + parent commit = ENABLED"
echo " Provider lifecycle timing = ENABLED"
echo " Worker claim timing = ENABLED"
echo " Fusion worker concurrency >= 28"
echo " Audio worker concurrency >= 28"
echo " Face worker concurrency >= 2"
echo " Certified Story remains completed"
echo " NO BILLABLE GENERATION EXECUTED"
echo "============================================================"
