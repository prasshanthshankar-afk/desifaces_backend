#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
ROOT="/home/azureuser/workspace/desifaces-v3"
ENV_FILE="$ROOT/infra/.env"
BACKEND_SHA="2bae9efa9afac679ec717156c4a1612063eecd0f"
WEB_SHA="d7e901053e93b5cd7c0cd0a532b65b36720808c0"
WT="/tmp/desifaces-shared-scene-motion-options"
WEB_SCRIPT="/tmp/deploy-shared-scene-motion-web.sh"

DIRECTOR="df-v3-svc-director"
DIRECTOR_WORKER="df-v3-svc-director-worker"
FUSION="df-v3-svc-fusion"
FUSION_WORKER="df-v3-svc-fusion-worker"
WEB="df-web-dev"

fail(){ echo "FAIL: $*" >&2; exit 1; }

resolve_web_root(){
  local candidate remote gitdir
  for candidate in \
    /home/azureuser/workspace/desifaces_web \
    /home/azureuser/workspace/desifaces-web \
    /home/azureuser/workspace/desifaces_frontend \
    /home/azureuser/workspace/desifaces-web-review
  do
    [[ -d "$candidate/.git" || -f "$candidate/.git" ]] || continue
    remote="$(git -C "$candidate" remote get-url origin 2>/dev/null || true)"
    if [[ "$remote" == *"prasshanthshankar-afk/desifaces_web"* ]]; then
      printf "%s" "$candidate"
      return 0
    fi
  done
  while IFS= read -r gitdir; do
    candidate="${gitdir%/.git}"
    remote="$(git -C "$candidate" remote get-url origin 2>/dev/null || true)"
    if [[ "$remote" == *"prasshanthshankar-afk/desifaces_web"* ]]; then
      printf "%s" "$candidate"
      return 0
    fi
  done < <(find /home/azureuser/workspace -maxdepth 3 -type d -name .git 2>/dev/null | sort)
  return 1
}

cleanup(){
  set +e
  rm -f "$WEB_SCRIPT"
  if git -C "$ROOT" worktree list --porcelain 2>/dev/null | grep -Fxq "worktree $WT"; then
    git -C "$ROOT" worktree remove --force "$WT" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ -f "$ENV_FILE" ]] || fail "canonical DEV env missing"
for c in "$DIRECTOR" "$DIRECTOR_WORKER" "$FUSION" "$FUSION_WORKER" "$WEB"; do
  docker inspect "$c" >/dev/null 2>&1 || fail "$c missing"
done
WEB_ROOT="$(resolve_web_root || true)"
[[ -n "$WEB_ROOT" ]] || fail "desifaces_web repository not found"

echo "============================================================"
echo " desifaces DEV — SHARED-SCENE MOTION OPTIONS"
echo " backend_sha=$BACKEND_SHA"
echo " web_sha=$WEB_SHA"
echo " database_migration=NONE"
echo " production_touch=NONE"
echo "============================================================"

echo
echo "=== 1. EXACT BACKEND SOURCE + CONTRACT GATES ==="
git -C "$ROOT" fetch --no-tags origin "$BACKEND_SHA"
git -C "$ROOT" cat-file -e "$BACKEND_SHA^{commit}"
if git -C "$ROOT" worktree list --porcelain | grep -Fxq "worktree $WT"; then
  git -C "$ROOT" worktree remove --force "$WT"
elif [[ -e "$WT" ]]; then
  fail "unrecognized backend worktree exists: $WT"
fi
git -C "$ROOT" worktree add --detach "$WT" "$BACKEND_SHA"
[[ "$(git -C "$WT" rev-parse HEAD)" == "$BACKEND_SHA" ]] || fail "backend worktree SHA mismatch"

grep -Fq "SharedSceneVideoSettingsIn" "$WT/services/svc-director/app/app/shared_scene_routes.py"
grep -Fq "shared-scene-video-settings" "$WT/services/svc-director/app/app/shared_scene_routes.py"
grep -Fq "_shared_scene_video_provider" "$WT/services/svc-director/app/app/fusion_input_performance.py"
grep -Fq "omnihuman_v15" "$WT/services/svc-director/app/app/fusion_input_performance.py"
grep -Fq "_build_shared_scene_mask_url" "$WT/services/svc-fusion/app/app/services/providers/omnihuman_adapter.py"
grep -Fq "generated_shared_scene_mask" "$WT/services/svc-fusion/app/app/services/providers/omnihuman_adapter.py"
python3 -m py_compile \
  "$WT/services/svc-director/app/app/shared_scene_routes.py" \
  "$WT/services/svc-director/app/app/fusion_input_performance.py" \
  "$WT/services/svc-fusion/app/app/services/providers/omnihuman_adapter.py"
