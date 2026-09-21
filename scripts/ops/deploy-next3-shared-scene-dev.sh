#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_SHA="${1:-}"
EXPECTED_HOST="desifaces-dev"
LIVE_ROOT="/home/azureuser/workspace/desifaces-v3"
WORKTREE="/home/azureuser/workspace/desifaces-v3-next3-runtime"
ENV_FILE="$LIVE_ROOT/infra/.env"
REPO_URL="https://github.com/prasshanthshankar-afk/desifaces_backend.git"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ "$BACKEND_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "exact backend SHA required"
[[ -d "$LIVE_ROOT/.git" || -f "$LIVE_ROOT/.git" ]] || fail "V3 live source repository not found"
[[ -f "$ENV_FILE" ]] || fail "V3 runtime env not found"
command -v docker >/dev/null || fail "docker missing"
command -v git >/dev/null || fail "git missing"

echo "============================================================"
echo " desifaces #next3 — DEV BACKEND DEPLOY"
echo " host=$(hostname -s)"
echo " backend_sha=$BACKEND_SHA"
echo " target=desifaces-dev"
echo " production_touch=NONE"
echo " database_migration=NONE"
echo "============================================================"

python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
vals={}
for raw in p.read_text().splitlines():
    s=raw.strip()
    if not s or s.startswith("#") or "=" not in s:
        continue
    k,v=s.split("=",1)
    vals[k.strip()]=v.strip()
required=("POSTGRES_DB","DATABASE_URL","JWT_SECRET","SYNC_API_KEY")
missing=[k for k in required if not vals.get(k)]
if missing:
    raise SystemExit("missing required DEV runtime config: "+",".join(missing))
if vals.get("POSTGRES_DB") != "desifaces_v3":
    raise SystemExit("refusing non-V3 database identity")
print("DEV_RUNTIME_CONFIG=PASS")
print("SYNC_API_KEY_PRESENT=PASS")
PY

if git -C "$LIVE_ROOT" worktree list --porcelain | grep -Fxq "worktree $WORKTREE"; then
  git -C "$LIVE_ROOT" worktree remove --force "$WORKTREE"
elif [[ -e "$WORKTREE" ]]; then
  fail "refusing unrecognized existing path: $WORKTREE"
fi

git -C "$LIVE_ROOT" fetch --no-tags "$REPO_URL" "$BACKEND_SHA"
git -C "$LIVE_ROOT" cat-file -e "$BACKEND_SHA^{commit}"
git -C "$LIVE_ROOT" worktree add --detach "$WORKTREE" "$BACKEND_SHA"
[[ "$(git -C "$WORKTREE" rev-parse HEAD)" == "$BACKEND_SHA" ]] || fail "worktree SHA mismatch"

