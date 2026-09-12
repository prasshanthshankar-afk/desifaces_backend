#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-gpu"
ROOT="${ROOT:-/home/azureuser/workspace/desifaces}"
BASE_COMPOSE="$ROOT/docker-compose.yml"
PROD_COMPOSE="$ROOT/deploy/production/docker-compose.v3-app.production.yml"
ENV_FILE="$ROOT/infra/.env"
PROJECT="desifaces"
BACKEND_REPO="prasshanthshankar-afk/desifaces_backend"
WEB_REPO="prasshanthshankar-afk/desifaces_web"
BACKEND_COMMIT="f459ae128d0bf5e2b0d6b63dfaf476df11c4731b"
WEB_COMMIT="0e832f2d14e8f7cfe1d74975ac1277c8f476bfdc"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/home/azureuser/backups/multiperson-aspect-face-readurl-$STAMP"

FACE_C="df-svc-face"
FACE_WORKER_C="df-svc-face-worker"
DIRECTOR_C="df-v3-svc-director"
DIRECTOR_WORKER_C="df-v3-svc-director-worker"
AUDIO_C="df-svc-audio"
AUDIO_WORKER_C="df-svc-audio-worker"
FUSION_C="df-svc-fusion"
FUSION_WORKER_C="df-svc-fusion-worker"
DB_C="desifaces-db"
REDIS_C="desifaces-redis"
WEB_C="df-v3-web-prod"
WEB_PORT=13001
CANDIDATE_PORT=13002
CANDIDATE="df-v3-web-prod-aspect-candidate-$STAMP"
ROLLBACK_WEB="df-v3-web-prod-aspect-rollback-$STAMP"
WEB_IMAGE="desifaces-web-production:$WEB_COMMIT"

