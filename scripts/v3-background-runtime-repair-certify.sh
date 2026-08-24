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

section "5. WAIT FOR STABLE COORDINATOR START"
READY=0
LAST_RESTARTS=""
for _ in $(seq 1 30); do
  status="$(docker inspect -f '{{.State.Status}}' "$CID")"
  running="$(docker inspect -f '{{.State.Running}}' "$CID")"
  restarting="$(docker inspect -f '{{.State.Restarting}}' "$CID")"
  oom="$(docker inspect -f '{{.State.OOMKilled}}' "$CID")"
  restarts="$(docker inspect -f '{{.RestartCount}}' "$CID")"
  LAST_RESTARTS="$restarts"
  if [[ "$status" == "running" && "$running" == "true" && "$restarting" == "false" && "$oom" == "false" ]] \
      && "${COMPOSE[@]}" logs --no-color --tail=120 svc-fusion-extension-stitch-worker 2>&1 \
           | grep -Fq 'V3 scene coordinator started'; then
    READY=1
    break
  fi
  sleep 1
done

if [[ "$READY" != "1" ]]; then
  docker inspect -f 'status={{.State.Status}} running={{.State.Running}} restarting={{.State.Restarting}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} restarts={{.RestartCount}} error={{json .State.Error}}' "$CID" || true
  "${COMPOSE[@]}" logs --no-color --tail=160 svc-fusion-extension-stitch-worker >&2 || true
  hold "stitch worker did not reach stable coordinator startup"
fi

echo "stitch_worker_restart_count=$LAST_RESTARTS"
echo "STITCH_WORKER_STABLE = PASS"

section "6. SHARED IMPORT RUNTIME PROOF"
"${COMPOSE[@]}" exec -T svc-fusion-extension-stitch-worker python - <<'PY'
import df_contracts
from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore
print(f"df_contracts_module={df_contracts.__name__}")
print(f"workflow_store={CanonicalStudioWorkflowStore.__name__}")
print("SHARED_IMPORT_RUNTIME = PASS")
PY

section "7. COMPLETE ZERO-CREDIT RESUME CERTIFICATE"
WORKFLOW_ID="$WORKFLOW_ID" bash "$ROOT/scripts/v3-parallel-runtime-resume-certify.sh"

echo
echo "============================================================"
echo " V3 BACKGROUND RUNTIME REPAIR = PASS"
echo " Existing image reused; no rebuild"
echo " Stitch worker shared imports = PASS"
echo " Server-side scene coordinator = RUNNING"
echo " Director parallel/background runtime = PASS"
echo " Closed certified Story remains closed"
echo " NO BILLABLE GENERATION EXECUTED"
echo "============================================================"