echo "SHARED_SCENE_MOTION_SOURCE_CONTRACT=PASS"

compose_project_label() {
  local container="$1"
  docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$container" 2>/dev/null || true
}

compose_managed() {
  local project
  project="$(compose_project_label "$1")"
  [[ -n "$project" && "$project" != "<no value>" ]]
}

# Fusion is already a Compose-owned member of the active V3 stack. Use that
# exact project as the adoption target for older unlabeled Director containers.
FUSION_PROJECT="$(compose_project_label "$FUSION")"
[[ -n "$FUSION_PROJECT" && "$FUSION_PROJECT" != "<no value>" ]] || fail "Fusion Compose project missing"
FUSION_WORKER_PROJECT="$(compose_project_label "$FUSION_WORKER")"
[[ -n "$FUSION_WORKER_PROJECT" && "$FUSION_WORKER_PROJECT" != "<no value>" ]] || FUSION_WORKER_PROJECT="$FUSION_PROJECT"

DIRECTOR_PROJECT="$(compose_project_label "$DIRECTOR")"
[[ -n "$DIRECTOR_PROJECT" && "$DIRECTOR_PROJECT" != "<no value>" ]] || DIRECTOR_PROJECT="$FUSION_PROJECT"
DIRECTOR_WORKER_PROJECT="$(compose_project_label "$DIRECTOR_WORKER")"
[[ -n "$DIRECTOR_WORKER_PROJECT" && "$DIRECTOR_WORKER_PROJECT" != "<no value>" ]] || DIRECTOR_WORKER_PROJECT="$FUSION_PROJECT"

echo "DIRECTOR_PROJECT=$DIRECTOR_PROJECT managed=$(compose_managed "$DIRECTOR" && echo yes || echo no)"
echo "DIRECTOR_WORKER_PROJECT=$DIRECTOR_WORKER_PROJECT managed=$(compose_managed "$DIRECTOR_WORKER" && echo yes || echo no)"
echo "FUSION_PROJECT=$FUSION_PROJECT managed=$(compose_managed "$FUSION" && echo yes || echo no)"
echo "FUSION_WORKER_PROJECT=$FUSION_WORKER_PROJECT managed=$(compose_managed "$FUSION_WORKER" && echo yes || echo no)"

echo
echo "=== 2. BUILD DIRECTOR + FUSION API/WORKERS ==="
cd "$WT"

# Build the four target images. Project selection affects Compose ownership, not
# the explicit V3 Director image name; Fusion uses its existing project image.
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$FUSION_PROJECT" --profile v3-orchestration --profile v3-execution \
  build svc-director svc-director-worker svc-fusion svc-fusion-worker
echo "SHARED_SCENE_MOTION_IMAGES=PASS"

# Recreate every service through its owning Compose project. Older DEV Director
# containers may predate Compose labels; adopt those once into the canonical
# V3 Compose project by temporarily renaming the exact running container.
recover_stranded_adoption() {
  local canonical="$1"
  local rollback_name="$2"
  if ! docker inspect "$rollback_name" >/dev/null 2>&1; then
    return 0
  fi

  local canonical_state=""
  if docker inspect "$canonical" >/dev/null 2>&1; then
    canonical_state="$(docker inspect -f '{{.State.Status}}' "$canonical" 2>/dev/null || true)"
  fi

  if [[ "$canonical_state" == "running" ]]; then
    docker rm -f "$rollback_name" >/dev/null 2>&1 || true
    echo "STRANDED_ADOPTION_CLEANUP=$canonical canonical_running"
    return 0
  fi

  docker rm -f "$canonical" >/dev/null 2>&1 || true
  docker stop "$rollback_name" >/dev/null 2>&1 || true
  docker rename "$rollback_name" "$canonical"
  docker start "$canonical" >/dev/null
  echo "STRANDED_ADOPTION_RECOVERED=$canonical"
}

