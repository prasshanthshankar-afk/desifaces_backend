#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
WEB_REPO_ROOT="/home/azureuser/workspace/desifaces-web"
WEB_SHA="0e832f2d14e8f7cfe1d74975ac1277c8f476bfdc"
WEB_IMAGE="desifaces-web-dev:${WEB_SHA:0:12}"
WEB_CONTAINER="df-v3-web"
NETWORK="df-v3-net"
PORT="13000"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CANDIDATE="df-v3-web-candidate-${STAMP}"
ROLLBACK="df-v3-web-rollback-${STAMP}"
RUN_DIR="$(mktemp -d /tmp/desifaces-dev-web-acceptance-XXXXXX)"
WORKTREE="$RUN_DIR/repo"
SWAPPED=0
NEW_STARTED=0
CANDIDATE_STARTED=0
SUCCESS=0

fail(){ echo "FAIL: $*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
for x in git docker curl python3 grep; do need "$x"; done

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "run only on $EXPECTED_HOST; current=$(hostname -s)"
[[ -d "$WEB_REPO_ROOT/.git" ]] || fail "web Git checkout missing: $WEB_REPO_ROOT"
docker network inspect "$NETWORK" >/dev/null 2>&1 || fail "required dev network missing: $NETWORK"

wait_http(){
  local url="$1" label="$2" tries="${3:-45}" code i
  for i in $(seq 1 "$tries"); do
    code="$(curl -sS --connect-timeout 2 --max-time 5 -o /tmp/df-dev-web-${$}.html -w '%{http_code}' "$url" 2>/dev/null || true)"
    echo "wait=$i target=$label http=$code"
    if [[ "$code" == "200" ]]; then
      grep -qi 'desifaces' /tmp/df-dev-web-${$}.html || fail "$label returned HTTP 200 without desifaces branding"
      return 0
    fi
    sleep 2
  done
  return 1
}

free_candidate_port(){
  python3 - <<'PY'
import socket
for port in range(23001, 23021):
    s=socket.socket()
    try:
        s.bind(('127.0.0.1',port))
    except OSError:
        s.close(); continue
    s.close(); print(port); raise SystemExit(0)
raise SystemExit('no free candidate port in 23001-23020')
PY
}

run_web(){
  local name="$1" host_port="$2" restart_policy="$3"
  docker run -d \
    --name "$name" \
    --restart "$restart_policy" \
    --network "$NETWORK" \
    -p "127.0.0.1:${host_port}:3000" \
    -e CORE_BASE_URL=http://df-v3-svc-core:8000 \
    -e DASHBOARD_BASE_URL=http://df-v3-svc-dashboard:8005 \
    -e FACE_BASE_URL=http://df-v3-svc-face:8003 \
    -e AUDIO_BASE_URL=http://df-v3-svc-audio:8004 \
    -e FUSION_BASE_URL=http://df-v3-svc-fusion:8002 \
    -e DIRECTOR_BASE_URL=http://df-v3-svc-director:8011 \
    -e FUSION_EXTENSION_BASE_URL=http://df-v3-svc-fusion-extension:8006 \
    -e PRICING_BASE_URL=http://df-v3-svc-pricing:8009 \
    -e COMMERCE_BASE_URL=http://df-v3-svc-commerce:8008 \
    -e NOTIFICATION_BASE_URL=http://df-v3-svc-core:8000 \
    -e ASSISTANT_BASE_URL=http://df-v3-svc-assistant:8012 \
    -e COOKIE_SECURE=false \
    --label desifaces.environment=dev \
    --label desifaces.purpose=browser-acceptance \
    --label desifaces.web_sha="$WEB_SHA" \
    "$WEB_IMAGE" >/dev/null
}

cleanup(){
  local rc=$?
  set +e
  rm -f /tmp/df-dev-web-${$}.html
  (( CANDIDATE_STARTED == 1 )) && docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
  if (( rc != 0 && SUCCESS == 0 )); then
    echo "===== DEV WEB AUTOMATIC ROLLBACK ====="
    (( NEW_STARTED == 1 )) && docker rm -f "$WEB_CONTAINER" >/dev/null 2>&1 || true
    if (( SWAPPED == 1 )) && docker inspect "$ROLLBACK" >/dev/null 2>&1; then
      docker rename "$ROLLBACK" "$WEB_CONTAINER" >/dev/null 2>&1 || true
      docker update --restart=unless-stopped "$WEB_CONTAINER" >/dev/null 2>&1 || true
      docker start "$WEB_CONTAINER" >/dev/null 2>&1 || true
      echo "DEV_WEB_ROLLBACK=ATTEMPTED"
    fi
  fi
  git -C "$WEB_REPO_ROOT" worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
  rm -rf "$RUN_DIR"
  exit "$rc"
}
trap cleanup EXIT

echo "============================================================"
echo " desifaces DEV — PERSISTENT WEB ACCEPTANCE RUNTIME"
echo "============================================================"
echo "host=$(hostname -s)"
echo "environment=DEV_ONLY"
echo "production_touch=FORBIDDEN"
echo "web_sha=$WEB_SHA"
echo "persistent_container=$WEB_CONTAINER"
echo "persistent_port=127.0.0.1:$PORT"
echo "network=$NETWORK"

echo
echo "===== 1. MATERIALIZE IMMUTABLE WEB SOURCE ====="
git -C "$WEB_REPO_ROOT" fetch --no-tags origin main >/dev/null
git -C "$WEB_REPO_ROOT" cat-file -e "${WEB_SHA}^{commit}" || fail "required web commit unavailable: $WEB_SHA"
git -C "$WEB_REPO_ROOT" worktree add --detach "$WORKTREE" "$WEB_SHA" >/dev/null
[[ -f "$WORKTREE/web/Dockerfile" ]] || fail "web Dockerfile missing at pinned commit"
echo "WEB_IMMUTABLE_WORKTREE=PASS"

echo
echo "===== 2. BUILD + STATIC CERTIFICATION ====="
docker build -t "$WEB_IMAGE" "$WORKTREE/web"
docker run --rm --entrypoint sh "$WEB_IMAGE" -lc \
  'grep -R -q "Production format" /app/.next && grep -R -q "Landscape" /app/.next && grep -R -q "stage-runs" /app/.next'
echo "WEB_BUILD_AND_FORMAT_BUNDLE=PASS"

echo
echo "===== 3. ISOLATED CANDIDATE ====="
CANDIDATE_PORT="$(free_candidate_port)"
run_web "$CANDIDATE" "$CANDIDATE_PORT" no
CANDIDATE_STARTED=1
wait_http "http://127.0.0.1:${CANDIDATE_PORT}/auth/login" candidate 45 || {
  docker logs --tail 160 "$CANDIDATE" 2>&1 || true
  fail "isolated web candidate failed"
}
echo "WEB_CANDIDATE=PASS"

echo
echo "===== 4. PREPARE PERSISTENT DEV SLOT ====="
if docker inspect "$WEB_CONTAINER" >/dev/null 2>&1; then
  echo "existing_web=$(docker inspect "$WEB_CONTAINER" --format '{{.Config.Image}}|{{.State.Status}}')"
  docker stop "$WEB_CONTAINER" >/dev/null 2>&1 || true
  docker rename "$WEB_CONTAINER" "$ROLLBACK"
  docker update --restart=no "$ROLLBACK" >/dev/null 2>&1 || true
  SWAPPED=1
  echo "DEV_WEB_ROLLBACK_SLOT=PASS"
else
  echo "existing_web=NONE"
fi

python3 - "$PORT" <<'PY'
import socket,sys
port=int(sys.argv[1]); s=socket.socket()
try:
    s.bind(('127.0.0.1',port))
except OSError as exc:
    raise SystemExit(f'FAIL: persistent dev web port {port} unavailable: {exc}')
finally:
    s.close()
print('DEV_WEB_PORT_AVAILABLE=PASS')
PY

echo
echo "===== 5. START PERSISTENT DEV WEB ====="
run_web "$WEB_CONTAINER" "$PORT" unless-stopped
NEW_STARTED=1
wait_http "http://127.0.0.1:${PORT}/auth/login" persistent-dev-web 45 || {
  docker logs --tail 180 "$WEB_CONTAINER" 2>&1 || true
  fail "persistent dev web failed"
}
echo "DEV_WEB_LOCAL_HTTP=PASS"

echo
echo "===== 6. FINALIZE DEV WEB SLOT ====="
docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
CANDIDATE_STARTED=0
if (( SWAPPED == 1 )); then
  docker rm -f "$ROLLBACK" >/dev/null 2>&1 || true
  SWAPPED=0
fi
SUCCESS=1

cat <<EOF

============================================================
 DEV WEB ACCEPTANCE RUNTIME PASS
============================================================
WEB_IMMUTABLE_WORKTREE=PASS
WEB_BUILD_AND_FORMAT_BUNDLE=PASS
WEB_CANDIDATE=PASS
DEV_WEB_LOCAL_HTTP=PASS
container=$WEB_CONTAINER
image=$WEB_IMAGE
listen=127.0.0.1:$PORT
network=$NETWORK
web_sha=$WEB_SHA

BROWSER_ACCESS=SSH_TUNNEL_REQUIRED
LAPTOP_TUNNEL=ssh -N -L 13000:127.0.0.1:13000 desifaces-dev
BROWSER_URL=http://localhost:13000/auth/login
MULTI_PERSON_URL=http://localhost:13000/app/multi-person
EOF
