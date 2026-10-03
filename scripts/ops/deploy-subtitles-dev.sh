#!/usr/bin/env bash
set -Eeuo pipefail

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-dev" ]] || fail "DEV host required"
docker info >/dev/null 2>&1 || fail "Docker is unavailable"

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$ROOT" ]] || fail "run from the desifaces_backend repository"
cd "$ROOT"

test -f services/svc-fusion-extension/app/app/services/subtitle_service.py || fail "subtitle source is missing"
grep -Fq 'subtitle_track_generation_failed' services/svc-fusion-extension/app/app/services/longform_orchestrator.py || fail "subtitle finalization guard is missing"

IMAGE="desifaces-svc-fusion-extension:subtitles-dev"
API_NAME="df-svc-fusion-extension"
STITCH_NAME="df-svc-fusion-extension-stitch-worker"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP_DIR="$(mktemp -d /tmp/desifaces-subtitles-dev.XXXXXX)"
API_ROLLBACK="${API_NAME}-rollback"
STITCH_ROLLBACK="${STITCH_NAME}-rollback"
CUTOVER=0

cleanup(){ rm -rf "$TMP_DIR"; }
trap cleanup EXIT

find_container(){
  local canonical="$1"
  local service="$2"
  if docker inspect "$canonical" >/dev/null 2>&1; then
    printf '%s\n' "$canonical"
    return 0
  fi
  docker ps     --filter "label=com.docker.compose.service=$service"     --format '{{.Names}}'     | head -n1
}

capture_env(){
  local container="$1" path="$2"
  docker inspect "$container" --format '{{range .Config.Env}}{{println .}}{{end}}' > "$path"
  chmod 600 "$path"
}

network_of(){
  docker inspect "$1" --format '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' | sed '/^$/d' | head -n1
}

published_port(){
  docker port "$1" "$2/tcp" 2>/dev/null | head -n1 || true
}

rollback(){
  local rc=$?
  if [[ "$CUTOVER" == "1" ]]; then
    echo "ROLLBACK=START"
    docker rm -f "$API_NAME" >/dev/null 2>&1 || true
    docker rm -f "$STITCH_NAME" >/dev/null 2>&1 || true
    if docker inspect "$API_ROLLBACK" >/dev/null 2>&1; then
      docker rename "$API_ROLLBACK" "$API_NAME" >/dev/null 2>&1 || true
      docker update --restart=unless-stopped "$API_NAME" >/dev/null 2>&1 || true
      docker start "$API_NAME" >/dev/null 2>&1 || true
    fi
    if docker inspect "$STITCH_ROLLBACK" >/dev/null 2>&1; then
      docker rename "$STITCH_ROLLBACK" "$STITCH_NAME" >/dev/null 2>&1 || true
      docker update --restart=unless-stopped "$STITCH_NAME" >/dev/null 2>&1 || true
      docker start "$STITCH_NAME" >/dev/null 2>&1 || true
    fi
    echo "ROLLBACK=COMPLETE"
  fi
  exit "$rc"
}

echo "============================================================"
echo " desifaces DEV — SUBTITLE BACKEND DEPLOY"
echo "============================================================"
echo "source_sha=$(git rev-parse HEAD)"
echo "target_api=$API_NAME"
echo "target_stitch_worker=$STITCH_NAME"
echo "production_touch=NONE"
echo "db_schema_change=NONE"

API_CURRENT="$(find_container "$API_NAME" "svc-fusion-extension" || true)"
STITCH_CURRENT="$(find_container "$STITCH_NAME" "svc-fusion-extension-stitch-worker" || true)"
[[ -n "$API_CURRENT" ]] || fail "Fusion Extension API container not found"
[[ -n "$STITCH_CURRENT" ]] || fail "Fusion Extension stitch worker container not found"

API_NETWORK="$(network_of "$API_CURRENT")"
STITCH_NETWORK="$(network_of "$STITCH_CURRENT")"
[[ -n "$API_NETWORK" ]] || fail "API network unavailable"
[[ -n "$STITCH_NETWORK" ]] || fail "stitch worker network unavailable"

capture_env "$API_CURRENT" "$TMP_DIR/api.env"
capture_env "$STITCH_CURRENT" "$TMP_DIR/stitch.env"
API_PORT="$(published_port "$API_CURRENT" 8006)"

