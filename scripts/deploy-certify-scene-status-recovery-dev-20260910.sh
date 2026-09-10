#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
WEB_REPO_ROOT="/home/azureuser/workspace/desifaces-web"
WEB_SHA="97715a32bd88af8170bdb58729cbdaf5559d7bde"
WEB_IMAGE="desifaces-web-dev:${WEB_SHA:0:12}"
WEB_CONTAINER="df-v3-web"
NETWORK="df-v3-net"
PORT="13000"
DB="desifaces-v3-db"
WORKFLOW_ID="f9e0581d-c5cb-40de-955e-cf8da4296c3b"
STAGE_ID="7d13c846-a9a4-468d-9bac-6295ead91667"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CANDIDATE="df-v3-web-candidate-scene-status-${STAMP}"
ROLLBACK="df-v3-web-rollback-scene-status-${STAMP}"
RUN_DIR="$(mktemp -d /tmp/desifaces-scene-status-dev-XXXXXX)"
WORKTREE="$RUN_DIR/repo"
CANDIDATE_STARTED=0
SWAPPED=0
NEW_STARTED=0
SUCCESS=0

fail(){ echo "FAIL: $*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
for x in git docker curl python3 grep; do need "$x"; done
[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "run only on $EXPECTED_HOST; current=$(hostname -s)"
[[ -d "$WEB_REPO_ROOT/.git" ]] || fail "web Git checkout missing: $WEB_REPO_ROOT"
docker network inspect "$NETWORK" >/dev/null 2>&1 || fail "required dev network missing: $NETWORK"

snapshot(){ docker inspect "$1" --format '{{.Id}}|{{.State.StartedAt}}|{{.Config.Image}}' 2>/dev/null || true; }
DIRECTOR_BEFORE="$(snapshot df-v3-svc-director)"
DIRECTOR_WORKER_BEFORE="$(snapshot df-v3-svc-director-worker)"
FUSION_BEFORE="$(snapshot df-v3-svc-fusion)"
FUSION_WORKER_BEFORE="$(snapshot df-v3-svc-fusion-worker)"
EXT_BEFORE="$(snapshot df-v3-svc-fusion-extension)"
STITCH_BEFORE="$(snapshot df-v3-svc-fusion-extension-stitch-worker)"
DB_BEFORE="$(snapshot "$DB")"
REDIS_BEFORE="$(snapshot desifaces-v3-redis)"

psql_scalar(){
  local sql="$1"
  docker exec -e DF_READONLY_SQL="$sql" "$DB" sh -lc 'psql -X -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${POSTGRES_DB:-desifaces}" -c "$DF_READONLY_SQL"' | tr -d '[:space:]'
}

wait_http(){
  local url="$1" label="$2" tries="${3:-45}" code i
  for i in $(seq 1 "$tries"); do
    code="$(curl -sS --connect-timeout 2 --max-time 5 -o /tmp/df-scene-status-${$}.html -w '%{http_code}' "$url" 2>/dev/null || true)"
    echo "wait=$i target=$label http=$code"
    if [[ "$code" == "200" ]]; then return 0; fi
    sleep 2
  done
  return 1
}

free_candidate_port(){
  python3 - <<'PY'
import socket
for port in range(23001,23021):
    s=socket.socket()
    try: s.bind(('127.0.0.1',port))
    except OSError: s.close(); continue
    s.close(); print(port); raise SystemExit(0)
raise SystemExit('no free candidate port')
PY
}

run_web(){
  local name="$1" host_port="$2" restart_policy="$3"
  docker run -d --name "$name" --restart "$restart_policy" --network "$NETWORK" \
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
    --label desifaces.purpose=scene-status-recovery-acceptance \
    --label desifaces.web_sha="$WEB_SHA" \
    "$WEB_IMAGE" >/dev/null
}

cleanup(){
  local rc=$?
  set +e
  rm -f /tmp/df-scene-status-${$}.html /tmp/df-scene-openapi-${$}.json
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
echo " desifaces DEV — SCENE STATUS + STITCH RECOVERY UI"
echo "============================================================"
echo "host=$(hostname -s)"
echo "environment=DEV_ONLY"
echo "production_touch=FORBIDDEN"
echo "runtime_scope=DEV_WEB_ONLY"
echo "backend_restart=NONE"
echo "provider_generation=NONE"
echo "web_sha=$WEB_SHA"
echo "workflow_id=$WORKFLOW_ID"
echo "stage_run_id=$STAGE_ID"

echo
echo "===== 1. LIVE BACKEND RECOVERY CONTRACT ====="
curl -fsS http://127.0.0.1:18011/openapi.json -o /tmp/df-scene-openapi-${$}.json
python3 - /tmp/df-scene-openapi-${$}.json "$WORKFLOW_ID" "$STAGE_ID" <<'PY'
import json,sys
p=json.load(open(sys.argv[1]))['paths']
path='/api/director/studio-workflows/{workflow_id}/fusion-stages/{stage_run_id}/retry-stitch'
assert path in p and 'post' in p[path], path
print('DIRECTOR_RETRY_STITCH_ROUTE_LIVE=PASS')
PY
[[ "$(docker inspect df-v3-svc-fusion-extension-stitch-worker --format '{{.State.Status}}')" == "running" ]] || fail "stitch worker is not running"
echo "STITCH_WORKER_RUNNING=PASS"

echo
echo "===== 2. EXACT SCENE RECOVERY PREFLIGHT — READ ONLY ====="
SCENE_STATE="$(psql_scalar "select state from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and stage_run_id='$STAGE_ID'::uuid")"
[[ "$SCENE_STATE" == "failed" ]] || fail "expected failed Scene before recovery UI, got: $SCENE_STATE"
ASPECT="$(psql_scalar "select coalesce(metadata_json->>'aspect_ratio','') from public.v3_studio_stage_runs where stage_run_id='$STAGE_ID'::uuid")"
[[ "$ASPECT" == "16:9" ]] || fail "expected 16:9 Scene, got: $ASPECT"
SUCCEEDED="$(psql_scalar "select count(*) from public.studio_jobs j where j.studio_type='fusion' and j.status='succeeded' and (j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='$STAGE_ID' or j.payload_json #>> '{provider_options,billing_context,parent_longform_job_id}'='$STAGE_ID' or j.payload_json #>> '{provider_options,billing_context,parent_job_id}'='$STAGE_ID' or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'='$STAGE_ID' or j.payload_json #>> '{tags,billing_context,parent_longform_job_id}'='$STAGE_ID' or j.payload_json #>> '{tags,billing_context,parent_job_id}'='$STAGE_ID')")"
ACTIVE="$(psql_scalar "select count(*) from public.studio_jobs j where j.studio_type='fusion' and j.status in ('queued','processing','running','submitted','pending') and (j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='$STAGE_ID' or j.payload_json #>> '{provider_options,billing_context,parent_longform_job_id}'='$STAGE_ID' or j.payload_json #>> '{provider_options,billing_context,parent_job_id}'='$STAGE_ID' or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'='$STAGE_ID' or j.payload_json #>> '{tags,billing_context,parent_longform_job_id}'='$STAGE_ID' or j.payload_json #>> '{tags,billing_context,parent_job_id}'='$STAGE_ID')")"
[[ "$SUCCEEDED" == "7" ]] || fail "expected 7 successful preserved child videos, got: $SUCCEEDED"
[[ "$ACTIVE" == "0" ]] || fail "expected zero active child renders, got: $ACTIVE"
echo "SCENE_FAILED_16_9=PASS"
echo "PRESERVED_CHILDREN=7"
echo "ACTIVE_CHILDREN=0"
echo "STITCH_ONLY_RECOVERY_ELIGIBLE=PASS"

echo
echo "===== 3. MATERIALIZE IMMUTABLE WEB SOURCE ====="
git -C "$WEB_REPO_ROOT" fetch --no-tags origin main >/dev/null
git -C "$WEB_REPO_ROOT" cat-file -e "${WEB_SHA}^{commit}" || fail "required web commit unavailable: $WEB_SHA"
git -C "$WEB_REPO_ROOT" worktree add --detach "$WORKTREE" "$WEB_SHA" >/dev/null
[[ -f "$WORKTREE/web/Dockerfile" ]] || fail "web Dockerfile missing"
echo "WEB_IMMUTABLE_WORKTREE=PASS"

echo
echo "===== 4. BUILD + STATUS/RECOVERY CONTRACT ====="
docker build -t "$WEB_IMAGE" "$WORKTREE/web"
docker run --rm --entrypoint sh "$WEB_IMAGE" -lc \
  'grep -R -q "Resume scene assembly" /app/.next && grep -R -q "authoritative backend" /app/.next && grep -R -q "retry-stitch" /app/.next && grep -R -q "clips ready" /app/.next'
echo "WEB_AUTHORITATIVE_SCENE_STATUS_BUNDLE=PASS"

echo
echo "===== 5. ISOLATED WEB CANDIDATE ====="
CANDIDATE_PORT="$(free_candidate_port)"
run_web "$CANDIDATE" "$CANDIDATE_PORT" no
CANDIDATE_STARTED=1
wait_http "http://127.0.0.1:${CANDIDATE_PORT}/auth/login" web-candidate 45 || { docker logs --tail 160 "$CANDIDATE" 2>&1 || true; fail "candidate failed"; }
echo "WEB_CANDIDATE=PASS"

echo
echo "===== 6. PERSISTENT DEV WEB CUTOVER ====="
if docker inspect "$WEB_CONTAINER" >/dev/null 2>&1; then
  docker stop "$WEB_CONTAINER" >/dev/null 2>&1 || true
  docker rename "$WEB_CONTAINER" "$ROLLBACK"
  docker update --restart=no "$ROLLBACK" >/dev/null 2>&1 || true
  SWAPPED=1
fi
python3 - "$PORT" <<'PY'
import socket,sys
s=socket.socket()
try: s.bind(('127.0.0.1',int(sys.argv[1])))
except OSError as e: raise SystemExit(f'FAIL: port unavailable: {e}')
finally: s.close()
PY
run_web "$WEB_CONTAINER" "$PORT" unless-stopped
NEW_STARTED=1
wait_http "http://127.0.0.1:${PORT}/auth/login" persistent-dev-web 45 || { docker logs --tail 180 "$WEB_CONTAINER" 2>&1 || true; fail "persistent dev web failed"; }
echo "DEV_WEB_CUTOVER=PASS"

echo
echo "===== 7. NON-WEB RUNTIME INVARIANTS ====="
[[ "$(snapshot df-v3-svc-director)" == "$DIRECTOR_BEFORE" ]] || fail "Director API changed unexpectedly"
[[ "$(snapshot df-v3-svc-director-worker)" == "$DIRECTOR_WORKER_BEFORE" ]] || fail "Director worker changed unexpectedly"
[[ "$(snapshot df-v3-svc-fusion)" == "$FUSION_BEFORE" ]] || fail "Fusion API changed unexpectedly"
[[ "$(snapshot df-v3-svc-fusion-worker)" == "$FUSION_WORKER_BEFORE" ]] || fail "Fusion worker changed unexpectedly"
[[ "$(snapshot df-v3-svc-fusion-extension)" == "$EXT_BEFORE" ]] || fail "Fusion Extension changed unexpectedly"
[[ "$(snapshot df-v3-svc-fusion-extension-stitch-worker)" == "$STITCH_BEFORE" ]] || fail "stitch worker changed unexpectedly"
[[ "$(snapshot "$DB")" == "$DB_BEFORE" ]] || fail "DB changed unexpectedly"
[[ "$(snapshot desifaces-v3-redis)" == "$REDIS_BEFORE" ]] || fail "Redis changed unexpectedly"
echo "BACKEND_RUNTIME_UNCHANGED=PASS"
echo "DB_REDIS_UNCHANGED=PASS"

docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
CANDIDATE_STARTED=0
if (( SWAPPED == 1 )); then docker rm -f "$ROLLBACK" >/dev/null 2>&1 || true; SWAPPED=0; fi
SUCCESS=1

cat <<EOF

============================================================
 DEV SCENE STATUS + STITCH RECOVERY UI PASS
============================================================
DIRECTOR_RETRY_STITCH_ROUTE_LIVE=PASS
STITCH_WORKER_RUNNING=PASS
SCENE_FAILED_16_9=PASS
PRESERVED_CHILDREN=7
ACTIVE_CHILDREN=0
STITCH_ONLY_RECOVERY_ELIGIBLE=PASS
WEB_IMMUTABLE_WORKTREE=PASS
WEB_AUTHORITATIVE_SCENE_STATUS_BUNDLE=PASS
WEB_CANDIDATE=PASS
DEV_WEB_CUTOVER=PASS
BACKEND_RUNTIME_UNCHANGED=PASS
DB_REDIS_UNCHANGED=PASS
PRODUCTION_TOUCH=NONE
web_sha=$WEB_SHA
NEXT=HARD_REFRESH_EXISTING_STORY_THEN_CLICK_RESUME_SCENE_ASSEMBLY
EOF