adopt_if_unmanaged() {
  local container="$1"
  local rollback_name="$2"
  if compose_managed "$container"; then
    return 0
  fi

  # Renaming a running container does not release its published host ports.
  # Stop first, then rename, so the canonical replacement can bind 18011/etc.
  docker rm -f "$rollback_name" >/dev/null 2>&1 || true
  docker stop "$container" >/dev/null
  docker rename "$container" "$rollback_name"
  echo "ADOPT_UNMANAGED_CONTAINER=$container rollback=$rollback_name stopped_before_rename=YES"
}

restore_if_needed() {
  local canonical="$1"
  local rollback_name="$2"
  local canonical_state=""

  if docker inspect "$canonical" >/dev/null 2>&1; then
    canonical_state="$(docker inspect -f '{{.State.Status}}' "$canonical" 2>/dev/null || true)"
    if [[ "$canonical_state" == "running" ]]; then
      return 0
    fi
    docker rm -f "$canonical" >/dev/null 2>&1 || true
  fi

  if docker inspect "$rollback_name" >/dev/null 2>&1; then
    docker stop "$rollback_name" >/dev/null 2>&1 || true
    docker rename "$rollback_name" "$canonical" >/dev/null 2>&1 || true
    docker start "$canonical" >/dev/null 2>&1 || true
  fi
}

DIRECTOR_ROLLBACK="df-v3-svc-director-pre-motion"
DIRECTOR_WORKER_ROLLBACK="df-v3-svc-director-worker-pre-motion"
FUSION_ROLLBACK="df-v3-svc-fusion-pre-motion"
FUSION_WORKER_ROLLBACK="df-v3-svc-fusion-worker-pre-motion"

# Self-heal any partial adoption left by an interrupted/failed earlier run.
recover_stranded_adoption "$DIRECTOR" "$DIRECTOR_ROLLBACK"
recover_stranded_adoption "$DIRECTOR_WORKER" "$DIRECTOR_WORKER_ROLLBACK"
recover_stranded_adoption "$FUSION" "$FUSION_ROLLBACK"
recover_stranded_adoption "$FUSION_WORKER" "$FUSION_WORKER_ROLLBACK"

DIRECTOR_ADOPTED=0
DIRECTOR_WORKER_ADOPTED=0
FUSION_ADOPTED=0
FUSION_WORKER_ADOPTED=0

if ! compose_managed "$DIRECTOR"; then adopt_if_unmanaged "$DIRECTOR" "$DIRECTOR_ROLLBACK"; DIRECTOR_ADOPTED=1; fi
if ! compose_managed "$DIRECTOR_WORKER"; then adopt_if_unmanaged "$DIRECTOR_WORKER" "$DIRECTOR_WORKER_ROLLBACK"; DIRECTOR_WORKER_ADOPTED=1; fi
if ! compose_managed "$FUSION"; then adopt_if_unmanaged "$FUSION" "$FUSION_ROLLBACK"; FUSION_ADOPTED=1; fi
if ! compose_managed "$FUSION_WORKER"; then adopt_if_unmanaged "$FUSION_WORKER" "$FUSION_WORKER_ROLLBACK"; FUSION_WORKER_ADOPTED=1; fi

set +e
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$DIRECTOR_PROJECT" \
  up -d --no-deps --force-recreate svc-director
RC_DIRECTOR=$?

V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$DIRECTOR_WORKER_PROJECT" --profile v3-orchestration \
  up -d --no-deps --force-recreate svc-director-worker
RC_DIRECTOR_WORKER=$?

V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$FUSION_PROJECT" \
  up -d --no-deps --force-recreate svc-fusion
RC_FUSION=$?

V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$FUSION_WORKER_PROJECT" --profile v3-execution \
  up -d --no-deps --force-recreate svc-fusion-worker
RC_FUSION_WORKER=$?
set -e