fail(){ echo "FAIL: $*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"; }
for x in curl docker python3 grep; do need "$x"; done
[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "wrong host: $(hostname -s)"
[[ -d "$ROOT" ]] || fail "missing backend root: $ROOT"
[[ -f "$BASE_COMPOSE" ]] || fail "missing $BASE_COMPOSE"
[[ -f "$PROD_COMPOSE" ]] || fail "missing $PROD_COMPOSE"
[[ -f "$ENV_FILE" ]] || fail "missing $ENV_FILE"

WEB_SRC="${WEB_SRC:-}"
if [[ -z "$WEB_SRC" ]]; then
  for candidate in \
    /home/azureuser/workspace/desifaces-web/web \
    /home/azureuser/workspace/desifaces-web-review/web \
    /home/azureuser/workspace/desifaces_web/web; do
    if [[ -f "$candidate/Dockerfile" && -f "$candidate/components/MultiPersonDirector.tsx" ]]; then
      WEB_SRC="$candidate"
      break
    fi
  done
fi
[[ -n "$WEB_SRC" && -d "$WEB_SRC" ]] || fail "active production web source not found"
echo "WEB_SOURCE=$WEB_SRC"

for c in "$FACE_C" "$FACE_WORKER_C" "$DIRECTOR_C" "$DIRECTOR_WORKER_C" "$AUDIO_C" "$AUDIO_WORKER_C" "$FUSION_C" "$FUSION_WORKER_C" "$DB_C" "$REDIS_C" "$WEB_C"; do
  docker inspect "$c" >/dev/null 2>&1 || fail "required production container missing: $c"
done

COMPOSE=(docker compose --project-directory "$ROOT" -p "$PROJECT" --env-file "$ENV_FILE" -f "$BASE_COMPOSE" -f "$PROD_COMPOSE")
mkdir -p "$BACKUP/backend" "$BACKUP/web"

BACKEND_FILES=(
  services/svc-director/app/app/face_execution_runtime.py
  services/svc-director/app/app/fusion_input_performance.py
  services/svc-director/app/app/studio_aspect_routes.py
  services/svc-director/app/app/studio_routes_runtime.py
  services/svc-face/app/app/api/routes/face_media.py
  services/svc-face/app/app/api/__init__.py
)
WEB_FILES=(
  lib/multiperson-aspect.ts
  lib/client.ts
  components/MultiPersonAspectControls.tsx
  app/app/multi-person/multi-person-aspect.css
  app/app/multi-person/layout.tsx
)

backup_file(){
  local root="$1" rel="$2" bucket="$3"
  mkdir -p "$BACKUP/$bucket/$(dirname "$rel")"
  if [[ -f "$root/$rel" ]]; then
    cp -a "$root/$rel" "$BACKUP/$bucket/$rel"
  else
    : > "$BACKUP/$bucket/$rel.__MISSING__"
  fi
}
restore_bucket(){
  local root="$1" bucket="$2" rel
  while IFS= read -r -d '' marker; do
    rel="${marker#"$BACKUP/$bucket/"}"
    rel="${rel%.__MISSING__}"
    rm -f "$root/$rel"
  done < <(find "$BACKUP/$bucket" -type f -name '*.__MISSING__' -print0)
  while IFS= read -r -d '' file; do
    [[ "$file" == *.__MISSING__ ]] && continue
    rel="${file#"$BACKUP/$bucket/"}"
    mkdir -p "$root/$(dirname "$rel")"
    cp -a "$file" "$root/$rel"
  done < <(find "$BACKUP/$bucket" -type f -print0)
}
for f in "${BACKEND_FILES[@]}"; do backup_file "$ROOT" "$f" backend; done
for f in "${WEB_FILES[@]}"; do backup_file "$WEB_SRC" "$f" web; done

snapshot(){ docker inspect "$1" --format '{{.Id}}|{{.State.StartedAt}}|{{.Image}}|{{.Config.Image}}'; }
DB_BEFORE="$(snapshot "$DB_C")"
REDIS_BEFORE="$(snapshot "$REDIS_C")"
FACE_WORKER_BEFORE="$(snapshot "$FACE_WORKER_C")"
DIRECTOR_WORKER_BEFORE="$(snapshot "$DIRECTOR_WORKER_C")"
AUDIO_BEFORE="$(snapshot "$AUDIO_C")"
AUDIO_WORKER_BEFORE="$(snapshot "$AUDIO_WORKER_C")"
FUSION_BEFORE="$(snapshot "$FUSION_C")"
FUSION_WORKER_BEFORE="$(snapshot "$FUSION_WORKER_C")"
FACE_IMAGE_ID_BEFORE="$(docker inspect "$FACE_C" --format '{{.Image}}')"
FACE_IMAGE_REF="$(docker inspect "$FACE_C" --format '{{.Config.Image}}')"
DIRECTOR_IMAGE_ID_BEFORE="$(docker inspect "$DIRECTOR_C" --format '{{.Image}}')"
DIRECTOR_IMAGE_REF="$(docker inspect "$DIRECTOR_C" --format '{{.Config.Image}}')"
WEB_IMAGE_BEFORE="$(docker inspect "$WEB_C" --format '{{.Config.Image}}')"

FACE_RECREATED=0
DIRECTOR_RECREATED=0
WEB_SWAPPED=0
CANDIDATE_STARTED=0
SUCCESS=0

run_web(){
  local name="$1" port="$2" image="$3"
  docker run -d --name "$name" --restart unless-stopped --network df-net \
    -p "127.0.0.1:${port}:3000" \
    -e CORE_BASE_URL=http://svc-core:8000 \
    -e DASHBOARD_BASE_URL=http://svc-dashboard:8005 \
    -e FACE_BASE_URL=http://svc-face:8003 \
    -e AUDIO_BASE_URL=http://svc-audio:8004 \
    -e FUSION_BASE_URL=http://svc-fusion:8002 \
    -e DIRECTOR_BASE_URL=http://svc-director:8011 \
    -e FUSION_EXTENSION_BASE_URL=http://svc-fusion-extension:8006 \
    -e PRICING_BASE_URL=http://svc-pricing:8009 \
    -e COMMERCE_BASE_URL=http://svc-commerce:8008 \
    -e NOTIFICATION_BASE_URL=http://svc-core:8000 \
    -e ASSISTANT_BASE_URL=http://svc-assistant:8012 \
    -e COOKIE_SECURE=true \
    "$image" >/dev/null
}

wait_http(){
  local name="$1" url="$2" tries="${3:-45}" i code
  for i in $(seq 1 "$tries"); do
    code="$(curl -sS --max-time 5 -o /tmp/df-aspect-http.$$ -w '%{http_code}' "$url" 2>/dev/null || true)"
    echo "wait=$i target=$name http=$code"
    [[ "$code" == "200" ]] && return 0
    sleep 2
  done
  return 1
}

rollback(){
  local rc=$?
  set +e
  if (( rc != 0 )) && (( SUCCESS == 0 )); then
    echo
    echo "===== AUTOMATIC ROLLBACK ====="
    if (( WEB_SWAPPED == 1 )); then
      docker rm -f "$WEB_C" >/dev/null 2>&1 || true
      if docker inspect "$ROLLBACK_WEB" >/dev/null 2>&1; then
        docker rename "$ROLLBACK_WEB" "$WEB_C" >/dev/null 2>&1 || true
        docker update --restart=unless-stopped "$WEB_C" >/dev/null 2>&1 || true
        docker start "$WEB_C" >/dev/null 2>&1 || true
        echo "WEB_ROLLBACK=ATTEMPTED"
      fi
    fi
    (( CANDIDATE_STARTED == 1 )) && docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
    restore_bucket "$ROOT" backend
    restore_bucket "$WEB_SRC" web
    if (( FACE_RECREATED == 1 )); then
      docker tag "$FACE_IMAGE_ID_BEFORE" "$FACE_IMAGE_REF" >/dev/null 2>&1 || true
      "${COMPOSE[@]}" up -d --no-deps --force-recreate svc-face >/dev/null 2>&1 || true
      echo "FACE_API_ROLLBACK=ATTEMPTED"
    fi
    if (( DIRECTOR_RECREATED == 1 )); then
      docker tag "$DIRECTOR_IMAGE_ID_BEFORE" "$DIRECTOR_IMAGE_REF" >/dev/null 2>&1 || true
      "${COMPOSE[@]}" up -d --no-deps --force-recreate svc-director >/dev/null 2>&1 || true
      echo "DIRECTOR_API_ROLLBACK=ATTEMPTED"
    fi
    echo "BACKUP=$BACKUP"
  fi
  exit "$rc"
}
trap rollback EXIT

cat <<EOF
============================================================
 desifaces PROD — MULTI-PERSON ASPECT + FACE READ-URL REPAIR
============================================================
host=$(hostname -s)
backend_root=$ROOT
web_source=$WEB_SRC
backend_commit=$BACKEND_COMMIT
web_commit=$WEB_COMMIT
aspect_ratios=9:16,16:9,1:1
scope=Face API + Director API + Multi-Person web
Face_worker_restart=FORBIDDEN
Director_worker_restart=FORBIDDEN
Audio_restart=FORBIDDEN
Fusion_service_restart=FORBIDDEN
DB_change=NONE
Redis_change=NONE
backup=$BACKUP
EOF

echo
echo "===== 1. SYNC IMMUTABLE SOURCE ====="
for f in "${BACKEND_FILES[@]}"; do
  mkdir -p "$ROOT/$(dirname "$f")"
  curl -fsSL "https://raw.githubusercontent.com/$BACKEND_REPO/$BACKEND_COMMIT/$f" -o "$ROOT/$f"
done
for f in "${WEB_FILES[@]}"; do
  mkdir -p "$WEB_SRC/$(dirname "$f")"
  curl -fsSL "https://raw.githubusercontent.com/$WEB_REPO/$WEB_COMMIT/web/$f" -o "$WEB_SRC/$f"
done
echo "SOURCE_SYNC=PASS"

echo
echo "===== 2. STATIC CONTRACT GATES ====="
python3 -m py_compile \
  "$ROOT/services/svc-director/app/app/face_execution_runtime.py" \
  "$ROOT/services/svc-director/app/app/fusion_input_performance.py" \
  "$ROOT/services/svc-director/app/app/studio_aspect_routes.py" \
  "$ROOT/services/svc-director/app/app/studio_routes_runtime.py" \
  "$ROOT/services/svc-face/app/app/api/routes/face_media.py" \
  "$ROOT/services/svc-face/app/app/api/__init__.py"
grep -Fq '"9:16", "16:9", "1:1"' "$ROOT/services/svc-director/app/app/studio_aspect_routes.py"
grep -Fq 'video: dict[str, Any] = {"aspect_ratio": aspect_ratio}' "$ROOT/services/svc-director/app/app/fusion_input_performance.py"
grep -Fq 'studio_input["aspect_ratio"]' "$ROOT/services/svc-director/app/app/face_execution_runtime.py"
grep -Fq '@router.get("/assets/{media_asset_id}/read-url")' "$ROOT/services/svc-face/app/app/api/routes/face_media.py"
grep -Fq 'MultiPersonAspectControls' "$WEB_SRC/app/app/multi-person/layout.tsx"
grep -Fq 'Landscape' "$WEB_SRC/components/MultiPersonAspectControls.tsx"
grep -Fq 'multiPersonAspectPath' "$WEB_SRC/lib/client.ts"
echo "STATIC_CONTRACT=PASS"

echo
echo "===== 3. COMPOSE INTERPOLATION ====="
"${COMPOSE[@]}" config svc-face svc-director >/dev/null
echo "COMPOSE_INTERPOLATION=PASS"

echo
echo "===== 4. BUILD FACE + DIRECTOR API IMAGES ====="
"${COMPOSE[@]}" build svc-face svc-director

echo
echo "===== 5. PRE-DEPLOY BACKEND CONTRACT PROOF ====="
"${COMPOSE[@]}" run --rm --no-deps -T --entrypoint python svc-face - <<'PY'
from app.main import app
paths = {getattr(r, "path", "") for r in app.routes}
assert "/api/face/assets/{media_asset_id}/read-url" in paths, sorted(p for p in paths if "face/assets" in p)
print("FACE_READ_URL_ROUTE_IMAGE=PASS")
PY
"${COMPOSE[@]}" run --rm --no-deps -T --entrypoint python svc-director - <<'PY'
from types import SimpleNamespace
from pydantic import ValidationError
from app.main import app
import app.face_execution_runtime as face_runtime
from app.fusion_input_performance import _scene_aspect_ratio
from app.studio_aspect_routes import StageAspectIn
paths = {getattr(r, "path", "") for r in app.routes}
assert "/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/aspect-ratio" in paths
for ratio in ("9:16", "16:9", "1:1"):
    assert StageAspectIn(aspect_ratio=ratio).aspect_ratio == ratio
    assert _scene_aspect_ratio(SimpleNamespace(stage_metadata={"aspect_ratio": ratio})) == ratio
face_runtime._original_base_compile_context_face_input = lambda context: {"aspect_ratio": "9:16"}
for ratio in ("9:16", "16:9", "1:1"):
    value = face_runtime.compile_context_face_input_with_stage_aspect(SimpleNamespace(metadata={"aspect_ratio": ratio}))
    assert value["aspect_ratio"] == ratio
try:
    StageAspectIn(aspect_ratio="4:5")
except ValidationError:
    pass
else:
    raise AssertionError("unsupported aspect accepted")
print("DIRECTOR_ASPECT_ROUTE_IMAGE=PASS")
print("FACE_ASPECT_PROPAGATION_IMAGE=PASS")
print("FUSION_ASPECT_PROPAGATION_IMAGE=PASS")
PY

echo
echo "===== 6. BUILD + CERTIFY WEB IMAGE ====="
docker build -t "$WEB_IMAGE" "$WEB_SRC"
docker run --rm --entrypoint sh "$WEB_IMAGE" -lc \
  'grep -R -q "Production format" /app/.next && grep -R -q "Landscape" /app/.next && grep -R -q "desifaces:multiperson:face-aspect" /app/.next && grep -R -q "stage-runs" /app/.next'
echo "WEB_BUILD_AND_FORMAT_BUNDLE=PASS"

echo
echo "===== 7. START ISOLATED WEB CANDIDATE ====="
python3 - "$CANDIDATE_PORT" <<'PY'
import socket, sys
p=int(sys.argv[1]); s=socket.socket()
try: s.bind(("127.0.0.1",p))
except OSError as exc: raise SystemExit(f"candidate port {p} unavailable: {exc}")
finally: s.close()
print("CANDIDATE_PORT_AVAILABLE=PASS")
PY
docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
run_web "$CANDIDATE" "$CANDIDATE_PORT" "$WEB_IMAGE"
CANDIDATE_STARTED=1
wait_http web-candidate "http://127.0.0.1:${CANDIDATE_PORT}/auth/login" 45 || {
  docker logs --tail 160 "$CANDIDATE" 2>&1 || true
  fail "web candidate failed"
}
echo "WEB_CANDIDATE=PASS"

echo
echo "===== 8. PROMOTE FACE API ONLY ====="
"${COMPOSE[@]}" up -d --no-deps --force-recreate svc-face
FACE_RECREATED=1
wait_http face-api "http://127.0.0.1:8003/api/health" 40 || {
  docker logs --tail 180 "$FACE_C" 2>&1 || true
  fail "Face API health failed"
}
FACE_ROUTES="$(docker exec -i "$FACE_C" python - <<'PY'
from app.main import app
for r in sorted(app.routes, key=lambda x: getattr(x,'path','')):
    p=getattr(r,'path','')
    if 'face/assets' in p:
        print(','.join(sorted(getattr(r,'methods',[]) or [])),p)
PY
)"
printf '%s\n' "$FACE_ROUTES"
grep -Fq '/api/face/assets/{media_asset_id}/read-url' <<<"$FACE_ROUTES" || fail "Face read-url route missing after promotion"
FACE_AUTH="$(curl -sS -o /tmp/df-face-aspect-auth.$$ -w '%{http_code}' http://127.0.0.1:8003/api/face/assets/00000000-0000-0000-0000-000000000000/read-url || true)"
[[ "$FACE_AUTH" == "401" ]] || fail "Face read-url auth guard expected 401, got $FACE_AUTH"
echo "FACE_READ_URL_ROUTE=PASS"
echo "FACE_READ_URL_AUTH=PASS"

