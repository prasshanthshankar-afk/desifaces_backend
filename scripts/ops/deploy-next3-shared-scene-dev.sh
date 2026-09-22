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

# Azure VM Run Command executes this script as root while the checked-out DEV
# repository is owned by azureuser. Trust only the two exact DEV worktree paths
# per Git invocation; do not change global/system Git ownership policy.
git_dev(){ git -c safe.directory="$LIVE_ROOT" -c safe.directory="$WORKTREE" "$@"; }

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

if git_dev -C "$LIVE_ROOT" worktree list --porcelain | grep -Fxq "worktree $WORKTREE"; then
  git_dev -C "$LIVE_ROOT" worktree remove --force "$WORKTREE"
elif [[ -e "$WORKTREE" ]]; then
  fail "refusing unrecognized existing path: $WORKTREE"
fi

git_dev -C "$LIVE_ROOT" fetch --no-tags "$REPO_URL" "$BACKEND_SHA"
git_dev -C "$LIVE_ROOT" cat-file -e "$BACKEND_SHA^{commit}"
git_dev -C "$LIVE_ROOT" worktree add --detach "$WORKTREE" "$BACKEND_SHA"
[[ "$(git_dev -C "$WORKTREE" rev-parse HEAD)" == "$BACKEND_SHA" ]] || fail "worktree SHA mismatch"

cleanup(){
  set +e
  if git_dev -C "$LIVE_ROOT" worktree list --porcelain 2>/dev/null | grep -Fxq "worktree $WORKTREE"; then
    git_dev -C "$LIVE_ROOT" worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

cd "$WORKTREE"
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh config >/tmp/desifaces-next3-compose.yml
echo "DEV_COMPOSE_PREFLIGHT=PASS"

SERVICES=(svc-director svc-fusion svc-fusion-extension svc-dashboard svc-face)
CONTAINERS=(df-v3-svc-director df-v3-svc-fusion df-v3-svc-fusion-extension df-v3-svc-dashboard df-v3-svc-face)
WORKER_SERVICES=(svc-director-worker svc-fusion-worker svc-fusion-extension-worker svc-fusion-extension-stitch-worker svc-face-worker)
WORKER_CONTAINERS=(df-v3-svc-director-worker df-v3-svc-fusion-worker df-v3-svc-fusion-extension-worker df-v3-svc-fusion-extension-stitch-worker df-v3-svc-face-worker)

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

# Reuse the Compose project that already owns the DEV V3 containers. The
# source worktree must not create a second project with the same fixed
# container names.
LIVE_PROJECT=""
EXISTING_API_TARGETS=0
for i in "${!CONTAINERS[@]}"; do
  c="${CONTAINERS[$i]}"; svc="${SERVICES[$i]}"
  if ! docker inspect "$c" >/dev/null 2>&1; then
    echo "DEV_TARGET_CONTAINER_ABSENT=$c"
    continue
  fi
  EXISTING_API_TARGETS=$((EXISTING_API_TARGETS+1))
  project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$c")"
  owner_service="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$c")"
  [[ -n "$project" ]] || fail "compose project label missing for $c"
  [[ "$owner_service" == "$svc" ]] || fail "compose service ownership mismatch for $c: $owner_service != $svc"
  if [[ -z "$LIVE_PROJECT" ]]; then
    LIVE_PROJECT="$project"
  else
    [[ "$project" == "$LIVE_PROJECT" ]] || fail "target containers span multiple Compose projects"
  fi
done
(( EXISTING_API_TARGETS > 0 )) || fail "no existing DEV V3 API container available to establish Compose ownership"
for i in "${!WORKER_CONTAINERS[@]}"; do
  c="${WORKER_CONTAINERS[$i]}"; svc="${WORKER_SERVICES[$i]}"
  [[ "${ACTIVE_WORKER[$c]:-0}" == "1" ]] || continue
  project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$c")"
  owner_service="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$c")"
  [[ "$project" == "$LIVE_PROJECT" ]] || fail "worker Compose project mismatch for $c"
  [[ "$owner_service" == "$svc" ]] || fail "worker Compose service mismatch for $c: $owner_service != $svc"
done
[[ -n "$LIVE_PROJECT" ]] || fail "unable to resolve live DEV Compose project"
echo "DEV_COMPOSE_PROJECT=$LIVE_PROJECT"
echo "DEV_COMPOSE_OWNERSHIP=PASS"

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
    V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$LIVE_PROJECT" up -d --no-deps --force-recreate "${SERVICES[@]}" >/dev/null 2>&1 || true
    for i in "${!WORKER_SERVICES[@]}"; do
      svc="${WORKER_SERVICES[$i]}"; c="${WORKER_CONTAINERS[$i]}"
      [[ "${ACTIVE_WORKER[$c]:-0}" == "1" ]] || continue
      if [[ "$svc" == "svc-director-worker" ]]; then
        V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$LIVE_PROJECT" --profile v3-orchestration up -d --no-deps --force-recreate "$svc" >/dev/null 2>&1 || true
      else
        V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$LIVE_PROJECT" --profile v3-execution up -d --no-deps --force-recreate "$svc" >/dev/null 2>&1 || true
      fi
    done
  fi
  echo "DEV_ROLLBACK=ATTEMPTED"
  exit "$rc"
}
trap 'rollback' ERR

