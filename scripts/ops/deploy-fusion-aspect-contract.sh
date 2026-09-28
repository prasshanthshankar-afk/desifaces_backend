#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 1; }

ROOT="$HOME/workspace/desifaces-runtime"
ENV_FILE="${RUNTIME_ENV_FILE:-$ROOT/infra/.env}"
REF="${SOURCE_REF:-fix/next3-shared-scene-profile-lock-fix-20260928}"
LIVE="df-svc-fusion-extension-stitch-worker"
SERVICE="svc-fusion-extension-stitch-worker"
NETWORK="df-net"
PATCH="/tmp/stitch_service.aspect.py"
OLD="/tmp/stitch_service.live.py"
PREP="df-fusion-stitch-prep"
CANDIDATE="desifaces-fusion-stitch:aspect-contract-candidate"

fail(){ echo "FAIL: $*" >&2; exit 1; }

echo "============================================================"
echo " desifaces DEV — FUSION FINAL ASPECT CONTRACT"
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
gh api   "repos/prasshanthshankar-afk/desifaces_backend/contents/services/svc-fusion-extension/app/app/services/stitch_service.py?ref=$REF"   --jq .content | base64 -d > "$PATCH"

grep -q 'aspect_ratio=aspect_ratio' "$PATCH" || fail "compose aspect forwarding missing"
grep -q 'aspect_ratio: Optional\[str\] = None' "$PATCH" || fail "normalize aspect contract missing"
python3 -m py_compile "$PATCH"
echo "PATCH_SOURCE=PASS"

docker cp "$LIVE:/app/app/services/stitch_service.py" "$OLD"

echo
echo "===== 2. TARGET DIFF ====="
diff -u "$OLD" "$PATCH" || true

echo
echo "===== 3. SURGICAL CANDIDATE ====="
docker rm -f "$PREP" >/dev/null 2>&1 || true
docker image rm "$CANDIDATE" >/dev/null 2>&1 || true
docker create --name "$PREP" "$OLD_ID" >/dev/null
docker cp "$PATCH" "$PREP:/app/app/services/stitch_service.py"
docker commit "$PREP" "$CANDIDATE" >/dev/null
docker rm "$PREP" >/dev/null

NEW_ID="$(docker image inspect "$CANDIDATE" --format '{{.Id}}')"
echo "candidate_image=$NEW_ID"

docker run --rm --entrypoint sh "$OLD_ID" -c   'find /app/app -type f -name "*.py" ! -path "/app/app/services/stitch_service.py" -exec sha256sum {} \; | sort'   > /tmp/fusion-stitch-before.txt
docker run --rm --entrypoint sh "$CANDIDATE" -c   'find /app/app -type f -name "*.py" ! -path "/app/app/services/stitch_service.py" -exec sha256sum {} \; | sort'   > /tmp/fusion-stitch-after.txt
diff -u /tmp/fusion-stitch-before.txt /tmp/fusion-stitch-after.txt
echo "NON_TARGET_CODE=BYTE_IDENTICAL"

echo
echo "===== 4. REAL VIDEO ASPECT CERTIFICATION ====="
docker run --rm --entrypoint bash "$CANDIDATE" -lc '
set -Eeuo pipefail
ffmpeg -v error -y   -f lavfi -i "color=c=black:s=1536x1024:r=30:d=1"   -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000"   -shortest -c:v libx264 -pix_fmt yuv420p -c:a aac /tmp/in.mp4
python - <<'"'"'PY'"'"'
from app.services.stitch_service import normalize_segment_mp4
normalize_segment_mp4(
    "/tmp/in.mp4",
    "/tmp/out.mp4",
    edge_fade_override=0.0,
    aspect_ratio="16:9",
)
PY
DIMS="$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 /tmp/out.mp4)"
test "$DIMS" = "1920x1080"
echo "FINAL_16_9_DIMENSIONS=PASS:$DIMS"
'

echo
echo "===== 5. ACTIVE STITCH SAFETY GATE ====="
for attempt in $(seq 1 60); do
  ACTIVE="$(
    docker exec -i desifaces-db sh -lc '
      psql -X -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"
    ' <<'SQL'
select count(*)
from public.longform_jobs
where status in ('stitching','stitching_running');
SQL
  )"
  ACTIVE="${ACTIVE:-0}"
  if [[ "$ACTIVE" == "0" ]]; then
    echo "ACTIVE_STITCH_JOBS=0"
    break
  fi
  if (( attempt == 1 || attempt % 6 == 0 )); then
    echo "waiting_for_active_stitch_jobs=$ACTIVE attempt=$attempt/60"
  fi
  sleep 5
done
[[ "${ACTIVE:-0}" == "0" ]] || fail "active stitch jobs did not drain"
echo "ACTIVE_STITCH_GATE=PASS"

echo
echo "===== 6. PROMOTE STITCH WORKER ONLY ====="
docker tag "$OLD_ID" "${LIVE_REF}:rollback-aspect" 2>/dev/null || true
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
[[ "$(docker inspect "$LIVE" --format '{{.State.Status}}')" == "running" ]] || fail "stitch worker not running"
[[ "$(docker inspect "$LIVE" --format '{{.Image}}')" == "$NEW_ID" ]] || fail "running image mismatch"
[[ "$(docker inspect "$LIVE" --format '{{.RestartCount}}')" == "0" ]] || fail "stitch worker restarted"

docker exec "$LIVE" python - <<'PY'
from app.services import stitch_service
import inspect
assert stitch_service._aspect_dimensions("16:9") == (1920, 1080)
src = inspect.getsource(stitch_service.compose_timeline)
assert "aspect_ratio=aspect_ratio" in src
print("LIVE_ASPECT_CONTRACT=PASS")
PY

BAD_C="$(docker ps -a --format '{{.Names}}' | grep -Ei 'v3|next3' || true)"
BAD_N="$(docker network ls --format '{{.Name}}' | grep -Ei 'v3|next3' || true)"
[[ -z "$BAD_C" ]] || { echo "$BAD_C"; fail "versioned container name detected"; }
[[ -z "$BAD_N" ]] || { echo "$BAD_N"; fail "versioned network name detected"; }

trap - ERR

echo
echo "============================================================"
echo " FUSION_ASPECT_CONTRACT_DEPLOY=PASS"
echo " running_image=$NEW_ID"
echo " canonical_network=$NETWORK"
echo " only_code_changed=stitch_service.py"
echo " production=UNTOUCHED"
echo "============================================================"
