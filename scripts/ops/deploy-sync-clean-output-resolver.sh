#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 1; }

ROOT="$HOME/workspace/desifaces-runtime"
ENV_FILE="${RUNTIME_ENV_FILE:-$ROOT/infra/.env}"
REF="${SOURCE_REF:-fix/next3-shared-scene-profile-lock-fix-20260928}"
LIVE="df-svc-fusion-worker"
SERVICE="svc-fusion-worker"
NETWORK="df-net"
PATCH="/tmp/sync3_adapter.clean-output.py"
OLD="/tmp/sync3_adapter.live.py"
PREP="df-fusion-sync-prep"
CANDIDATE="desifaces-fusion-worker:clean-output-candidate"

fail(){ echo "FAIL: $*" >&2; exit 1; }

echo "============================================================"
echo " desifaces DEV — SYNC PLAN-AWARE CLEAN OUTPUT"
echo "============================================================"
echo "production=UNTOUCHED"

[[ -f "$ROOT/docker-compose.yml" ]] || fail "canonical compose missing"
[[ -f "$ENV_FILE" ]] || fail "runtime env missing"
docker inspect "$LIVE" >/dev/null 2>&1 || fail "$LIVE missing"

OLD_ID="$(docker inspect "$LIVE" --format '{{.Image}}')"
LIVE_REF="$(docker inspect "$LIVE" --format '{{.Config.Image}}')"
echo "live_image=$OLD_ID"
echo "live_image_ref=$LIVE_REF"

echo
echo "===== 1. FETCH PATCH ====="
gh api   "repos/prasshanthshankar-afk/desifaces_backend/contents/services/svc-fusion/app/app/services/providers/sync3_adapter.py?ref=$REF"   --jq .content | base64 -d > "$PATCH"

grep -q '/v2/generations/{job_id}/download' "$PATCH" || fail "plan-aware download resolver missing"
grep -q '_concurrency_retry_after' "$PATCH" || fail "existing concurrency retry missing"
python3 -m py_compile "$PATCH"
echo "PATCH_SOURCE=PASS"

docker cp "$LIVE:/app/app/services/providers/sync3_adapter.py" "$OLD"

echo
echo "===== 2. TARGET DIFF ====="
diff -u "$OLD" "$PATCH" || true

echo
echo "===== 3. SURGICAL CANDIDATE ====="
docker rm -f "$PREP" >/dev/null 2>&1 || true
docker image rm "$CANDIDATE" >/dev/null 2>&1 || true
docker create --name "$PREP" "$OLD_ID" >/dev/null
docker cp "$PATCH" "$PREP:/app/app/services/providers/sync3_adapter.py"
docker commit "$PREP" "$CANDIDATE" >/dev/null
docker rm "$PREP" >/dev/null

NEW_ID="$(docker image inspect "$CANDIDATE" --format '{{.Id}}')"
echo "candidate_image=$NEW_ID"

docker run --rm --entrypoint sh "$OLD_ID" -c   'find /app/app -type f -name "*.py" ! -path "/app/app/services/providers/sync3_adapter.py" -exec sha256sum {} \; | sort'   > /tmp/fusion-sync-before.txt
docker run --rm --entrypoint sh "$CANDIDATE" -c   'find /app/app -type f -name "*.py" ! -path "/app/app/services/providers/sync3_adapter.py" -exec sha256sum {} \; | sort'   > /tmp/fusion-sync-after.txt
diff -u /tmp/fusion-sync-before.txt /tmp/fusion-sync-after.txt
echo "NON_TARGET_CODE=BYTE_IDENTICAL"

echo
echo "===== 4. IN-IMAGE CONTRACT ====="
docker run --rm -i --entrypoint python "$CANDIDATE" - <<'PY'
import httpx
from app.services.providers.sync3_adapter import _concurrency_retry_after, _download_url_from_response

r=httpx.Response(429,json={"errorCode":"concurrency_limit_reached","retryAfterSeconds":20})
assert _concurrency_retry_after(r) == 20.0

d=httpx.Response(200,text="https://example.com/clean.mp4")
assert _download_url_from_response(d) == "https://example.com/clean.mp4"

print("SYNC_RETRY_AND_CLEAN_OUTPUT_CONTRACT=PASS")
PY

echo
echo "===== 5. ACTIVE FUSION SAFETY GATE ====="
for attempt in $(seq 1 80); do
  ACTIVE="$(
    docker exec -i desifaces-db sh -lc '
      psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"
    ' <<'SQL'
select count(*)
from public.studio_jobs
where studio_type='fusion'
  and status in ('running','processing');
SQL
  )"
  ACTIVE="${ACTIVE:-0}"
  if [[ "$ACTIVE" == "0" ]]; then
    echo "ACTIVE_FUSION_JOBS=0"
    break
  fi
  if (( attempt == 1 || attempt % 4 == 0 )); then
    echo "waiting_for_active_fusion_jobs=$ACTIVE attempt=$attempt/80"
  fi
  sleep 10
done
[[ "${ACTIVE:-0}" == "0" ]] || fail "active Fusion jobs did not drain"
echo "ACTIVE_FUSION_GATE=PASS"

echo
echo "===== 6. PROMOTE FUSION WORKER ONLY ====="
docker tag "$OLD_ID" "${LIVE_REF}:rollback-clean-output" 2>/dev/null || true
docker tag "$NEW_ID" "$LIVE_REF"

rollback(){
  rc=$?
  set +e
  docker tag "$OLD_ID" "$LIVE_REF"
  RUNTIME_ENV_FILE="$ENV_FILE" docker compose     --project-directory "$ROOT"     --env-file "$ENV_FILE"     -f "$ROOT/docker-compose.yml"     --profile execution     up -d --no-build --no-deps --force-recreate "$SERVICE" >/dev/null 2>&1
  exit "$rc"
}
trap rollback ERR

RUNTIME_ENV_FILE="$ENV_FILE" docker compose   --project-directory "$ROOT"   --env-file "$ENV_FILE"   -f "$ROOT/docker-compose.yml"   --profile execution   up -d --no-build --no-deps --force-recreate "$SERVICE" >/dev/null

sleep 4
[[ "$(docker inspect "$LIVE" --format '{{.State.Status}}')" == "running" ]] || fail "Fusion worker not running"
[[ "$(docker inspect "$LIVE" --format '{{.Image}}')" == "$NEW_ID" ]] || fail "running image mismatch"
[[ "$(docker inspect "$LIVE" --format '{{.RestartCount}}')" == "0" ]] || fail "Fusion worker restarted"

docker exec "$LIVE" python - <<'PY'
from pathlib import Path
p=Path("/app/app/services/providers/sync3_adapter.py").read_text()
assert "/v2/generations/{job_id}/download" in p
assert "_concurrency_retry_after" in p
print("LIVE_SYNC_CLEAN_OUTPUT_CONTRACT=PASS")
PY

BAD_C="$(docker ps -a --format '{{.Names}}' | grep -Ei 'v3|next3' || true)"
BAD_N="$(docker network ls --format '{{.Name}}' | grep -Ei 'v3|next3' || true)"
[[ -z "$BAD_C" ]] || { echo "$BAD_C"; fail "versioned container name detected"; }
[[ -z "$BAD_N" ]] || { echo "$BAD_N"; fail "versioned network name detected"; }

trap - ERR

echo
echo "============================================================"
echo " SYNC_CLEAN_OUTPUT_DEPLOY=PASS"
echo " running_image=$NEW_ID"
echo " canonical_network=$NETWORK"
echo " only_code_changed=sync3_adapter.py"
echo " production=UNTOUCHED"
echo "============================================================"
