#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
COMPOSE=(bash "$ROOT/scripts/v3-compose.sh")
WORKFLOW_ID="${WORKFLOW_ID:-a58bd7bf-b958-4bfe-9855-0d964d500b04}"

hold() { echo "V3 PARALLEL RESUME CERT: HOLD: $*" >&2; exit 1; }
section() { echo; echo "===== $* ====="; }

section "1. SOURCE + CLEAN TREE"
BRANCH="$(git branch --show-current)"
[[ "$BRANCH" == "feature/v3-multiperson-core-20260818" ]] || hold "wrong branch: $BRANCH"
[[ -z "$(git status --porcelain)" ]] || hold "working tree must be clean"
git --no-pager log -1 --oneline

section "2. HOST + CONTAINER RESOURCE DIAGNOSTIC"
free -h || true
echo
for svc in \
  svc-director \
  svc-director-worker \
  svc-fusion-worker \
  svc-audio-worker \
  svc-face-worker \
  svc-fusion-extension-stitch-worker; do
  cid="$("${COMPOSE[@]}" ps -q "$svc")"
  [[ -n "$cid" ]] || hold "$svc container missing"
  running="$(docker inspect -f '{{.State.Running}}' "$cid")"
  oom="$(docker inspect -f '{{.State.OOMKilled}}' "$cid")"
  restarts="$(docker inspect -f '{{.RestartCount}}' "$cid")"
  echo "$svc running=$running oom_killed=$oom restarts=$restarts"
  [[ "$running" == "true" ]] || hold "$svc not running"
  [[ "$oom" == "false" ]] || hold "$svc OOM-killed"
done

echo
echo "docker_stats_snapshot:"
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}' \
  df-v3-svc-director \
  df-v3-svc-director-worker \
  df-v3-svc-fusion-worker \
  df-v3-svc-audio-worker \
  df-v3-svc-face-worker \
  df-v3-svc-fusion-extension-stitch-worker || true

section "3. DIRECTOR READINESS + INSTALLED PARALLEL RUNTIME"
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
print("director_parallel_background_runtime=PASS")
PY

section "4. BACKGROUND SCENE COORDINATOR — LOW-MEMORY PROBE"
STITCH_CID="$("${COMPOSE[@]}" ps -q svc-fusion-extension-stitch-worker)"
STITCH_ENV="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$STITCH_CID")"
COORD_ENABLED="$(printf '%s\n' "$STITCH_ENV" | awk -F= '$1=="DF_V3_SCENE_COORDINATOR_ENABLED"{print $2; exit}')"
COORD_STATUS="$(printf '%s\n' "$STITCH_ENV" | awk -F= '$1=="DF_V3_SCENE_COORDINATOR_STATUS_CONCURRENCY"{print $2; exit}')"
COORD_BATCH="$(printf '%s\n' "$STITCH_ENV" | awk -F= '$1=="DF_V3_SCENE_COORDINATOR_BATCH_SIZE"{print $2; exit}')"
VIDEO_CONTAINER="$(printf '%s\n' "$STITCH_ENV" | awk -F= '$1=="AZURE_VIDEO_OUTPUT_CONTAINER"{print $2; exit}')"
[[ -n "$VIDEO_CONTAINER" ]] || VIDEO_CONTAINER="$(printf '%s\n' "$STITCH_ENV" | awk -F= '$1=="AZURE_FINAL_VIDEO_CONTAINER"{print $2; exit}')"
[[ -n "$VIDEO_CONTAINER" ]] || VIDEO_CONTAINER="video-output"
echo "scene_coordinator_enabled=${COORD_ENABLED:-missing}"
echo "scene_coordinator_status_concurrency=${COORD_STATUS:-missing}"
echo "scene_coordinator_batch_size=${COORD_BATCH:-missing}"
echo "scene_video_output_container=$VIDEO_CONTAINER"
[[ "${COORD_ENABLED,,}" =~ ^(1|true|yes|on)$ ]] || hold "scene coordinator env not enabled"
[[ "${COORD_STATUS:-0}" -ge 28 ]] || hold "scene coordinator status concurrency < 28"