cleanup(){
  set +e
  if git -C "$LIVE_ROOT" worktree list --porcelain 2>/dev/null | grep -Fxq "worktree $WORKTREE"; then
    git -C "$LIVE_ROOT" worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

cd "$WORKTREE"
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh config >/tmp/desifaces-next3-compose.yml
echo "DEV_COMPOSE_PREFLIGHT=PASS"

SERVICES=(svc-director svc-fusion svc-fusion-extension svc-dashboard)
CONTAINERS=(df-v3-svc-director df-v3-svc-fusion df-v3-svc-fusion-extension df-v3-svc-dashboard)
WORKER_SERVICES=(svc-director-worker svc-fusion-worker svc-fusion-extension-worker svc-fusion-extension-stitch-worker)
WORKER_CONTAINERS=(df-v3-svc-director-worker df-v3-svc-fusion-worker df-v3-svc-fusion-extension-worker df-v3-svc-fusion-extension-stitch-worker)

declare -A OLD_IMAGE_ID OLD_IMAGE_REF ACTIVE_WORKER
snapshot_container(){
  local c="$1"
  if docker inspect "$c" >/dev/null 2>&1; then
    OLD_IMAGE_ID["$c"]="$(docker inspect -f '{{.Image}}' "$c")"
    OLD_IMAGE_REF["$c"]="$(docker inspect -f '{{.Config.Image}}' "$c")"
  fi
}
for c in "${CONTAINERS[@]}" "${WORKER_CONTAINERS[@]}"; do snapshot_container "$c"; done
for i in "${!WORKER_CONTAINERS[@]}"; do
  c="${WORKER_CONTAINERS[$i]}"
  if docker inspect "$c" >/dev/null 2>&1; then ACTIVE_WORKER["$c"]=1; else ACTIVE_WORKER["$c"]=0; fi
done

RUNTIME_CHANGED=0
rollback(){
  local rc=$?
  if (( rc == 0 )); then return 0; fi
  set +e
  echo "===== DEV AUTOMATIC ROLLBACK ====="
  for c in "${!OLD_IMAGE_ID[@]}"; do
    ref="${OLD_IMAGE_REF[$c]}"
    id="${OLD_IMAGE_ID[$c]}"
    [[ -n "$ref" && -n "$id" ]] && docker tag "$id" "$ref" >/dev/null 2>&1 || true
  done
  if (( RUNTIME_CHANGED == 1 )); then
    V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh up -d --no-deps --force-recreate "${SERVICES[@]}" >/dev/null 2>&1 || true
    for i in "${!WORKER_SERVICES[@]}"; do
      svc="${WORKER_SERVICES[$i]}"; c="${WORKER_CONTAINERS[$i]}"
      [[ "${ACTIVE_WORKER[$c]:-0}" == "1" ]] || continue
      if [[ "$svc" == "svc-director-worker" ]]; then
        V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh --profile v3-orchestration up -d --no-deps --force-recreate "$svc" >/dev/null 2>&1 || true
      else
        V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh --profile v3-execution up -d --no-deps --force-recreate "$svc" >/dev/null 2>&1 || true
      fi
    done
  fi
  echo "DEV_ROLLBACK=ATTEMPTED"
  exit "$rc"
}
trap 'rollback' ERR

V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh build "${SERVICES[@]}"
echo "NEXT3_IMAGE_BUILD=PASS"

RUNTIME_CHANGED=1
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh up -d --no-deps --force-recreate "${SERVICES[@]}"

for i in "${!WORKER_SERVICES[@]}"; do
  svc="${WORKER_SERVICES[$i]}"; c="${WORKER_CONTAINERS[$i]}"
  [[ "${ACTIVE_WORKER[$c]:-0}" == "1" ]] || continue
  if [[ "$svc" == "svc-director-worker" ]]; then
    V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh --profile v3-orchestration up -d --no-deps --force-recreate "$svc"
  else
    V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh --profile v3-execution up -d --no-deps --force-recreate "$svc"
  fi
done

for _ in $(seq 1 60); do
  ready=1
  for c in "${CONTAINERS[@]}"; do
    state="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)"
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$c" 2>/dev/null || true)"
    [[ "$state" == "running" ]] || ready=0
    [[ "$health" == "healthy" || "$health" == "no-healthcheck" ]] || ready=0
  done
  (( ready == 1 )) && break
  sleep 2
done
(( ready == 1 )) || fail "DEV API containers did not become ready"

docker exec df-v3-svc-director python - <<'PY'
from app.main import app
paths={getattr(r,"path","") for r in app.routes}
required={
 "/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/shared-scene",
}
missing=sorted(required-paths)
assert not missing, missing
from app.studio_workflow import build_story_studio_workflow, build_shared_scene_studio_workflow
assert callable(build_story_studio_workflow)
assert callable(build_shared_scene_studio_workflow)
print("NEXT3_DIRECTOR_RUNTIME_CONTRACT=PASS")
print("LEGACY_STUDIO_WORKFLOW_PRESENT=PASS")
PY

docker exec df-v3-svc-fusion python - <<'PY'
from app.services.providers.sync3_adapter import Sync3Adapter
adapter=Sync3Adapter()
assert adapter.provider_name=="sync3"
assert adapter.model=="sync-3" or bool(adapter.model)
assert bool(adapter.api_key)
print("SYNC3_ADAPTER_RUNTIME=PASS")
print("SYNC_API_KEY_BOUND=PASS")
PY

docker exec df-v3-svc-fusion-extension python - <<'PY'
from app.main import app
paths={getattr(r,"path","") for r in app.routes}
assert "/api/longform/v3/scene-stitch" in paths
print("NEXT3_STITCH_RUNTIME_CONTRACT=PASS")
PY

docker exec df-v3-svc-dashboard python - <<'PY'
from app.services.dashboard_service import _library_conversation_mode
assert _library_conversation_mode({"conversation_mode":"shared_scene"}, {}) == "shared_scene"
assert _library_conversation_mode({"conversation_mode":"ordered_speaker_shots"}, {}) == "ordered_speaker_shots"
print("NEXT3_SAVED_WORK_CLASSIFICATION=PASS")
PY

for i in "${!WORKER_CONTAINERS[@]}"; do
  c="${WORKER_CONTAINERS[$i]}"
  [[ "${ACTIVE_WORKER[$c]:-0}" == "1" ]] || continue
  [[ "$(docker inspect -f '{{.State.Status}}' "$c")" == "running" ]] || fail "worker not running after deploy: $c"
done

STATE_DIR="/home/azureuser/.local/state/desifaces-next3"
install -d -o azureuser -g azureuser "$STATE_DIR"
printf '%s\n' "$BACKEND_SHA" > "$STATE_DIR/backend.sha"
chown azureuser:azureuser "$STATE_DIR/backend.sha"

echo "NEXT3_BACKEND_SHA=$BACKEND_SHA"
echo "NEXT3_DEV_BACKEND_DEPLOY=PASS"
echo "PRODUCTION_TOUCH=NONE"
