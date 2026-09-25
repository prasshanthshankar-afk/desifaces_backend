#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
ROOT="/home/azureuser/workspace/desifaces-v3"
ENV_FILE="$ROOT/infra/.env"
BACKEND_SHA="32a44fc9b2201cd93ca71e9ae858ee76b151f8a5"
WEB_SHA="6129900d1267f41792cdb7c6bd121ca5f7be82f5"
WT="/tmp/desifaces-launch-video-path"
WEB_SCRIPT="/tmp/deploy-next3-launch-video-web.sh"

DIRECTOR="df-v3-svc-director"
FUSION="df-v3-svc-fusion"
FUSION_WORKER="df-v3-svc-fusion-worker"
STITCH_WORKER="df-v3-svc-fusion-extension-stitch-worker"
WEB="df-web-dev"

fail(){ echo "FAIL: $*" >&2; exit 1; }

project_of(){
  local c="$1" p
  p="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$c" 2>/dev/null || true)"
  [[ -n "$p" && "$p" != "<no value>" ]] || return 1
  printf "%s" "$p"
}

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
    if [[ "$remote" == *"prasshanthshankar-afk/desifaces_web"* ]]; then printf "%s" "$candidate"; return 0; fi
  done
  while IFS= read -r gitdir; do
    candidate="${gitdir%/.git}"
    remote="$(git -C "$candidate" remote get-url origin 2>/dev/null || true)"
    if [[ "$remote" == *"prasshanthshankar-afk/desifaces_web"* ]]; then printf "%s" "$candidate"; return 0; fi
  done < <(find /home/azureuser/workspace -maxdepth 4 -type d -name .git 2>/dev/null | sort)
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
for c in "$DIRECTOR" "$FUSION" "$FUSION_WORKER" "$STITCH_WORKER" "$WEB"; do
  docker inspect "$c" >/dev/null 2>&1 || fail "$c missing"
done

FUSION_IMAGE_BEFORE="$(docker inspect -f '{{.Image}}' "$FUSION")"
FUSION_WORKER_IMAGE_BEFORE="$(docker inspect -f '{{.Image}}' "$FUSION_WORKER")"
STITCH_IMAGE_BEFORE="$(docker inspect -f '{{.Image}}' "$STITCH_WORKER")"

echo "============================================================"
echo " desifaces DEV — LAUNCH VIDEO PATH"
echo " backend_sha=$BACKEND_SHA"
echo " web_sha=$WEB_SHA"
echo " launch_default=precise_lipsync_sync3"
echo " natural_motion=feature_flag_only"
echo " touch_scope=director+web"
echo " production_touch=NONE"
echo "============================================================"

echo
echo "=== 1. EXACT SOURCE ==="
git -C "$ROOT" fetch --no-tags origin "$BACKEND_SHA"
git -C "$ROOT" cat-file -e "$BACKEND_SHA^{commit}"
if git -C "$ROOT" worktree list --porcelain | grep -Fxq "worktree $WT"; then
  git -C "$ROOT" worktree remove --force "$WT"
elif [[ -e "$WT" ]]; then
  fail "unrecognized backend worktree exists: $WT"
fi
git -C "$ROOT" worktree add --detach "$WT" "$BACKEND_SHA"
[[ "$(git -C "$WT" rev-parse HEAD)" == "$BACKEND_SHA" ]] || fail "backend worktree SHA mismatch"

grep -Fq 'DF_OMNIHUMAN_SHARED_SCENE_ENABLED' "$WT/services/svc-director/app/app/shared_scene_routes.py"
grep -Fq 'natural_motion_temporarily_unavailable' "$WT/services/svc-director/app/app/shared_scene_routes.py"
grep -Fq 'motion_mode: Literal["natural_motion", "precise_lipsync"] = "precise_lipsync"' "$WT/services/svc-director/app/app/shared_scene_routes.py"
python3 -m py_compile "$WT/services/svc-director/app/app/shared_scene_routes.py"
echo "LAUNCH_VIDEO_BACKEND_SOURCE=PASS"

DIR_PROJECT="$(project_of "$DIRECTOR")" || fail "Director Compose project missing"
echo "DIRECTOR_PROJECT=$DIR_PROJECT"

echo
echo "=== 2. DEPLOY DIRECTOR CONTROL-PLANE ONLY ==="
cd "$WT"
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$DIR_PROJECT" build svc-director
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$DIR_PROJECT" up -d --no-deps --force-recreate svc-director
[[ "$(docker inspect -f '{{.State.Status}}' "$DIRECTOR")" == "running" ]] || fail "Director not running"

docker exec -i "$DIRECTOR" python - <<'PY'
import inspect, os
from app import shared_scene_routes
src=inspect.getsource(shared_scene_routes)
assert "natural_motion_temporarily_unavailable" in src
assert os.getenv("DF_OMNIHUMAN_SHARED_SCENE_ENABLED", "0").strip().lower() not in {"1","true","yes","on"}
print("NATURAL_MOTION_LAUNCH_GUARD=PASS")
print("DEFAULT_VIDEO_PROVIDER=SYNC3")
PY

echo
echo "=== 3. PROVE GENERATION RUNTIMES UNTOUCHED ==="
[[ "$(docker inspect -f '{{.Image}}' "$FUSION")" == "$FUSION_IMAGE_BEFORE" ]] || fail "Fusion API image changed unexpectedly"
[[ "$(docker inspect -f '{{.Image}}' "$FUSION_WORKER")" == "$FUSION_WORKER_IMAGE_BEFORE" ]] || fail "Fusion worker image changed unexpectedly"
[[ "$(docker inspect -f '{{.Image}}' "$STITCH_WORKER")" == "$STITCH_IMAGE_BEFORE" ]] || fail "Stitch worker image changed unexpectedly"
echo "CURRENT_GENERATION_RUNTIME_UNTOUCHED=PASS"

echo
echo "=== 4. DEPLOY WEB LAUNCH UX ==="
WEB_ROOT="$(resolve_web_root || true)"
[[ -n "$WEB_ROOT" ]] || fail "desifaces_web repository not found"
gh api \
  "repos/prasshanthshankar-afk/desifaces_web/contents/scripts/ops/deploy-next3-web-dev.sh?ref=$WEB_SHA" \
  --jq .content | base64 -d > "$WEB_SCRIPT"
chmod 700 "$WEB_SCRIPT"
WEB_REPO_ROOT="$WEB_ROOT" bash "$WEB_SCRIPT" "$WEB_SHA"

docker exec "$WEB" sh -lc "grep -R -F -m1 'Precise lip-sync is the current launch mode' /app/.next >/dev/null"
echo "LAUNCH_VIDEO_WEB_UX=PASS"

echo
echo "============================================================"
echo " LAUNCH VIDEO PATH DEV DEPLOY COMPLETE"
echo " DEFAULT_VIDEO_MODE=PRECISE_LIPSYNC_SYNC3"
echo " NATURAL_MOTION=FEATURE_FLAG_ONLY"
echo " CURRENT_OMNIHUMAN_JOB=UNTOUCHED"
echo " SMOOTH_STITCH_RUNTIME=PRESERVED"
echo " PRODUCTION_TOUCH=NONE"
echo "============================================================"
