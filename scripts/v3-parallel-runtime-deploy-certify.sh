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
  services/svc-director/app/app/fusion_execution_runtime.py \
  services/svc-fusion/app/app/workers/fusion_worker.py \
  services/svc-audio/app/app/workers/audio_worker.py \
  services/svc-face/app/app/workers/face_worker.py

python3 - <<'PY'
from pathlib import Path
checks = {
    "director_parallel": (
        "services/svc-director/app/app/fusion_execution_parallel_dispatch.py",
        ["asyncio.gather", "DF_DIRECTOR_FUSION_DISPATCH_CONCURRENCY", "dispatch_spread_ms", '"execution_mode": "parallel"'],
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
PY

echo "STATIC_CONTRACT = PASS"

section "2. BUILD ONLY V3 PERFORMANCE SERVICES"
"${COMPOSE[@]}" build \
  svc-director \
  svc-director-worker \
  svc-fusion-worker \
  svc-audio-worker \
  svc-face-worker

section "3. CUT OVER ONLY V3 PERFORMANCE SERVICES"
"${COMPOSE[@]}" up -d --no-deps \
  svc-director \
  svc-director-worker \
  svc-fusion-worker \
  svc-audio-worker \
  svc-face-worker

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

section "5. RUNTIME PARALLELISM"
"${COMPOSE[@]}" exec -T svc-director python - <<'PY'
from app.fusion_execution_parallel_dispatch import _dispatch_limit, ParallelOrphanReconciledParentPricedSceneFusionExecutionService
from app import fusion_execution
print(f"director_dispatch_concurrency={_dispatch_limit()}")
print(f"parallel_service={ParallelOrphanReconciledParentPricedSceneFusionExecutionService.__name__}")
print(f"installed_service={fusion_execution.SceneFusionExecutionService.__name__}")
assert _dispatch_limit() >= 28
assert fusion_execution.SceneFusionExecutionService is ParallelOrphanReconciledParentPricedSceneFusionExecutionService
print("director_parallel_runtime=PASS")
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

section "6. CLOSED STORY MUST REMAIN CLOSED"
POSTGRES_DB="$(awk -F= '$1=="POSTGRES_DB"{sub(/^[^=]*=/,""); print; exit}' infra/.env)"
POSTGRES_USER="$(awk -F= '$1=="POSTGRES_USER"{sub(/^[^=]*=/,""); print; exit}' infra/.env)"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || hold "refusing non-V3 DB: $POSTGRES_DB"
STATE="$("${COMPOSE[@]}" exec -T desifaces-db psql -At -F '|' -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select state,current_stage,coalesce(final_media_id::text,'') from public.v3_studio_workflows where workflow_id='$WORKFLOW_ID'::uuid")"
echo "closed_workflow=$STATE"
IFS='|' read -r WF_STATE WF_STAGE WF_MEDIA <<< "$STATE"
[[ "$WF_STATE" == "completed" ]] || hold "certified story no longer completed"
[[ -n "$WF_MEDIA" ]] || hold "certified story final media missing"

section "7. NO BILLABLE WORK PERFORMED"
echo "This gate called no pricing preview/reserve/commit endpoint and created no Face/Audio/Fusion generation job."

echo
echo "============================================================"
echo " V3 PARALLEL RUNTIME DEPLOYMENT = PASS"
echo " Director Fusion fan-out >= 28"
echo " Fusion worker concurrency >= 28"
echo " Audio worker concurrency >= 28"
echo " Face worker concurrency >= 2"
echo " Certified Story remains completed"
echo " NO BILLABLE GENERATION EXECUTED"
echo "============================================================"