echo
echo "===== 9. PROMOTE DIRECTOR API ONLY ====="
"${COMPOSE[@]}" up -d --no-deps --force-recreate svc-director
DIRECTOR_RECREATED=1
wait_http director-api "http://127.0.0.1:18011/api/health" 40 || {
  docker logs --tail 200 "$DIRECTOR_C" 2>&1 || true
  fail "Director API health failed"
}
DIRECTOR_ROUTES="$(docker exec -i "$DIRECTOR_C" python - <<'PY'
from app.main import app
for r in sorted(app.routes, key=lambda x: getattr(x,'path','')):
    p=getattr(r,'path','')
    if 'aspect-ratio' in p:
        print(','.join(sorted(getattr(r,'methods',[]) or [])),p)
PY
)"
printf '%s\n' "$DIRECTOR_ROUTES"
grep -Fq '/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/aspect-ratio' <<<"$DIRECTOR_ROUTES" || fail "Director aspect route missing"
echo "DIRECTOR_ASPECT_ROUTE=PASS"

echo
echo "===== 10. DIRECTOR -> FACE NETWORK CONTRACT ====="
NETWORK_CODE="$(docker exec -i "$DIRECTOR_C" python - <<'PY'
import urllib.error, urllib.request
u='http://svc-face:8003/api/face/assets/00000000-0000-0000-0000-000000000000/read-url'
try:
    urllib.request.urlopen(u,timeout=5); print('200')