V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$LIVE_PROJECT" build "${SERVICES[@]}"
echo "NEXT3_IMAGE_BUILD=PASS"

RUNTIME_CHANGED=1
# These containers have already passed exact project/service ownership checks.
# Remove only this bounded #next3 target set so Compose can recreate the
# fixed-name DEV containers from the newly built images.
for c in "${CONTAINERS[@]}"; do
  if docker inspect "$c" >/dev/null 2>&1; then
    docker rm -f "$c" >/dev/null
  fi
done
echo "NEXT3_TARGET_API_CONTAINERS_REMOVED=PASS"
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$LIVE_PROJECT" up -d --no-deps --force-recreate "${SERVICES[@]}"

for i in "${!WORKER_SERVICES[@]}"; do
  svc="${WORKER_SERVICES[$i]}"; c="${WORKER_CONTAINERS[$i]}"
  [[ "${ACTIVE_WORKER[$c]:-0}" == "1" ]] || continue
  docker rm -f "$c" >/dev/null
  if [[ "$svc" == "svc-director-worker" ]]; then
    V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$LIVE_PROJECT" --profile v3-orchestration up -d --no-deps --force-recreate "$svc"
  else
    V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$LIVE_PROJECT" --profile v3-execution up -d --no-deps --force-recreate "$svc"
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
from desifaces_shared.safety import evaluate_text_policy, evaluate_texts_policy
paths={getattr(r,"path","") for r in app.routes}
required={
 "/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/shared-scene",
 "/api/director/studio-workflows/{workflow_id}/participants/{participant_id}/shared-scene-profile",
 "/api/director/runs",
}
missing=sorted(required-paths)
assert not missing, missing
from app.studio_workflow import build_story_studio_workflow, build_shared_scene_studio_workflow
from app.audio_execution import evaluate_text_policy as audio_boundary_policy
assert callable(build_story_studio_workflow)
assert callable(build_shared_scene_studio_workflow)
assert callable(evaluate_text_policy)
assert callable(evaluate_texts_policy)
assert callable(audio_boundary_policy)
print("NEXT3_DIRECTOR_RUNTIME_CONTRACT=PASS")
print("NEXT3_SHARED_SCENE_SPEAKER_PROFILE=PASS")
print("DIRECTOR_SHARED_CONTENT_SAFETY=PASS")
print("AUDIO_DIALOGUE_SAFETY_RUNTIME=PASS")
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

docker exec df-v3-svc-face python - <<'PY'
import os
from app.main import app
paths={getattr(r,"path","") for r in app.routes}
assert "/api/face/creator/group-photo/validate" in paths
assert "/api/face/creator/group-photo/validate-asset" in paths
from desifaces_shared.safety import (
    SafetyDecision,
    SafetyFinding,
    SafetyStatus,
    evaluate_text_policy,
)
from app.services.group_photo_quality import (
    analyze_group_photo,
    USABLE_FACE_MIN_HEIGHT_RATIO,
    USABLE_FACE_MIN_WIDTH_RATIO,
    USABLE_FACE_MIN_AREA_RATIO,
)
from app.services.product_visual_policy import evaluate_product_visual_policy, _SCHEMA
assert SafetyDecision and SafetyFinding and SafetyStatus
assert callable(analyze_group_photo)
assert 0 < USABLE_FACE_MIN_HEIGHT_RATIO < 1
assert 0 < USABLE_FACE_MIN_WIDTH_RATIO < 1
assert 0 < USABLE_FACE_MIN_AREA_RATIO < 1
assert callable(evaluate_text_policy)
assert callable(evaluate_product_visual_policy)
assert "child_abuse_or_exploitation" in _SCHEMA["properties"]
assert bool(os.getenv("OPENAI_API_KEY"))
print("NEXT3_GROUP_PHOTO_VALIDATION_RUNTIME=PASS")
print("GROUP_PHOTO_USABLE_FACE_FILTER=PASS")
print("SHARED_CONTENT_SAFETY_RUNTIME=PASS")
print("PRODUCT_VISUAL_POLICY_RUNTIME=PASS")
print("PRODUCT_VISUAL_CHILD_ABUSE_POLICY=PASS")
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
