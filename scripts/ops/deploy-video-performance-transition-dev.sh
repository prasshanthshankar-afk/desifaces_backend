#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
ROOT="/home/azureuser/workspace/desifaces-v3"
ENV_FILE="$ROOT/infra/.env"
BACKEND_SHA="96b0bc1a9b95f6cdc7e0b06b773ef39f8d4d0752"
WEB_SHA="207d46e730101f1534a4ab2bb338fd68bba30291"
WT="/tmp/desifaces-video-perf-transition"
WEB_SCRIPT="/tmp/deploy-next3-video-perf-transition-web.sh"

DIRECTOR="df-v3-svc-director"
FUSION="df-v3-svc-fusion"
FUSION_WORKER="df-v3-svc-fusion-worker"
EXT="df-v3-svc-fusion-extension"
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
for c in "$DIRECTOR" "$FUSION" "$FUSION_WORKER" "$EXT" "$STITCH_WORKER" "$WEB"; do
  docker inspect "$c" >/dev/null 2>&1 || fail "$c missing"
done

echo "============================================================"
echo " desifaces DEV — VIDEO PERFORMANCE + SMOOTH HANDOFF"
echo " backend_sha=$BACKEND_SHA"
echo " web_sha=$WEB_SHA"
echo " database_migration=NONE"
echo " production_touch=NONE"
echo "============================================================"

echo
echo "=== 0. ACTIVE JOB GUARD ==="
ACTIVE_FUSION_JOBS="$(docker exec -i "$FUSION" python - <<'PY'
import asyncio, os, asyncpg
async def main():
    conn=await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        n=await conn.fetchval("""
          select count(*)
          from public.studio_jobs
          where studio_type='fusion'
            and lower(coalesce(status,'')) not in ('succeeded','failed','blocked','canceled','cancelled')
        """)
        print(int(n or 0))
    finally:
        await conn.close()
asyncio.run(main())
PY
)"
echo "ACTIVE_FUSION_JOBS=$ACTIVE_FUSION_JOBS"
[[ "$ACTIVE_FUSION_JOBS" =~ ^[0-9]+$ ]] || fail "could not determine active Fusion jobs"
(( ACTIVE_FUSION_JOBS == 0 )) || fail "active Fusion jobs exist; wait for them to finish before runtime replacement"
echo "ACTIVE_FUSION_JOB_GUARD=PASS"

echo
echo "=== 1. EXACT SOURCE + CONTRACT GATES ==="
git -C "$ROOT" fetch --no-tags origin "$BACKEND_SHA"
git -C "$ROOT" cat-file -e "$BACKEND_SHA^{commit}"
if git -C "$ROOT" worktree list --porcelain | grep -Fxq "worktree $WT"; then
  git -C "$ROOT" worktree remove --force "$WT"
elif [[ -e "$WT" ]]; then
  fail "unrecognized backend worktree exists: $WT"
fi
git -C "$ROOT" worktree add --detach "$WT" "$BACKEND_SHA"
[[ "$(git -C "$WT" rev-parse HEAD)" == "$BACKEND_SHA" ]] || fail "backend worktree SHA mismatch"

grep -Fq "shared_input_cache_hit" "$WT/services/svc-fusion/app/app/services/providers/omnihuman_adapter.py"
grep -Fq "asyncio.gather(face_upload, audio_upload)" "$WT/services/svc-fusion/app/app/services/providers/omnihuman_adapter.py"
grep -Fq 'DF_FUSION_WORKER_CONCURRENCY: ${DF_FUSION_WORKER_CONCURRENCY:-8}' "$WT/docker-compose.v3.yml"
grep -Fq "conversation_handoff" "$WT/services/svc-fusion-extension/app/app/services/stitch_service.py"
grep -Fq 'stitch_mode = "conversation_handoff" if conversation_mode == "shared_scene" else None' "$WT/services/svc-fusion-extension/app/app/workers/v3_scene_coordinator.py"
grep -Fq 'motion_mode: Literal["natural_motion", "precise_lipsync"] = "precise_lipsync"' "$WT/services/svc-director/app/app/shared_scene_routes.py"
python3 -m py_compile \
  "$WT/services/svc-fusion/app/app/services/providers/omnihuman_adapter.py" \
  "$WT/services/svc-fusion-extension/app/app/services/stitch_service.py" \
  "$WT/services/svc-fusion-extension/app/app/api/routes/v3_scene_stitch.py" \
  "$WT/services/svc-fusion-extension/app/app/workers/v3_scene_coordinator.py" \
  "$WT/services/svc-director/app/app/shared_scene_routes.py"
echo "VIDEO_PERF_TRANSITION_SOURCE_CONTRACT=PASS"

DIR_PROJECT="$(project_of "$DIRECTOR")" || fail "Director Compose project missing"
FUSION_PROJECT="$(project_of "$FUSION")" || fail "Fusion Compose project missing"
FUSION_WORKER_PROJECT="$(project_of "$FUSION_WORKER")" || fail "Fusion worker Compose project missing"
EXT_PROJECT="$(project_of "$EXT")" || fail "Fusion Extension Compose project missing"
STITCH_PROJECT="$(project_of "$STITCH_WORKER")" || fail "Stitch worker Compose project missing"
echo "DIRECTOR_PROJECT=$DIR_PROJECT"
echo "FUSION_PROJECT=$FUSION_PROJECT"
echo "FUSION_WORKER_PROJECT=$FUSION_WORKER_PROJECT"
echo "EXT_PROJECT=$EXT_PROJECT"
echo "STITCH_PROJECT=$STITCH_PROJECT"

