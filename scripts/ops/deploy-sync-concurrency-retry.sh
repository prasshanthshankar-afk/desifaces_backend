#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 1; }

ROOT="$HOME/workspace/desifaces-runtime"
TARGET_SHA="${TARGET_SHA:-ac89e4185aab5651649376eca1d7c4b1725287b5}"
LIVE="df-svc-fusion-worker"
NETWORK="df-net"
IMAGE="desifaces-svc-fusion-worker:candidate-concurrency-retry"
CANONICAL="desifaces-svc-fusion-worker:latest"
ROLLBACK="desifaces-svc-fusion-worker:rollback-concurrency"
ENV_FILE="${RUNTIME_ENV_FILE:-$ROOT/infra/.env}"

fail(){ echo "FAIL: $*" >&2; exit 1; }

echo "============================================================"
echo " desifaces DEV — SYNC CONCURRENCY WAIT/RETRY"
echo "============================================================"
echo "target_sha=$TARGET_SHA"
echo "production=UNTOUCHED"

[[ -d "$ROOT" ]] || fail "runtime source missing"
[[ -f "$ROOT/docker-compose.yml" ]] || fail "canonical compose missing"
[[ -f "$ENV_FILE" ]] || fail "runtime env missing"
[[ -z "$(git -C "$ROOT" status --porcelain)" ]] || fail "runtime worktree is dirty"

echo
echo "===== 1. SOURCE ====="
git -C "$ROOT" fetch --quiet origin "$TARGET_SHA"
git -C "$ROOT" checkout --quiet --detach "$TARGET_SHA"
[[ "$(git -C "$ROOT" rev-parse HEAD)" == "$TARGET_SHA" ]] || fail "source SHA mismatch"

grep -q '_concurrency_retry_after' "$ROOT/services/svc-fusion/app/app/services/providers/sync3_adapter.py"
grep -q 'concurrency_limit_reached' "$ROOT/services/svc-fusion/app/app/services/providers/sync3_adapter.py"
echo "SOURCE_CONTRACT=PASS"

echo
echo "===== 2. PRESERVE LIVE IMAGE ====="
OLD_ID="$(docker inspect "$LIVE" --format '{{.Image}}')"
docker tag "$OLD_ID" "$ROLLBACK"
echo "old_image=$OLD_ID"

echo
echo "===== 3. BUILD CANDIDATE ====="
docker build   --label "org.opencontainers.image.revision=$TARGET_SHA"   -t "$IMAGE"   -f "$ROOT/services/svc-fusion/app/Dockerfile"   "$ROOT"

NEW_ID="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
echo "candidate_image=$NEW_ID"

echo
echo "===== 4. IN-IMAGE CONTRACT ====="
docker run --rm --entrypoint python "$IMAGE" - <<'PY'
import httpx
from app.services.providers.sync3_adapter import _concurrency_retry_after
r=httpx.Response(429,json={
    "errorCode":"concurrency_limit_reached",
    "retryAfterSeconds":20,
    "activeGenerations":1,
    "concurrencyLimit":1,
})
assert _concurrency_retry_after(r) == 20.0
r2=httpx.Response(429,json={"errorCode":"rate_limit_exceeded","retryAfterSeconds":20})
assert _concurrency_retry_after(r2) is None
print("SYNC_429_RETRY_CONTRACT=PASS")
PY

docker run --rm --entrypoint python "$IMAGE" -m py_compile   /app/app/services/providers/sync3_adapter.py
echo "PY_COMPILE=PASS"

echo
echo "===== 5. ACTIVE FUSION JOB SAFETY GATE ====="
for attempt in $(seq 1 80); do
  ACTIVE="$(
    docker exec desifaces-db sh -lc '
      psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"         -c "select count(*) from public.studio_jobs where studio_type='''fusion''' and status in ('''running''','''processing''');"
    '
  )"
  ACTIVE="${ACTIVE:-0}"
  if [[ "$ACTIVE" == "0" ]]; then
    echo "ACTIVE_FUSION_JOBS=0"
    break
  fi
  if (( attempt == 1 || attempt % 4 == 0 )); then
    echo "waiting_for_active_fusion_jobs=$ACTIVE attempt=$attempt/80"
  fi
  sleep 15
done

[[ "${ACTIVE:-0}" == "0" ]] || fail "active Fusion jobs did not drain; worker was NOT restarted"
echo "ACTIVE_FUSION_JOB_GATE=PASS"

echo
echo "===== 6. PROMOTE WORKER ONLY =====
docker tag "$NEW_ID" "$CANONICAL"

rollback(){
  rc=$?
  set +e
  docker tag "$OLD_ID" "$CANONICAL"
  RUNTIME_ENV_FILE="$ENV_FILE" docker compose     --project-directory "$ROOT"     --env-file "$ENV_FILE"     -f "$ROOT/docker-compose.yml"     --profile execution     up -d --no-build --no-deps --force-recreate svc-fusion-worker >/dev/null 2>&1
  exit "$rc"
}
trap rollback ERR

RUNTIME_ENV_FILE="$ENV_FILE" docker compose   --project-directory "$ROOT"   --env-file "$ENV_FILE"   -f "$ROOT/docker-compose.yml"   --profile execution   up -d --no-build --no-deps --force-recreate svc-fusion-worker >/dev/null

sleep 3

RUNNING_ID="$(docker inspect "$LIVE" --format '{{.Image}}')"
[[ "$RUNNING_ID" == "$NEW_ID" ]] || fail "worker image mismatch"

docker exec "$LIVE" sh -lc '
  grep -q "_concurrency_retry_after" /app/app/services/providers/sync3_adapter.py
  grep -q "concurrency_limit_reached" /app/app/services/providers/sync3_adapter.py
  test "$DF_SYNC3_PROVIDER_CONCURRENCY" = "1"
  test "$DF_SYNC3_CONCURRENCY_WAIT_SECONDS" = "900"
'
echo "LIVE_CONTRACT=PASS"

BAD_C="$(docker ps -a --format '{{.Names}}' | grep -Ei 'v3|next3' || true)"
BAD_N="$(docker network ls --format '{{.Name}}' | grep -Ei 'v3|next3' || true)"
[[ -z "$BAD_C" ]] || { echo "$BAD_C"; fail "versioned container name detected"; }
[[ -z "$BAD_N" ]] || { echo "$BAD_N"; fail "versioned network name detected"; }

trap - ERR

echo
echo "============================================================"
echo " SYNC_CONCURRENCY_RETRY_DEPLOY=PASS"
echo " running_image=$RUNNING_ID"
echo " canonical_network=$NETWORK"
echo " versioned_runtime_names=NONE"
echo " production=UNTOUCHED"
echo "============================================================"