except urllib.error.HTTPError as e: print(e.code)
PY
)"
[[ "$NETWORK_CODE" == "401" ]] || fail "Director->Face read-url route expected auth 401, got $NETWORK_CODE"
echo "DIRECTOR_FACE_READ_URL_NETWORK=PASS"

echo
echo "===== 11. CUT OVER WEB WITH ROLLBACK SLOT ====="
docker stop "$WEB_C" >/dev/null
if ! docker rename "$WEB_C" "$ROLLBACK_WEB"; then
  docker start "$WEB_C" >/dev/null 2>&1 || true
  fail "failed to create web rollback slot"
fi
WEB_SWAPPED=1
docker update --restart=no "$ROLLBACK_WEB" >/dev/null 2>&1 || true
run_web "$WEB_C" "$WEB_PORT" "$WEB_IMAGE"
wait_http production-web "http://127.0.0.1:${WEB_PORT}/auth/login" 45 || {
  docker logs --tail 180 "$WEB_C" 2>&1 || true
  fail "production web failed"
}
wait_http public-web "https://web.desifaces.ai/auth/login" 30 || fail "public web failed"
echo "WEB_PRODUCTION_CUTOVER=PASS"

echo
echo "===== 12. NON-TARGET RUNTIME INVARIANTS ====="
[[ "$DB_BEFORE" == "$(snapshot "$DB_C")" ]] || fail "DB changed or restarted"
[[ "$REDIS_BEFORE" == "$(snapshot "$REDIS_C")" ]] || fail "Redis changed or restarted"
[[ "$FACE_WORKER_BEFORE" == "$(snapshot "$FACE_WORKER_C")" ]] || fail "Face worker changed or restarted"
[[ "$DIRECTOR_WORKER_BEFORE" == "$(snapshot "$DIRECTOR_WORKER_C")" ]] || fail "Director worker changed or restarted"
[[ "$AUDIO_BEFORE" == "$(snapshot "$AUDIO_C")" ]] || fail "Audio API changed or restarted"
[[ "$AUDIO_WORKER_BEFORE" == "$(snapshot "$AUDIO_WORKER_C")" ]] || fail "Audio worker changed or restarted"
[[ "$FUSION_BEFORE" == "$(snapshot "$FUSION_C")" ]] || fail "Fusion API changed or restarted"
[[ "$FUSION_WORKER_BEFORE" == "$(snapshot "$FUSION_WORKER_C")" ]] || fail "Fusion worker changed or restarted"
echo "DB_REDIS_UNCHANGED=PASS"
echo "FACE_WORKER_UNCHANGED=PASS"
echo "DIRECTOR_WORKER_UNCHANGED=PASS"
echo "AUDIO_RUNTIME_UNCHANGED=PASS"
echo "FUSION_RUNTIME_UNCHANGED=PASS"

