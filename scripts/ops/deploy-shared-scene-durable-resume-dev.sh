#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
ROOT="/home/azureuser/workspace/desifaces-v3"
ENV_FILE="$ROOT/infra/.env"
BACKEND_SHA="ee3b042e97195e3dc770a515be8ac0393affb644"
WEB_SHA="599b45fedff355ad32b679a2d3b954b54bc5c723"
WT="/tmp/desifaces-shared-scene-durable-resume"
DIRECTOR="df-v3-svc-director"
FUSION_WORKER="df-v3-svc-fusion-worker"
WEB="df-web-dev"
WEB_SCRIPT="/tmp/deploy-next3-durable-resume-web.sh"

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
for c in "$DIRECTOR" "$FUSION_WORKER" "$WEB"; do
  docker inspect "$c" >/dev/null 2>&1 || fail "$c missing"
done

AVAILABLE_KB="$(df -Pk / | awk 'NR==2 {print $4}')"
[[ "$AVAILABLE_KB" =~ ^[0-9]+$ ]] || fail "could not determine free disk space"
(( AVAILABLE_KB >= 6 * 1024 * 1024 )) || fail "less than 6 GiB free; no build or runtime cutover attempted"
echo "FREE_DISK_GIB=$(( AVAILABLE_KB / 1024 / 1024 ))"

WORKER_IMAGE_BEFORE="$(docker inspect -f '{{.Image}}' "$FUSION_WORKER")"
SYNC_CONCURRENCY_BEFORE="$(docker exec "$FUSION_WORKER" sh -lc 'printf "%s" "${DF_SYNC3_PROVIDER_CONCURRENCY:-1}"')"

echo "============================================================"
echo " desifaces DEV — DURABLE GROUP-PHOTO RESUME"
echo " backend_sha=$BACKEND_SHA"
echo " web_sha=$WEB_SHA"
echo " touch_scope=director+web"
echo " fusion_worker_touch=NONE"
echo " production_touch=NONE"
echo "============================================================"

echo
echo "=== 1. EXACT BACKEND SOURCE + CONTRACT ==="
git -C "$ROOT" fetch --no-tags origin "$BACKEND_SHA"
git -C "$ROOT" cat-file -e "$BACKEND_SHA^{commit}"

if git -C "$ROOT" worktree list --porcelain | grep -Fxq "worktree $WT"; then
  git -C "$ROOT" worktree remove --force "$WT"
elif [[ -e "$WT" ]]; then
  rm -rf "$WT"
fi
git -C "$ROOT" worktree add --detach "$WT" "$BACKEND_SHA"
[[ "$(git -C "$WT" rev-parse HEAD)" == "$BACKEND_SHA" ]] || fail "backend worktree SHA mismatch"

grep -Fq "shared-scene-draft" "$WT/services/svc-director/app/app/shared_scene_routes.py"
grep -Fq "shared_scene_draft_media_id" "$WT/services/svc-director/app/app/shared_scene_routes.py"
grep -Fq "metadata.pop(draft_key, None)" "$WT/services/svc-director/app/app/shared_scene_routes.py"
grep -Fq "'source_image'" "$WT/services/svc-director/app/app/shared_scene_routes.py"
python3 -m py_compile "$WT/services/svc-director/app/app/shared_scene_routes.py"
echo "DURABLE_GROUP_PHOTO_BACKEND_SOURCE=PASS"

DIR_PROJECT="$(project_of "$DIRECTOR")" || fail "Director Compose project missing"
echo "DIRECTOR_PROJECT=$DIR_PROJECT"

echo
echo "=== 2. BUILD DIRECTOR + PRE-CUTOVER CONTRACT CHECK ==="
cd "$WT"
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$DIR_PROJECT" build svc-director

