#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
COMPOSE=(bash "$ROOT/scripts/v3-compose.sh")

section() { echo; echo "===== $* ====="; }

section "1. SOURCE"
echo "branch=$(git branch --show-current)"
echo "head=$(git rev-parse --short HEAD)"
git status --short --branch

section "2. DIRECTOR RUNTIME — CORRECT INSTALL PROBE"
curl -fsS http://127.0.0.1:18011/api/health | jq '{ok,service,execution_mode,runtime_ready,configuration_error}'
"${COMPOSE[@]}" exec -T svc-director python - <<'PY'
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
print("DIRECTOR_BACKGROUND_RUNTIME = PASS")
PY

section "3. STITCH WORKER CONTAINER STATE"
CID="$("${COMPOSE[@]}" ps -q svc-fusion-extension-stitch-worker)"
if [[ -z "$CID" ]]; then
  echo "STITCH_CONTAINER = MISSING"
  exit 2
fi

echo "container_id=$CID"
docker inspect -f 'status={{.State.Status}} running={{.State.Running}} restarting={{.State.Restarting}} exit_code={{.State.ExitCode}} oom_killed={{.State.OOMKilled}} restarts={{.RestartCount}} error={{printf "%q" .State.Error}} started_at={{.State.StartedAt}} finished_at={{.State.FinishedAt}}' "$CID"
echo "command=$(docker inspect -f '{{json .Config.Cmd}}' "$CID")"
echo "entrypoint=$(docker inspect -f '{{json .Config.Entrypoint}}' "$CID")"
echo "image=$(docker inspect -f '{{.Config.Image}}' "$CID")"

echo
for key in DF_V3_SCENE_COORDINATOR_ENABLED DF_V3_SCENE_COORDINATOR_POLL_SECONDS DF_V3_SCENE_COORDINATOR_BATCH_SIZE DF_V3_SCENE_COORDINATOR_STATUS_CONCURRENCY STITCH_WORKER_ENABLED SVC_FUSION_BASE_URL AZURE_VIDEO_OUTPUT_CONTAINER AZURE_FINAL_VIDEO_CONTAINER DATABASE_URL; do
  value="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CID" | awk -F= -v k="$key" '$1==k{print substr($0,length(k)+2); exit}')"
  if [[ "$key" == "DATABASE_URL" && -n "$value" ]]; then
    value='<configured>'
  fi
  echo "$key=${value:-<missing>}"
done

section "4. STITCH WORKER RECENT LOGS"
"${COMPOSE[@]}" logs --no-color --timestamps --tail=240 svc-fusion-extension-stitch-worker 2>&1 || true

section "5. HOST MEMORY + KERNEL OOM EVIDENCE"
free -h || true
if command -v journalctl >/dev/null 2>&1; then
  journalctl -k --since '30 minutes ago' --no-pager 2>/dev/null \
    | grep -Ei 'oom|out of memory|killed process' \
    | tail -80 || true
fi

section "6. NO MUTATION"
echo "This diagnostic performed no rebuild, restart, pricing call, or generation call."
echo "V3_BACKGROUND_DIAGNOSTIC = COMPLETE"