echo
echo "===== 13. FINALIZE ====="
docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
CANDIDATE_STARTED=0
docker rm -f "$ROLLBACK_WEB" >/dev/null 2>&1 || true
WEB_SWAPPED=0
SUCCESS=1

cat <<EOF

============================================================
 PROD MULTI-PERSON ASPECT + FACE READ-URL PASS
============================================================
SOURCE_SYNC=PASS
STATIC_CONTRACT=PASS
COMPOSE_INTERPOLATION=PASS
FACE_READ_URL_ROUTE_IMAGE=PASS
DIRECTOR_ASPECT_ROUTE_IMAGE=PASS
FACE_ASPECT_PROPAGATION_IMAGE=PASS
FUSION_ASPECT_PROPAGATION_IMAGE=PASS
WEB_BUILD_AND_FORMAT_BUNDLE=PASS
WEB_CANDIDATE=PASS
FACE_READ_URL_ROUTE=PASS
FACE_READ_URL_AUTH=PASS
DIRECTOR_ASPECT_ROUTE=PASS
DIRECTOR_FACE_READ_URL_NETWORK=PASS
WEB_PRODUCTION_CUTOVER=PASS
DB_REDIS_UNCHANGED=PASS
FACE_WORKER_UNCHANGED=PASS
DIRECTOR_WORKER_UNCHANGED=PASS
AUDIO_RUNTIME_UNCHANGED=PASS
FUSION_RUNTIME_UNCHANGED=PASS
backend_commit=$BACKEND_COMMIT
web_commit=$WEB_COMMIT
old_web_image=$WEB_IMAGE_BEFORE
new_web_image=$WEB_IMAGE
NEXT=REFRESH_EXISTING_MULTI_PERSON_STORY_SELECT_SCENE_FORMAT_AND_CHECK_PRICE
EOF