docker run --rm \
  --env-file "$ENV_FILE" \
  --entrypoint python \
  desifaces-v3-svc-director \
  -c 'import inspect; from app.shared_scene_routes import SharedSceneDraftIn,set_shared_scene_draft,set_shared_scene_conversation; d=SharedSceneDraftIn(shared_scene_media_id="00000000-0000-0000-0000-000000000001",speaker_targets=[]); assert d.image_width is None; s=inspect.getsource(set_shared_scene_draft); a=inspect.getsource(set_shared_scene_conversation); assert "source_image" in s; assert "shared_scene_draft_media_id" in a; print("DIRECTOR_DURABLE_DRAFT_CANDIDATE=PASS")'

echo
echo "=== 3. CUT OVER DIRECTOR ONLY ==="
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$DIR_PROJECT" up -d --no-deps --force-recreate svc-director
[[ "$(docker inspect -f '{{.State.Status}}' "$DIRECTOR")" == "running" ]] || fail "Director not running"

docker exec -i "$DIRECTOR" python - <<'PY'
import inspect
from app.shared_scene_routes import SharedSceneDraftIn,set_shared_scene_draft,set_shared_scene_conversation
draft=SharedSceneDraftIn(
    shared_scene_media_id="00000000-0000-0000-0000-000000000001",
    speaker_targets=[],
)
assert draft.image_width is None
assert "source_image" in inspect.getsource(set_shared_scene_draft)
assert "shared_scene_draft_media_id" in inspect.getsource(set_shared_scene_conversation)
print("DIRECTOR_DURABLE_DRAFT_RUNTIME=PASS")
PY

echo
echo "=== 4. DEPLOY WEB WITH TYPECHECK/REGRESSION SUITE ==="
WEB_ROOT="$(resolve_web_root || true)"
[[ -n "$WEB_ROOT" ]] || fail "desifaces_web repository not found"

gh api \
  "repos/prasshanthshankar-afk/desifaces_web/contents/scripts/ops/deploy-next3-web-dev.sh?ref=$WEB_SHA" \
  --jq .content | base64 -d > "$WEB_SCRIPT"
chmod 700 "$WEB_SCRIPT"
WEB_REPO_ROOT="$WEB_ROOT" bash "$WEB_SCRIPT" "$WEB_SHA"

docker exec "$WEB" sh -lc "grep -R -F -m1 'Restored group photo and' /app/.next >/dev/null"
docker exec "$WEB" sh -lc "grep -R -F -m1 'Refreshing secure group-photo access' /app/.next >/dev/null"
echo "WEB_DURABLE_GROUP_PHOTO_RUNTIME=PASS"

echo
echo "=== 5. NO-REGRESSION RUNTIME GUARD ==="
[[ "$(docker inspect -f '{{.Image}}' "$FUSION_WORKER")" == "$WORKER_IMAGE_BEFORE" ]] || fail "Fusion worker changed unexpectedly"
SYNC_CONCURRENCY_AFTER="$(docker exec "$FUSION_WORKER" sh -lc 'printf "%s" "${DF_SYNC3_PROVIDER_CONCURRENCY:-1}"')"
[[ "$SYNC_CONCURRENCY_AFTER" == "$SYNC_CONCURRENCY_BEFORE" ]] || fail "Sync3 provider concurrency changed unexpectedly"
[[ "$SYNC_CONCURRENCY_AFTER" == "6" ]] || fail "Sync3 six-way parallel runtime is not preserved"
echo "SYNC3_PARALLEL_WORKER_PRESERVED=PASS"
echo "SYNC3_PROVIDER_CONCURRENCY=$SYNC_CONCURRENCY_AFTER"

echo
echo "============================================================"
echo " DURABLE GROUP-PHOTO RESUME DEV DEPLOY COMPLETE"
echo " GROUP_PHOTO_URL=REFRESHED_FROM_DURABLE_MEDIA_ID"
echo " EXPIRED_SAS_SELF_HEAL=ENABLED"
echo " PARTIAL_SPEAKER_MAPPING=SERVER_PERSISTED"
echo " RESUME_AFTER_REFRESH=ENABLED"
echo " FINAL_APPROVAL_CONTRACT=UNCHANGED"
echo " SYNC3_PARALLEL_WORKER=PRESERVED"
echo " PRODUCTION_TOUCH=NONE"
echo "============================================================"
