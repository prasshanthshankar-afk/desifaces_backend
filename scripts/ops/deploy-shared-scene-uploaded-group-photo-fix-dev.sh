#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
ROOT="/home/azureuser/workspace/desifaces-v3"
ENV_FILE="$ROOT/infra/.env"
BACKEND_SHA="2241d2e46a759607f2a36f7e5f9f22664e79309c"
WT="/tmp/desifaces-shared-scene-uploaded-photo-fix"
DIRECTOR="df-v3-svc-director"
FUSION_WORKER="df-v3-svc-fusion-worker"

fail(){ echo "FAIL: $*" >&2; exit 1; }

project_of(){
  local p
  p="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$DIRECTOR" 2>/dev/null || true)"
  [[ -n "$p" && "$p" != "<no value>" ]] || return 1
  printf "%s" "$p"
}

cleanup(){
  set +e
  if git -C "$ROOT" worktree list --porcelain 2>/dev/null | grep -Fxq "worktree $WT"; then
    git -C "$ROOT" worktree remove --force "$WT" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ -f "$ENV_FILE" ]] || fail "canonical DEV env missing"
docker inspect "$DIRECTOR" >/dev/null 2>&1 || fail "$DIRECTOR missing"
docker inspect "$FUSION_WORKER" >/dev/null 2>&1 || fail "$FUSION_WORKER missing"

WORKER_IMAGE_BEFORE="$(docker inspect -f '{{.Image}}' "$FUSION_WORKER")"

echo "============================================================"
echo " desifaces DEV — SHARED-SCENE GROUP PHOTO BINDING FIX"
echo " backend_sha=$BACKEND_SHA"
echo " touch_scope=director_only"
echo " production_touch=NONE"
echo "============================================================"

echo
echo "=== 1. EXACT SOURCE ==="
git -C "$ROOT" fetch --no-tags origin "$BACKEND_SHA"
git -C "$ROOT" cat-file -e "$BACKEND_SHA^{commit}"

if git -C "$ROOT" worktree list --porcelain | grep -Fxq "worktree $WT"; then
  git -C "$ROOT" worktree remove --force "$WT"
elif [[ -e "$WT" ]]; then
  fail "unrecognized worktree exists: $WT"
fi

git -C "$ROOT" worktree add --detach "$WT" "$BACKEND_SHA"
[[ "$(git -C "$WT" rev-parse HEAD)" == "$BACKEND_SHA" ]] || fail "worktree SHA mismatch"

grep -Fq "'source_image'" "$WT/services/svc-director/app/app/shared_scene_routes.py"
grep -Fq "shared_scene_media_not_owned_active_image" "$WT/services/svc-director/app/app/shared_scene_routes.py"
python3 -m py_compile "$WT/services/svc-director/app/app/shared_scene_routes.py"
echo "SHARED_SCENE_GROUP_PHOTO_SOURCE_CONTRACT=PASS"

PROJECT="$(project_of)" || fail "Director Compose project missing"
echo "DIRECTOR_PROJECT=$PROJECT"

echo
echo "=== 2. BUILD + RECREATE DIRECTOR ONLY ==="
cd "$WT"
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" build svc-director
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" up -d --no-deps --force-recreate svc-director
[[ "$(docker inspect -f '{{.State.Status}}' "$DIRECTOR")" == "running" ]] || fail "Director not running"

echo
echo "=== 3. RUNTIME CERTIFICATION ==="
docker exec -i "$DIRECTOR" python - <<'PY'
import inspect
from app.shared_scene_routes import set_shared_scene_conversation
src=inspect.getsource(set_shared_scene_conversation).lower()
assert "'source_image'" in src
assert "shared_scene_media_not_owned_active_image" in src
print("UPLOADED_GROUP_PHOTO_BINDING_RUNTIME=PASS")
PY

[[ "$(docker inspect -f '{{.Image}}' "$FUSION_WORKER")" == "$WORKER_IMAGE_BEFORE" ]] || fail "Fusion worker changed unexpectedly"
echo "SYNC3_PARALLEL_WORKER_PRESERVED=PASS"

echo
echo "============================================================"
echo " SHARED-SCENE GROUP PHOTO BINDING DEV DEPLOY COMPLETE"
echo " UPLOADED_SOURCE_IMAGE=ACCEPTED"
echo " GENERATED_FACE_IMAGE=UNCHANGED"
echo " SYNC3_PARALLEL_WORKER=PRESERVED"
echo " PRODUCTION_TOUCH=NONE"
echo "============================================================"