echo
echo "=== 2. BUILD ONLY AFFECTED BACKEND SERVICES ==="
cd "$WT"
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$DIR_PROJECT" build svc-director
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$FUSION_PROJECT" --profile v3-execution build svc-fusion svc-fusion-worker
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$EXT_PROJECT" --profile v3-execution build svc-fusion-extension svc-fusion-extension-stitch-worker
echo "VIDEO_PERF_TRANSITION_IMAGES=PASS"

echo
echo "=== 3. RECREATE AFFECTED DEV RUNTIMES ==="
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$DIR_PROJECT" up -d --no-deps --force-recreate svc-director
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$FUSION_PROJECT" up -d --no-deps --force-recreate svc-fusion
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$FUSION_WORKER_PROJECT" --profile v3-execution up -d --no-deps --force-recreate svc-fusion-worker
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$EXT_PROJECT" up -d --no-deps --force-recreate svc-fusion-extension
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$STITCH_PROJECT" --profile v3-execution up -d --no-deps --force-recreate svc-fusion-extension-stitch-worker

for c in "$DIRECTOR" "$FUSION" "$FUSION_WORKER" "$EXT" "$STITCH_WORKER"; do
  [[ "$(docker inspect -f '{{.State.Status}}' "$c")" == "running" ]] || fail "$c not running"
done

echo
echo "=== 4. RUNTIME CERTIFICATION ==="
docker exec -i "$FUSION_WORKER" python - <<'PY'
import inspect, os
import app.services.providers.omnihuman_adapter as mod
from app.services.providers.omnihuman_adapter import OmniHumanAdapter
src=inspect.getsource(OmniHumanAdapter)
assert "_upload_shared_scene_remote_file_to_fal" in src
assert "asyncio.gather(face_upload, audio_upload)" in src
assert os.getenv("DF_FUSION_WORKER_CONCURRENCY") == "8"
print("OMNIHUMAN_SHARED_INPUT_CACHE_RUNTIME=PASS")
print("FUSION_WORKER_CONCURRENCY=8")
PY

docker exec -i "$STITCH_WORKER" python - <<'PY'
import inspect
from app.services import stitch_service
from app.workers import v3_scene_coordinator
s1=inspect.getsource(stitch_service)
s2=inspect.getsource(v3_scene_coordinator)
assert "conversation_handoff" in s1
assert 'stitch_mode = "conversation_handoff" if conversation_mode == "shared_scene" else None' in s2
assert "settb=AVTB" in s1
assert "acrossfade" in s1
print("SHARED_SCENE_SMOOTH_HANDOFF_RUNTIME=PASS")
PY

docker exec -i "$DIRECTOR" python - <<'PY'
import inspect
from app import shared_scene_routes
src=inspect.getsource(shared_scene_routes.SharedSceneVideoSettingsIn)
assert 'precise_lipsync' in src
print("FAST_DEFAULT_VIDEO_MODE_RUNTIME=PASS")
PY

echo
echo "=== 5. DEPLOY WEB FAST-DEFAULT LABELS ==="
WEB_ROOT="$(resolve_web_root || true)"
[[ -n "$WEB_ROOT" ]] || fail "desifaces_web repository not found"
gh api \
  "repos/prasshanthshankar-afk/desifaces_web/contents/scripts/ops/deploy-next3-web-dev.sh?ref=$WEB_SHA" \
  --jq .content | base64 -d > "$WEB_SCRIPT"
chmod 700 "$WEB_SCRIPT"
WEB_REPO_ROOT="$WEB_ROOT" bash "$WEB_SCRIPT" "$WEB_SHA"

docker exec "$WEB" sh -lc "grep -R -F -m1 'Precise lip-sync' /app/.next >/dev/null"
docker exec "$WEB" sh -lc "grep -R -F -m1 'Enhanced motion may take longer to render' /app/.next >/dev/null"
echo "VIDEO_MODE_UX_DEFAULT=PASS"

echo
echo "============================================================"
echo " VIDEO PERFORMANCE + TRANSITION DEV DEPLOY COMPLETE"
echo " DEFAULT_VIDEO_MODE=PRECISE_LIPSYNC_SYNC3"
echo " NATURAL_MOTION=OPTIONAL_OMNIHUMAN"
echo " OMNIHUMAN_GROUP_PHOTO_UPLOAD=SHARED_SINGLE_FLIGHT"
echo " OMNIHUMAN_SPEAKER_MASK_UPLOAD=SHARED_SINGLE_FLIGHT"
echo " FUSION_WORKER_CONCURRENCY=8"
echo " SHARED_SCENE_TRANSITION=CONVERSATION_HANDOFF"
echo " PRODUCTION_TOUCH=NONE"
echo "============================================================"
