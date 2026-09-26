#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
BACKEND_ROOT="/home/azureuser/workspace/desifaces-v3"
ENV_FILE="$BACKEND_ROOT/infra/.env"
BACKEND_SHA="04ce6346203504c58dbc915d7ed92b8c94d7f4f1"
WEB_SHA="a7683467af3678b2ac0831ae1899ab1077c16838"
BACKEND_WT="/tmp/desifaces-group-photo-lineage-routing"
WEB_DEPLOY_SCRIPT="/tmp/deploy-next3-web-dev-group-photo.sh"

FACE="df-v3-svc-face"
FACE_WORKER="df-v3-svc-face-worker"
DASH="df-v3-svc-dashboard"
WEB="df-web-dev"

fail(){ echo "FAIL: $*" >&2; exit 1; }

resolve_web_root() {
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
  if git -C "$BACKEND_ROOT" worktree list --porcelain 2>/dev/null | grep -Fxq "worktree $BACKEND_WT"; then
    git -C "$BACKEND_ROOT" worktree remove --force "$BACKEND_WT" >/dev/null 2>&1 || true
  fi
  rm -f "$WEB_DEPLOY_SCRIPT"
}
trap cleanup EXIT

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ -f "$ENV_FILE" ]] || fail "canonical DEV env missing"
for c in "$FACE" "$FACE_WORKER" "$DASH" "$WEB"; do
  docker inspect "$c" >/dev/null 2>&1 || fail "$c missing"
done

WEB_ROOT="$(resolve_web_root || true)"
[[ -n "$WEB_ROOT" ]] || fail "desifaces_web repository not found"
echo "WEB_ROOT=$WEB_ROOT"

echo "============================================================"
echo " desifaces DEV — GROUP PHOTO LINEAGE + ROUTING"
echo " backend_sha=$BACKEND_SHA"
echo " web_sha=$WEB_SHA"
echo " database_migration=NONE"
echo " production_touch=NONE"
echo "============================================================"

echo
echo "=== 1. BACKEND EXACT SOURCE ==="
git -C "$BACKEND_ROOT" fetch --no-tags origin "$BACKEND_SHA"
git -C "$BACKEND_ROOT" cat-file -e "$BACKEND_SHA^{commit}"

if git -C "$BACKEND_ROOT" worktree list --porcelain | grep -Fxq "worktree $BACKEND_WT"; then
  git -C "$BACKEND_ROOT" worktree remove --force "$BACKEND_WT"
elif [[ -e "$BACKEND_WT" ]]; then
  fail "unrecognized backend worktree exists: $BACKEND_WT"
fi

git -C "$BACKEND_ROOT" worktree add --detach "$BACKEND_WT" "$BACKEND_SHA"
[[ "$(git -C "$BACKEND_WT" rev-parse HEAD)" == "$BACKEND_SHA" ]] || fail "backend worktree SHA mismatch"

grep -Fq '"asset_class": "group_photo"' "$BACKEND_WT/services/svc-face/app/app/api/routes/face_jobs.py"
grep -Fq "group_photo_not_valid_face_source" "$BACKEND_WT/services/svc-face/app/app/services/creator_orchestrator.py"
grep -Fq "_is_group_photo_library_asset" "$BACKEND_WT/services/svc-dashboard/app/app/services/dashboard_service.py"
python3 -m py_compile \
  "$BACKEND_WT/services/svc-face/app/app/api/routes/face_jobs.py" \
  "$BACKEND_WT/services/svc-face/app/app/services/creator_orchestrator.py" \
  "$BACKEND_WT/services/svc-dashboard/app/app/services/dashboard_service.py"
echo "GROUP_PHOTO_BACKEND_SOURCE_CONTRACT=PASS"

PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$FACE")"
[[ -n "$PROJECT" ]] || fail "Compose project missing"

echo
echo "=== 2. BUILD / RECREATE ONLY FACE + DASHBOARD ==="
cd "$BACKEND_WT"
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" build svc-face svc-face-worker svc-dashboard
echo "GROUP_PHOTO_BACKEND_IMAGES=PASS"

V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" \
  up -d --no-deps --force-recreate svc-face svc-dashboard

V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" --profile v3-execution \
  up -d --no-deps --force-recreate svc-face-worker

for c in "$FACE" "$FACE_WORKER" "$DASH"; do
  [[ "$(docker inspect -f '{{.State.Status}}' "$c")" == "running" ]] || fail "$c not running"
done

docker exec -i "$FACE" python - <<'PY'
import inspect
from app.api.routes import face_jobs
from app.services import creator_orchestrator
assert '"asset_class": "group_photo"' in inspect.getsource(face_jobs)
assert "group_photo_not_valid_face_source" in inspect.getsource(creator_orchestrator)
print("GROUP_PHOTO_FACE_RUNTIME_CONTRACT=PASS")
PY

docker exec -i "$DASH" python - <<'PY'
import inspect
from app.services import dashboard_service
src = inspect.getsource(dashboard_service)
assert "_is_group_photo_library_asset" in src
assert "group_photo" in src
print("GROUP_PHOTO_DASHBOARD_RUNTIME_CONTRACT=PASS")
PY

echo
echo "=== 3. DEPLOY WEB CLASSIFICATION / REUSE ROUTING ==="
gh api \
  "repos/prasshanthshankar-afk/desifaces_web/contents/scripts/ops/deploy-next3-web-dev.sh?ref=$WEB_SHA" \
  --jq .content | base64 -d > "$WEB_DEPLOY_SCRIPT"
chmod 700 "$WEB_DEPLOY_SCRIPT"
WEB_REPO_ROOT="$WEB_ROOT" bash "$WEB_DEPLOY_SCRIPT" "$WEB_SHA"

[[ "$(docker inspect -f '{{.State.Status}}' "$WEB")" == "running" ]] || fail "DEV web not running"
docker exec "$WEB" sh -lc "grep -R -F -m1 'Reuse in Group Photo Conversation' /app/.next >/dev/null"
docker exec "$WEB" sh -lc "grep -R -F -m1 'Group Photos' /app/.next >/dev/null"
docker exec "$WEB" sh -lc "grep -R -F -m1 'Saved group photo loaded' /app/.next >/dev/null"

echo "GROUP_PHOTO_SAVED_WORK_CATEGORY=PASS"
echo "GROUP_PHOTO_REUSE_ROUTING=PASS"
echo "FACE_STUDIO_GROUP_PHOTO_EXCLUSION=PASS"
echo
echo "============================================================"
echo " GROUP PHOTO LINEAGE / ROUTING DEV DEPLOY COMPLETE"
echo " PRODUCTION_TOUCH=NONE"
echo "============================================================"
