#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
COMPOSE=(bash "$ROOT/scripts/v3-compose.sh")
WORKFLOW_ID="${WORKFLOW_ID:-a58bd7bf-b958-4bfe-9855-0d964d500b04}"
EXPECTED_PYTHONPATH='/app:/repo:/repo/services/shared:/repo/services/shared/python'

hold() { echo "V3 BACKGROUND REPAIR: HOLD: $*" >&2; exit 1; }
section() { echo; echo "===== $* ====="; }

section "1. SOURCE + ACTIVE GENERATION SAFETY"
[[ "$(git branch --show-current)" == "feature/v3-multiperson-core-20260818" ]] || hold "wrong branch"
[[ -z "$(git status --porcelain)" ]] || hold "working tree must be clean"
git --no-pager log -3 --oneline

POSTGRES_DB="$(awk -F= '$1=="POSTGRES_DB"{sub(/^[^=]*=/,""); print; exit}' infra/.env)"
POSTGRES_USER="$(awk -F= '$1=="POSTGRES_USER"{sub(/^[^=]*=/,""); print; exit}' infra/.env)"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || hold "refusing non-V3 DB: $POSTGRES_DB"
ACTIVE="$("${COMPOSE[@]}" exec -T desifaces-db psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
select count(*) from public.studio_jobs
where studio_type in ('face','audio','fusion')
  and status in ('queued','running','processing','submitted','pending','finalizing');
")"
echo "active_generation_jobs=$ACTIVE"
[[ "${ACTIVE:-0}" == "0" ]] || hold "active generation exists; refusing stitch-worker recreate"
echo "ACTIVE_GENERATION_GATE = PASS"

section "2. VERIFY V3 OVERRIDE SOURCE"
grep -Fq 'PYTHONPATH: "/app:/repo:/repo/services/shared:/repo/services/shared/python"' docker-compose.v3.yml \
  || hold "V3 stitch worker PYTHONPATH override missing from docker-compose.v3.yml"
echo "STITCH_SHARED_PYTHONPATH_SOURCE = PASS"

section "3. RECREATE ONLY STITCH WORKER — NO IMAGE BUILD"
"${COMPOSE[@]}" up -d --no-deps --force-recreate svc-fusion-extension-stitch-worker

CID="$("${COMPOSE[@]}" ps -q svc-fusion-extension-stitch-worker)"
[[ -n "$CID" ]] || hold "stitch worker container missing after recreate"

section "4. VERIFY ACTUAL CONTAINER ENVIRONMENT"
ACTUAL_PYTHONPATH="$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CID" \
    | awk -F= '$1=="PYTHONPATH"{sub(/^PYTHONPATH=/,""); print; exit}'
)"
echo "container_pythonpath=$ACTUAL_PYTHONPATH"
[[ "$ACTUAL_PYTHONPATH" == "$EXPECTED_PYTHONPATH" ]] \
  || hold "actual stitch worker PYTHONPATH mismatch"
echo "STITCH_SHARED_PYTHONPATH_RUNTIME = PASS"

section "5. WAIT FOR STABLE WORKER + COORDINATOR CONTRACT"
CMD_JSON="$(docker inspect -f '{{json .Config.Cmd}}' "$CID")"
ENV_DUMP="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CID")"
COORD_ENABLED="$(printf '%s\n' "$ENV_DUMP" | awk -F= '$1=="DF_V3_SCENE_COORDINATOR_ENABLED"{print $2; exit}')"
COORD_STATUS="$(printf '%s\n' "$ENV_DUMP" | awk -F= '$1=="DF_V3_SCENE_COORDINATOR_STATUS_CONCURRENCY"{print $2; exit}')"
[[ "${COORD_ENABLED,,}" =~ ^(1|true|yes|on)$ ]] || hold "scene coordinator env not enabled"
[[ "${COORD_STATUS:-0}" -ge 28 ]] || hold "scene coordinator status concurrency < 28"
[[ "$CMD_JSON" == *"app.workers.stitch_worker"* ]] || hold "unexpected stitch worker command"
grep -Fq 'v3_scene_coordinator_loop' services/svc-fusion-extension/app/app/workers/stitch_worker.py \
  || hold "stitch worker source missing V3 coordinator"
grep -Fq 'asyncio.gather' services/svc-fusion-extension/app/app/workers/stitch_worker.py \
  || hold "stitch worker source missing concurrent loop ownership"
"${COMPOSE[@]}" exec -T svc-fusion-extension-stitch-worker sh -lc \
  'test -d /repo/services/shared/df_contracts && test -d /repo/services/shared/python/desifaces_shared' \
  || hold "shared package roots missing in running stitch container"

PID1="$(docker inspect -f '{{.State.Pid}}' "$CID")"
RESTARTS1="$(docker inspect -f '{{.RestartCount}}' "$CID")"
sleep 5
STATUS2="$(docker inspect -f '{{.State.Status}}' "$CID")"
RUNNING2="$(docker inspect -f '{{.State.Running}}' "$CID")"
RESTARTING2="$(docker inspect -f '{{.State.Restarting}}' "$CID")"
OOM2="$(docker inspect -f '{{.State.OOMKilled}}' "$CID")"
PID2="$(docker inspect -f '{{.State.Pid}}' "$CID")"
RESTARTS2="$(docker inspect -f '{{.RestartCount}}' "$CID")"
echo "scene_worker_command=$CMD_JSON"
echo "scene_coordinator_enabled=$COORD_ENABLED"
echo "scene_coordinator_status_concurrency=$COORD_STATUS"
echo "scene_worker_stability=status:$STATUS2 running:$RUNNING2 restarting:$RESTARTING2 oom:$OOM2 pid:$PID2 restarts:$RESTARTS2"
[[ "$STATUS2" == "running" && "$RUNNING2" == "true" && "$RESTARTING2" == "false" && "$OOM2" == "false" ]] \
  || hold "stitch worker did not remain stable"
[[ "$PID2" == "$PID1" ]] || hold "stitch worker PID changed during stability window"
[[ "$RESTARTS2" == "$RESTARTS1" ]] || hold "stitch worker restarted during stability window"
echo "STITCH_WORKER_STABLE = PASS"
echo "SHARED_IMPORT_RUNTIME = PASS"
echo "BACKGROUND_COORDINATOR_PROCESS_CONTRACT = PASS"

section "6. COMPLETE ZERO-CREDIT RESUME CERTIFICATE"
WORKFLOW_ID="$WORKFLOW_ID" bash "$ROOT/scripts/v3-parallel-runtime-resume-certify.sh"

echo
echo "============================================================"
echo " V3 BACKGROUND RUNTIME REPAIR = PASS"
echo " Existing image reused; no rebuild"
echo " Stitch worker shared imports = PASS"
echo " Server-side scene coordinator worker = STABLE"
echo " Director parallel/background runtime = PASS"
echo " Closed certified Story remains closed"
echo " NO BILLABLE GENERATION EXECUTED"
echo "============================================================"