echo
echo "===== 1. SOURCE TEST ====="
python3 -m compileall -q services/svc-fusion-extension/app/app
echo "PYTHON_COMPILE=PASS"

echo
echo "===== 2. BUILD CANDIDATE IMAGE ====="
docker build -f services/svc-fusion-extension/app/Dockerfile -t "$IMAGE" .
echo "BACKEND_IMAGE_BUILD=PASS"

echo
echo "===== 3. RETIRE CURRENT RUNTIME ====="
docker rm -f "$API_ROLLBACK" >/dev/null 2>&1 || true
docker rm -f "$STITCH_ROLLBACK" >/dev/null 2>&1 || true
docker stop "$API_CURRENT" >/dev/null
docker rename "$API_CURRENT" "$API_ROLLBACK"
docker update --restart=no "$API_ROLLBACK" >/dev/null
docker stop "$STITCH_CURRENT" >/dev/null
docker rename "$STITCH_CURRENT" "$STITCH_ROLLBACK"
docker update --restart=no "$STITCH_ROLLBACK" >/dev/null

CUTOVER=1
trap rollback ERR

echo
echo "===== 4. START CANONICAL API ====="
API_RUN=(docker run -d --name "$API_NAME" --restart unless-stopped --network "$API_NETWORK" --network-alias svc-fusion-extension --env-file "$TMP_DIR/api.env" --volumes-from "$API_ROLLBACK")
if [[ -n "$API_PORT" ]]; then API_RUN+=( -p "$API_PORT:8006" ); fi
API_RUN+=( "$IMAGE" )
"${API_RUN[@]}" >/dev/null

echo
echo "===== 5. START CANONICAL STITCH WORKER ====="
docker run -d --name "$STITCH_NAME" --restart unless-stopped --network "$STITCH_NETWORK" --env-file "$TMP_DIR/stitch.env" --volumes-from "$STITCH_ROLLBACK" "$IMAGE" bash -lc 'python -m app.workers.stitch_worker' >/dev/null

echo
echo "===== 6. CERTIFY ====="
PASS=0
for i in $(seq 1 40); do
  if docker exec -i "$API_NAME" python3 - <<'PY' >/tmp/subtitles-api-health.out 2>&1
import urllib.request
with urllib.request.urlopen("http://127.0.0.1:8006/api/health", timeout=3) as r:
    print(r.status)
PY
  then
    if grep -q '^200$' /tmp/subtitles-api-health.out; then PASS=1; break; fi
  fi
  sleep 2
done
[[ "$PASS" == "1" ]] || { docker logs --tail 200 "$API_NAME" >&2 || true; fail "Fusion Extension API did not become healthy"; }

docker exec -i "$API_NAME" python3 - <<'PY'
from app.services.subtitle_service import build_webvtt
body = build_webvtt([{"duration_sec": 2, "text_chunk": "Subtitle deployment certification."}])
assert body.startswith("WEBVTT")
assert "Subtitle deployment certification." in body
print("SUBTITLE_MODULE=PASS")
PY

[[ "$(docker inspect -f '{{.State.Status}}' "$STITCH_NAME")" == "running" ]] || fail "stitch worker is not running"
sleep 2
if docker logs --since 30s "$STITCH_NAME" 2>&1 | grep -Eiq 'Traceback|ModuleNotFoundError|ImportError'; then
  docker logs --since 30s "$STITCH_NAME" 2>&1 >&2
  fail "stitch worker startup error"
fi

CUTOVER=0
trap - ERR

echo
echo "===== 7. NAMING POLICY ====="
for c in "$API_NAME" "$STITCH_NAME"; do
  if [[ "$c" =~ (^|[-_])v[0-9]+($|[-_]) ]]; then fail "version token found in live container name: $c"; fi
done
echo "VERSIONLESS_CONTAINER_NAMES=PASS"

echo
echo "============================================================"
echo " DEV_SUBTITLE_BACKEND_DEPLOY=PASS"
echo " active_api=$API_NAME"
echo " active_stitch_worker=$STITCH_NAME"
echo " image=$IMAGE"
echo " api_rollback=$API_ROLLBACK"
echo " stitch_rollback=$STITCH_ROLLBACK"
echo " production_touch=NONE"
echo "============================================================"
