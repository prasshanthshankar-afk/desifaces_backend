#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
BACKEND_ROOT="/home/azureuser/workspace/desifaces-v3"
WEB_ROOT="/home/azureuser/workspace/desifaces_web"
ENV_FILE="$BACKEND_ROOT/infra/.env"

BACKEND_RUNTIME_SHA="6dfb2b8ff47fd4e81a6e4bc70ca3857b5b3ba756"
WEB_RUNTIME_SHA="dbd2a1f9215742a560243401fa641bf3ddb4bba2"

BACKEND_WT="/tmp/desifaces-backend-next3-coordinator"
WEB_DEPLOY_SCRIPT="/tmp/deploy-next3-web-dev.sh"

STITCH="df-v3-svc-fusion-extension-stitch-worker"
WEB="df-web-dev"

fail(){ echo "FAIL: $*" >&2; exit 1; }

cleanup(){
  set +e
  if [[ -d "$BACKEND_ROOT/.git" || -f "$BACKEND_ROOT/.git" ]]; then
    if git -C "$BACKEND_ROOT" worktree list --porcelain 2>/dev/null | grep -Fxq "worktree $BACKEND_WT"; then
      git -C "$BACKEND_ROOT" worktree remove --force "$BACKEND_WT" >/dev/null 2>&1 || true
    fi
  fi
  rm -f "$WEB_DEPLOY_SCRIPT"
}
trap cleanup EXIT

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ -f "$ENV_FILE" ]] || fail "canonical DEV env missing"
[[ -d "$BACKEND_ROOT/.git" || -f "$BACKEND_ROOT/.git" ]] || fail "backend repo missing"
[[ -d "$WEB_ROOT/.git" || -f "$WEB_ROOT/.git" ]] || fail "web repo missing"
docker inspect "$STITCH" >/dev/null 2>&1 || fail "$STITCH missing"
docker inspect "$WEB" >/dev/null 2>&1 || fail "$WEB missing"

echo "============================================================"
echo " desifaces DEV — #next3 UI + COORDINATOR PATCH"
echo " backend_runtime_sha=$BACKEND_RUNTIME_SHA"
echo " web_runtime_sha=$WEB_RUNTIME_SHA"
echo " production_touch=NONE"
echo "============================================================"

echo
echo "=== 1. BACKEND COORDINATOR — ISOLATED EXACT SOURCE ==="

git -C "$BACKEND_ROOT" fetch --no-tags origin "$BACKEND_RUNTIME_SHA"
git -C "$BACKEND_ROOT" cat-file -e "$BACKEND_RUNTIME_SHA^{commit}"

if git -C "$BACKEND_ROOT" worktree list --porcelain | grep -Fxq "worktree $BACKEND_WT"; then
  git -C "$BACKEND_ROOT" worktree remove --force "$BACKEND_WT"
elif [[ -e "$BACKEND_WT" ]]; then
  fail "unrecognized backend worktree path exists: $BACKEND_WT"
fi

git -C "$BACKEND_ROOT" worktree add --detach "$BACKEND_WT" "$BACKEND_RUNTIME_SHA"
[[ "$(git -C "$BACKEND_WT" rev-parse HEAD)" == "$BACKEND_RUNTIME_SHA" ]] || fail "backend worktree SHA mismatch"

grep -Fq 'has_failed and all_terminal'   "$BACKEND_WT/services/svc-fusion-extension/app/app/workers/v3_scene_coordinator.py"   || fail "terminal child reconciliation fix missing"

echo "COORDINATOR_SOURCE_CONTRACT=PASS"

PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$STITCH")"
SERVICE="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$STITCH")"
[[ -n "$PROJECT" ]] || fail "stitch Compose project missing"
[[ "$SERVICE" == "svc-fusion-extension-stitch-worker" ]] || fail "unexpected stitch Compose service=$SERVICE"

OLD_STITCH_IMAGE="$(docker inspect -f '{{.Image}}' "$STITCH")"
OLD_STITCH_REF="$(docker inspect -f '{{.Config.Image}}' "$STITCH")"

backend_rollback(){
  local rc=$?
  if (( rc == 0 )); then return 0; fi
  set +e
  echo "===== DEV COORDINATOR ROLLBACK ====="
  [[ -n "$OLD_STITCH_REF" && -n "$OLD_STITCH_IMAGE" ]] && docker tag "$OLD_STITCH_IMAGE" "$OLD_STITCH_REF" >/dev/null 2>&1 || true
  cd "$BACKEND_WT" 2>/dev/null || cd "$BACKEND_ROOT"
  V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" --profile v3-execution     up -d --no-deps --force-recreate svc-fusion-extension-stitch-worker >/dev/null 2>&1 || true
  echo "DEV_COORDINATOR_ROLLBACK=ATTEMPTED"
  exit "$rc"
}
trap backend_rollback ERR

cd "$BACKEND_WT"
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" --profile v3-execution   build svc-fusion-extension-stitch-worker

V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" --profile v3-execution   up -d --no-deps --force-recreate svc-fusion-extension-stitch-worker

[[ "$(docker inspect -f '{{.State.Status}}' "$STITCH")" == "running" ]] || fail "stitch worker not running"

docker exec -i "$STITCH" python - <<'PY'
import inspect
from app.workers import v3_scene_coordinator
src=inspect.getsource(v3_scene_coordinator)
assert "has_failed and all_terminal" in src
assert "one_or_more_child_fusion_jobs_failed" in src
print("COORDINATOR_RUNTIME_CONTRACT=PASS")
PY

trap - ERR
echo "NEXT3_COORDINATOR_DEV_DEPLOY=PASS"

echo
echo "=== 2. WEB — EXACT DEV CANDIDATE/CUTOVER ==="

gh api   "repos/prasshanthshankar-afk/desifaces_web/contents/scripts/ops/deploy-next3-web-dev.sh?ref=$WEB_RUNTIME_SHA"   --jq .content | base64 -d > "$WEB_DEPLOY_SCRIPT"
chmod 700 "$WEB_DEPLOY_SCRIPT"

WEB_REPO_ROOT="$WEB_ROOT" bash "$WEB_DEPLOY_SCRIPT" "$WEB_RUNTIME_SHA"

[[ "$(docker inspect -f '{{.State.Status}}' "$WEB")" == "running" ]] || fail "DEV web not running"

docker exec "$WEB" sh -lc "grep -R -F -m1 'Speaker mapping' /app/.next >/dev/null"
docker exec "$WEB" sh -lc "grep -R -F -m1 'df-instagram-gradient' /app/.next >/dev/null"
docker exec "$WEB" sh -lc "grep -R -F -m1 'total_credits' /app/.next >/dev/null"

HTTP="$(curl -sS -o /tmp/next3-ui-live.html -w '%{http_code}' --connect-timeout 2 --max-time 8 http://127.0.0.1:13000/app/multi-person 2>/dev/null || true)"
[[ "$HTTP" == "200" ]] || fail "DEV multi-person route unhealthy"

echo "NEXT3_SHARED_SCENE_PREREQUISITE_UI=PASS"
echo "NEXT3_INLINE_SOCIAL_LOGOS=PASS"
echo "NEXT3_CANONICAL_PRICE_RENDERER=PASS"
echo
echo "============================================================"
echo " NEXT3 DEV PATCH COMPLETE"
echo " web=$WEB_RUNTIME_SHA"
echo " coordinator=$BACKEND_RUNTIME_SHA"
echo " PRODUCTION_TOUCH=NONE"
echo "============================================================"