if (( RC_DIRECTOR != 0 || RC_DIRECTOR_WORKER != 0 || RC_FUSION != 0 || RC_FUSION_WORKER != 0 )); then
  (( DIRECTOR_ADOPTED )) && restore_if_needed "$DIRECTOR" "$DIRECTOR_ROLLBACK"
  (( DIRECTOR_WORKER_ADOPTED )) && restore_if_needed "$DIRECTOR_WORKER" "$DIRECTOR_WORKER_ROLLBACK"
  (( FUSION_ADOPTED )) && restore_if_needed "$FUSION" "$FUSION_ROLLBACK"
  (( FUSION_WORKER_ADOPTED )) && restore_if_needed "$FUSION_WORKER" "$FUSION_WORKER_ROLLBACK"
  fail "one or more DEV service recreations failed"
fi

for c in "$DIRECTOR" "$DIRECTOR_WORKER" "$FUSION" "$FUSION_WORKER"; do
  [[ "$(docker inspect -f '{{.State.Status}}' "$c")" == "running" ]] || fail "$c not running"
done

echo
echo "=== 3. RUNTIME CONTRACT ==="
docker exec -i "$DIRECTOR" python - <<'PY'
import inspect
from app import shared_scene_routes
from app import fusion_input_performance
s1=inspect.getsource(shared_scene_routes)
s2=inspect.getsource(fusion_input_performance)
assert "SharedSceneVideoSettingsIn" in s1
assert "shared-scene-video-settings" in s1
assert "_shared_scene_video_provider" in s2
assert "omnihuman_v15" in s2
print("DIRECTOR_SHARED_SCENE_MOTION_RUNTIME=PASS")
PY

docker exec -i "$FUSION_WORKER" python - <<'PY'
import inspect, os
from app.services.providers.omnihuman_adapter import OmniHumanAdapter
src=inspect.getsource(OmniHumanAdapter)
assert "_build_shared_scene_mask_url" in src
assert "generated_shared_scene_mask" in src
assert bool(os.getenv("FAL_KEY") or os.getenv("DF_FAL_KEY"))
a=OmniHumanAdapter()
assert a.provider_name=="omnihuman_v15"
print("OMNIHUMAN_SHARED_SCENE_MASK_RUNTIME=PASS")
print("FAL_KEY_BOUND=PASS")
PY

docker exec -i "$FUSION_WORKER" python - <<'PY'
import os
assert bool(os.getenv("SYNC_API_KEY"))
print("SYNC3_FALLBACK_KEY_BOUND=PASS")
PY

# All runtime contracts passed. It is now safe to remove temporary adoption
# backups from pre-Compose DEV containers.
docker rm -f "$DIRECTOR_ROLLBACK" >/dev/null 2>&1 || true
docker rm -f "$DIRECTOR_WORKER_ROLLBACK" >/dev/null 2>&1 || true
docker rm -f "$FUSION_ROLLBACK" >/dev/null 2>&1 || true
docker rm -f "$FUSION_WORKER_ROLLBACK" >/dev/null 2>&1 || true
echo "DEV_COMPOSE_OWNERSHIP_ADOPTION=PASS"

echo
echo "=== 4. DEPLOY VALIDATED WEB UI ==="
gh api \
  "repos/prasshanthshankar-afk/desifaces_web/contents/scripts/ops/deploy-next3-web-dev.sh?ref=$WEB_SHA" \
  --jq .content | base64 -d > "$WEB_SCRIPT"
chmod 700 "$WEB_SCRIPT"
WEB_REPO_ROOT="$WEB_ROOT" bash "$WEB_SCRIPT" "$WEB_SHA"

docker exec "$WEB" sh -lc "grep -R -F -m1 'Natural motion' /app/.next >/dev/null"
docker exec "$WEB" sh -lc "grep -R -F -m1 'Precise lip-sync' /app/.next >/dev/null"
docker exec "$WEB" sh -lc "grep -R -F -m1 'Save video direction' /app/.next >/dev/null"
docker exec "$WEB" sh -lc "grep -R -F -m1 'shared-scene-video-settings' /app/.next >/dev/null"
echo "SHARED_SCENE_MOTION_UI=PASS"

echo
echo "============================================================"
echo " SHARED-SCENE MOTION OPTIONS DEV DEPLOY COMPLETE"
echo " NATURAL_MOTION=OMNIHUMAN_MASK"
echo " PRECISE_LIPSYNC=SYNC3"
echo " PRODUCTION_TOUCH=NONE"
echo "============================================================"
