#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
ROOT="/home/azureuser/workspace/desifaces-v3"
ENV_FILE="$ROOT/infra/.env"
TARGET_SHA="c9df4c809440754d11dd9067e2e48aefb2dc4a49"
WT="/tmp/desifaces-sync3-concurrency-$TARGET_SHA"
API="df-v3-svc-fusion"
WORKER="df-v3-svc-fusion-worker"

fail(){ echo "FAIL: $*" >&2; exit 1; }

cleanup(){
  set +e
  if git -C "$ROOT" worktree list --porcelain 2>/dev/null | grep -Fxq "worktree $WT"; then
    git -C "$ROOT" worktree remove --force "$WT" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ -f "$ENV_FILE" ]] || fail "DEV env missing"
docker inspect "$API" >/dev/null 2>&1 || fail "$API missing"
docker inspect "$WORKER" >/dev/null 2>&1 || fail "$WORKER missing"

echo "============================================================"
echo " desifaces DEV — SYNC3 CONCURRENCY REPAIR"
echo " target_sha=$TARGET_SHA"
echo " production_touch=NONE"
echo "============================================================"

git -C "$ROOT" fetch --no-tags origin "$TARGET_SHA"
git -C "$ROOT" cat-file -e "$TARGET_SHA^{commit}"

if git -C "$ROOT" worktree list --porcelain | grep -Fxq "worktree $WT"; then
  git -C "$ROOT" worktree remove --force "$WT"
elif [[ -e "$WT" ]]; then
  fail "unrecognized worktree exists: $WT"
fi

git -C "$ROOT" worktree add --detach "$WT" "$TARGET_SHA"
[[ "$(git -C "$WT" rev-parse HEAD)" == "$TARGET_SHA" ]] || fail "worktree SHA mismatch"

grep -Fq 'DF_SYNC3_PROVIDER_CONCURRENCY' "$WT/services/svc-fusion/app/app/services/providers/sync3_adapter.py"
grep -Fq 'concurrency_limit_reached' "$WT/services/svc-fusion/app/app/services/providers/sync3_adapter.py"
grep -Fq 'test_sync3_submit_waits_through_provider_concurrency_limit' "$WT/services/svc-fusion/tests/test_sync3_adapter.py"
echo "SYNC3_CONCURRENCY_SOURCE_CONTRACT=PASS"

PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$API")"
[[ -n "$PROJECT" ]] || fail "Compose project missing"

cd "$WT"

# Focused unit test before runtime replacement.
docker run --rm   -v "$WT:/repo"   -w /repo/services/svc-fusion   python:3.11-slim   sh -lc 'pip install -q -r app/app/requirements.txt >/dev/null && PYTHONPATH=app pytest -q tests/test_sync3_adapter.py'   || fail "focused Sync3 tests failed"

echo "SYNC3_CONCURRENCY_TESTS=PASS"

V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" build svc-fusion
echo "FUSION_IMAGE_BUILD=PASS"

V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT"   up -d --no-deps --force-recreate svc-fusion

V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" --profile v3-execution   up -d --no-deps --force-recreate svc-fusion-worker

[[ "$(docker inspect -f '{{.State.Status}}' "$API")" == "running" ]] || fail "Fusion API not running"
[[ "$(docker inspect -f '{{.State.Status}}' "$WORKER")" == "running" ]] || fail "Fusion worker not running"

docker exec -i "$WORKER" python - <<'PY'
import inspect
from app.services.providers.sync3_adapter import Sync3Adapter
import app.services.providers.sync3_adapter as m

src=inspect.getsource(m)
assert "DF_SYNC3_PROVIDER_CONCURRENCY" in src
assert "concurrency_limit_reached" in src
a=Sync3Adapter()
assert a.api_key
assert a.provider_name=="sync3"
print("SYNC3_PROVIDER_CONCURRENCY_DEFAULT="+str(m._sync3_provider_concurrency()))
print("SYNC3_CONCURRENCY_RUNTIME_CONTRACT=PASS")
PY

echo "============================================================"
echo " SYNC3 CONCURRENCY REPAIR DEPLOYED"
echo " PRODUCTION_TOUCH=NONE"
echo "============================================================"