COORD_LOG_READY=0
for _ in $(seq 1 10); do
  if "${COMPOSE[@]}" logs --no-color --tail=300 svc-fusion-extension-stitch-worker 2>&1 \
      | grep -Fq "V3 scene coordinator started"; then
    COORD_LOG_READY=1
    break
  fi
  sleep 1
done
if [[ "$COORD_LOG_READY" != "1" ]]; then
  "${COMPOSE[@]}" logs --no-color --tail=120 svc-fusion-extension-stitch-worker >&2 || true
  hold "scene coordinator startup log not observed"
fi
echo "background_scene_coordinator_runtime=PASS"

section "5. WORKER CONCURRENCY — LOW-MEMORY SOURCE/ENV PROOF"
for spec in \
  'svc-fusion-worker|DF_FUSION_WORKER_CONCURRENCY|32|services/svc-fusion/app/app/workers/fusion_worker.py' \
  'svc-audio-worker|DF_AUDIO_WORKER_CONCURRENCY|32|services/svc-audio/app/app/workers/audio_worker.py' \
  'svc-face-worker|DF_FACE_WORKER_CONCURRENCY|16|services/svc-face/app/app/workers/face_worker.py'; do
  IFS='|' read -r svc env_name default_value source_path <<< "$spec"
  cid="$("${COMPOSE[@]}" ps -q "$svc")"
  env_dump="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid")"
  value="$(printf '%s\n' "$env_dump" | awk -F= -v k="$env_name" '$1==k{print $2; exit}')"
  [[ -n "$value" ]] || value="$default_value"
  echo "$svc concurrency=$value"
  if [[ "$svc" == "svc-face-worker" ]]; then
    [[ "$value" -ge 2 ]] || hold "$svc concurrency < 2"
  else
    [[ "$value" -ge 28 ]] || hold "$svc concurrency < 28"
  fi
  grep -Fq 'asyncio.gather' "$source_path" || hold "$svc source missing asyncio.gather"
done

section "6. TELEMETRY CONTRACT IN DEPLOYED SOURCE"
for path in \
  services/svc-fusion/app/app/repos/provider_runs_repo.py \
  services/svc-audio/app/app/repos/provider_runs_repo.py \
  services/svc-face/app/app/repos/provider_runs_repo.py; do
  for key in provider_submitted_at provider_first_processing_at provider_terminal_at provider_last_status_at; do
    grep -Fq "$key" "$path" || hold "$path missing $key"
  done
  echo "provider_lifecycle_timing=PASS path=$path"
done
for path in \
  services/svc-fusion/app/app/repos/fusion_jobs_repo.py \
  services/svc-audio/app/app/repos/tts_jobs_repo.py \
  services/svc-face/app/app/repos/face_jobs_repo.py; do
  grep -Fq 'worker_claimed_at' "$path" || hold "$path missing worker_claimed_at"
  echo "worker_claim_timing=PASS path=$path"
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

section "8. ACTIVE GENERATION + BILLING SAFETY"
ACTIVE="$("${COMPOSE[@]}" exec -T desifaces-db psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
select count(*) from public.studio_jobs
where studio_type in ('face','audio','fusion')
  and status in ('queued','running','processing','submitted','pending','finalizing');
")"
echo "active_generation_jobs=$ACTIVE"
[[ "${ACTIVE:-0}" == "0" ]] || hold "unexpected active V3 generation exists"
echo "No pricing preview/reserve/commit endpoint was called by this resume certificate."
echo "NO_BILLABLE_GENERATION = PASS"

echo
echo "============================================================"
echo " V3 PARALLEL + BACKGROUND RUNTIME RESUME CERT = PASS"
echo " Director parallel fan-out >= 28"
echo " Director sync = background/read-only"
echo " Scene coordinator = running, enabled, not OOM-killed"
echo " Scene status coordination >= 28"
echo " Fusion worker concurrency >= 28"
echo " Audio worker concurrency >= 28"
echo " Face worker concurrency >= 2"
echo " Provider lifecycle timing = enabled"
echo " Worker claim timing = enabled"
echo " Certified Story remains completed"
echo " Active generation jobs = 0"
echo " NO BILLABLE GENERATION EXECUTED"
echo "============================================================"
