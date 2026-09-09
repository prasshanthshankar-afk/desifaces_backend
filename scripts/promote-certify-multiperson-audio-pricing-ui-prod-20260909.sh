#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-gpu"
ROOT="${ROOT:-/home/azureuser/workspace/desifaces}"
WEB_SRC="${WEB_SRC:-$ROOT/web-app/web}"
BASE_COMPOSE="${BASE_COMPOSE:-$ROOT/docker-compose.yml}"
PROD_COMPOSE="${PROD_COMPOSE:-$ROOT/deploy/production/docker-compose.v3-app.production.yml}"
ENV_FILE="${ENV_FILE:-$ROOT/infra/.env}"
PROJECT="${PROJECT:-desifaces}"

DIRECTOR_C="df-v3-svc-director"
DIRECTOR_WORKER_C="df-v3-svc-director-worker"
WEB_C="df-v3-web-prod"
DB_C="desifaces-db"
REDIS_C="desifaces-redis"
AUDIO_C="df-svc-audio"
AUDIO_WORKER_C="df-svc-audio-worker"

SOURCE_COMMIT="36bd83ab7a1612814aaba05fa07f7b92ec6bd2d6"
WEB_COMMIT="27e7bbd54decb3ab26462db56bd5916d653c1b60"
REPO="prasshanthshankar-afk/desifaces_backend"
WEB_IMAGE="desifaces-web-production:$WEB_COMMIT"
WEB_PORT="13001"
CANDIDATE_PORT="13002"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CANDIDATE="df-v3-web-prod-candidate-$STAMP"
ROLLBACK_WEB="df-v3-web-prod-rollback-$STAMP"
BACKUP="/home/azureuser/backups/multiperson-audio-pricing-ui-$STAMP"

DIRECTOR_SOURCE_REL="services/svc-director/app/app/audio_execution_runtime.py"
DIRECTOR_SOURCE="$ROOT/$DIRECTOR_SOURCE_REL"
ROUTE_DIR="$WEB_SRC/app/app/multi-person"
WEB_CSS="$ROUTE_DIR/multi-person-polish.css"
WEB_LAYOUT="$ROUTE_DIR/layout.tsx"
WEB_COMPONENT="$WEB_SRC/components/MultiPersonDirector.tsx"

fail(){ echo "FAIL: $*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }

for x in curl docker python3 grep; do need "$x"; done
[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "run on $EXPECTED_HOST; current=$(hostname -s)"
[[ -d "$ROOT" ]] || fail "production root missing: $ROOT"
[[ -d "$WEB_SRC" ]] || fail "canonical production web source missing: $WEB_SRC"
[[ -f "$WEB_COMPONENT" ]] || fail "Multi-Person component missing: $WEB_COMPONENT"
[[ -f "$BASE_COMPOSE" ]] || fail "base compose missing: $BASE_COMPOSE"
[[ -f "$PROD_COMPOSE" ]] || fail "production compose overlay missing: $PROD_COMPOSE"
[[ -f "$ENV_FILE" ]] || fail "production env missing: $ENV_FILE"
[[ -f "$DIRECTOR_SOURCE" ]] || fail "Director Audio runtime missing: $DIRECTOR_SOURCE"

grep -q 'audio-price-all-strong' "$WEB_COMPONENT" || fail "unexpected web baseline: bulk Audio pricing control not found"
grep -q 'mini-action strong' "$WEB_COMPONENT" || fail "unexpected web baseline: per-line pricing control not found"

for c in "$DIRECTOR_C" "$DIRECTOR_WORKER_C" "$WEB_C" "$DB_C" "$REDIS_C" "$AUDIO_C" "$AUDIO_WORKER_C"; do
  docker inspect "$c" >/dev/null 2>&1 || fail "required production container missing: $c"
done

COMPOSE=(docker compose --project-directory "$ROOT" -p "$PROJECT" --env-file "$ENV_FILE" -f "$BASE_COMPOSE" -f "$PROD_COMPOSE")
mkdir -p "$BACKUP"

DB_ID_BEFORE="$(docker inspect "$DB_C" --format '{{.Id}}')"
DB_STARTED_BEFORE="$(docker inspect "$DB_C" --format '{{.State.StartedAt}}')"
REDIS_ID_BEFORE="$(docker inspect "$REDIS_C" --format '{{.Id}}')"
REDIS_STARTED_BEFORE="$(docker inspect "$REDIS_C" --format '{{.State.StartedAt}}')"
WORKER_ID_BEFORE="$(docker inspect "$DIRECTOR_WORKER_C" --format '{{.Id}}')"
WORKER_STARTED_BEFORE="$(docker inspect "$DIRECTOR_WORKER_C" --format '{{.State.StartedAt}}')"
AUDIO_ID_BEFORE="$(docker inspect "$AUDIO_C" --format '{{.Id}}')"
AUDIO_WORKER_ID_BEFORE="$(docker inspect "$AUDIO_WORKER_C" --format '{{.Id}}')"
DIRECTOR_IMAGE_BEFORE="$(docker inspect "$DIRECTOR_C" --format '{{.Image}}')"
DIRECTOR_IMAGE_REF="$(docker inspect "$DIRECTOR_C" --format '{{.Config.Image}}')"
WEB_IMAGE_BEFORE="$(docker inspect "$WEB_C" --format '{{.Config.Image}}')"

cp -a "$DIRECTOR_SOURCE" "$BACKUP/audio_execution_runtime.py.before"
[[ -f "$WEB_CSS" ]] && cp -a "$WEB_CSS" "$BACKUP/multi-person-polish.css.before" || true
[[ -f "$WEB_LAYOUT" ]] && cp -a "$WEB_LAYOUT" "$BACKUP/layout.tsx.before" || true

DIRECTOR_RECREATED=0
WEB_SWAPPED=0
CANDIDATE_STARTED=0
SUCCESS=0

restore_sources(){
  cp -a "$BACKUP/audio_execution_runtime.py.before" "$DIRECTOR_SOURCE" 2>/dev/null || true
  if [[ -f "$BACKUP/multi-person-polish.css.before" ]]; then cp -a "$BACKUP/multi-person-polish.css.before" "$WEB_CSS"; else rm -f "$WEB_CSS"; fi
  if [[ -f "$BACKUP/layout.tsx.before" ]]; then cp -a "$BACKUP/layout.tsx.before" "$WEB_LAYOUT"; else rm -f "$WEB_LAYOUT"; fi
}

rollback(){
  rc=$?
  set +e
  if (( rc != 0 )) && (( SUCCESS == 0 )); then
    echo
    echo "===== AUTOMATIC ROLLBACK ====="
    if (( WEB_SWAPPED == 1 )); then
      docker rm -f "$WEB_C" >/dev/null 2>&1 || true
      if docker inspect "$ROLLBACK_WEB" >/dev/null 2>&1; then
        docker rename "$ROLLBACK_WEB" "$WEB_C" >/dev/null 2>&1 || true
        docker start "$WEB_C" >/dev/null 2>&1 || true
        echo "WEB_ROLLBACK=ATTEMPTED"
      fi
    fi
    (( CANDIDATE_STARTED == 1 )) && docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
    restore_sources
    if (( DIRECTOR_RECREATED == 1 )); then
      docker tag "$DIRECTOR_IMAGE_BEFORE" "$DIRECTOR_IMAGE_REF" >/dev/null 2>&1 || true
      "${COMPOSE[@]}" up -d --no-deps --force-recreate svc-director >/dev/null 2>&1 || true
      echo "DIRECTOR_ROLLBACK=ATTEMPTED"
    fi
    echo "BACKUP=$BACKUP"
  fi
  exit "$rc"
}
trap rollback EXIT

wait_http(){
  local name="$1" url="$2" tries="${3:-40}" i code
  for i in $(seq 1 "$tries"); do
    code="$(curl -sS --max-time 5 -o /tmp/df-hotfix-http.$$ -w '%{http_code}' "$url" 2>/dev/null || true)"
    echo "wait=$i target=$name http=$code"
    [[ "$code" == "200" ]] && return 0
    sleep 2
  done
  return 1
}

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

cat <<EOF
============================================================
 desifaces PROD — MULTI-PERSON AUDIO PRICING + UI HOTFIX
============================================================
host=$(hostname -s)
backend_root=$ROOT
web_source=$WEB_SRC
backend_source_commit=$SOURCE_COMMIT
web_git_commit=$WEB_COMMIT
scope=Director Audio payload contract + Multi-Person web pricing UI
Director_worker_restart=FORBIDDEN
Audio_restart=FORBIDDEN
DB_change=NONE
Redis_change=NONE
backup=$BACKUP
EOF

echo
echo "===== 1. SYNC IMMUTABLE HOTFIX SOURCE ====="
curl -fsSL "https://raw.githubusercontent.com/$REPO/$SOURCE_COMMIT/$DIRECTOR_SOURCE_REL" -o "$DIRECTOR_SOURCE"
mkdir -p "$ROUTE_DIR"
curl -fsSL "https://raw.githubusercontent.com/$REPO/$SOURCE_COMMIT/deploy/production/hotfixes/20260909/multi-person-polish.css" -o "$WEB_CSS"
curl -fsSL "https://raw.githubusercontent.com/$REPO/$SOURCE_COMMIT/deploy/production/hotfixes/20260909/multi-person-layout.tsx" -o "$WEB_LAYOUT"
echo "SOURCE_SYNC=PASS"

echo
echo "===== 2. STATIC CONTRACT GATES ====="
python3 - "$DIRECTOR_SOURCE" <<'PY'
import ast, pathlib, sys
s=pathlib.Path(sys.argv[1]).read_text()
ast.parse(s)
for marker in (
    '_NUMERIC_DELIVERY_FIELDS = ("style_degree", "rate", "pitch", "volume")',
    'def _finite_audio_number',
    'math.isfinite(number)',
    'studio_input.pop(key, None)',
    'delivery_direction=',
    '_preserve_qualitative_direction(studio_input, qualitative_notes)',
):
    assert marker in s, marker
print('AUDIO_PAYLOAD_STATIC_CONTRACT=PASS')
PY
grep -q 'audio-price-all-strong' "$WEB_CSS"
grep -q 'production-stage-card .stage-actions .mini-action.strong' "$WEB_CSS"
grep -q 'import "./multi-person-polish.css"' "$WEB_LAYOUT"
echo "WEB_UI_STATIC_CONTRACT=PASS"

echo
echo "===== 3. PRODUCTION COMPOSE GATE ====="
"${COMPOSE[@]}" config svc-director >/dev/null
echo "COMPOSE_INTERPOLATION=PASS"

echo
echo "===== 4. BUILD DIRECTOR HOTFIX IMAGE ====="
"${COMPOSE[@]}" build svc-director

echo
echo "===== 5. RUNTIME AUDIO PAYLOAD PROOF ====="
"${COMPOSE[@]}" run --rm --no-deps -T --entrypoint python svc-director - <<'PY'
from app.audio_execution_runtime import (
    _finite_audio_number,
    _preserve_qualitative_direction,
    _sanitize_numeric_delivery,
)

payload = {
    "context": "story_dialogue proof",
    "volume": "Softer than the previous line",
    "rate": "1.05",
    "pitch": 0.0,
    "style_degree": "0.70",
}
notes = _sanitize_numeric_delivery(payload)
assert "volume" not in payload, payload
assert payload["rate"] == 1.05, payload
assert payload["pitch"] == 0.0, payload
assert payload["style_degree"] == 0.70, payload
assert notes == ["volume: Softer than the previous line"], notes
_preserve_qualitative_direction(payload, notes)
assert "delivery_direction=volume: Softer than the previous line" in payload["context"]
assert _finite_audio_number("NaN") is None
assert _finite_audio_number("Infinity") is None
print("AUDIO_QUALITATIVE_VOLUME_SANITIZED=PASS")
print("AUDIO_NUMERIC_CONTROLS_NORMALIZED=PASS")
print("AUDIO_CREATIVE_DIRECTION_PRESERVED=PASS")
PY

echo
echo "===== 6. BUILD + CERTIFY WEB IMAGE ====="
docker build -t "$WEB_IMAGE" "$WEB_SRC"
docker run --rm --entrypoint sh "$WEB_IMAGE" -lc \
  'grep -R -q "audio-price-all-strong" /app/.next/static/css && grep -R -q "mini-action.strong\|mini-action" /app/.next/static/css'
echo "WEB_BUILD_AND_THEME_BUNDLE=PASS"

echo
echo "===== 7. START ISOLATED WEB CANDIDATE ====="
if ss -ltn "( sport = :$CANDIDATE_PORT )" | grep -q ":$CANDIDATE_PORT"; then
  fail "candidate port $CANDIDATE_PORT is already in use"
fi
docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
run_web "$CANDIDATE" "$CANDIDATE_PORT" "$WEB_IMAGE"
CANDIDATE_STARTED=1
wait_http web-candidate "http://127.0.0.1:${CANDIDATE_PORT}/auth/login" 45 || {
  docker logs --tail 150 "$CANDIDATE" 2>&1 || true
  fail "web candidate failed health certification"
}
curl -fsS "http://127.0.0.1:${CANDIDATE_PORT}/auth/login" | grep -qi desifaces || fail "web candidate brand smoke failed"
echo "WEB_CANDIDATE=PASS"

echo
echo "===== 8. PROMOTE DIRECTOR API ONLY ====="
"${COMPOSE[@]}" up -d --no-deps --force-recreate svc-director
DIRECTOR_RECREATED=1
wait_http director "http://127.0.0.1:18011/api/health" 30 || {
  docker logs --tail 180 "$DIRECTOR_C" 2>&1 || true
  fail "Director API failed after promotion"
}
python3 - <<'PY'
import json
p='/tmp/df-hotfix-http.' + str(__import__('os').getppid())
# Health was already certified by HTTP 200; runtime_ready is checked directly below.
PY
curl -fsS http://127.0.0.1:18011/api/health >/tmp/df-director-audio-hotfix-health.json
python3 - <<'PY'
import json
x=json.load(open('/tmp/df-director-audio-hotfix-health.json'))
assert x.get('ok') is True, x
assert x.get('runtime_ready') is True, x
print('DIRECTOR_HEALTH=PASS')
PY
ACTIVE_DIR="$(docker inspect "$DIRECTOR_C" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
[[ "$ACTIVE_DIR" == "$ROOT" ]] || fail "Director compose working directory is not active production root: $ACTIVE_DIR"
echo "DIRECTOR_ACTIVE_WORKSPACE=PASS"

echo
echo "===== 9. CUT OVER WEB WITH ROLLBACK SLOT ====="
docker stop "$WEB_C" >/dev/null
docker rename "$WEB_C" "$ROLLBACK_WEB"
docker update --restart=no "$ROLLBACK_WEB" >/dev/null 2>&1 || true
run_web "$WEB_C" "$WEB_PORT" "$WEB_IMAGE"
WEB_SWAPPED=1
wait_http production-web "http://127.0.0.1:${WEB_PORT}/auth/login" 45 || {
  docker logs --tail 180 "$WEB_C" 2>&1 || true
  fail "production web failed local certification"
}
wait_http public-web "https://web.desifaces.ai/auth/login" 30 || fail "public web failed after cutover"
echo "WEB_PRODUCTION_CUTOVER=PASS"

echo
echo "===== 10. NON-TARGET RUNTIME INVARIANTS ====="
[[ "$DB_ID_BEFORE" == "$(docker inspect "$DB_C" --format '{{.Id}}')" ]] || fail "DB identity changed"
[[ "$DB_STARTED_BEFORE" == "$(docker inspect "$DB_C" --format '{{.State.StartedAt}}')" ]] || fail "DB restarted"
[[ "$REDIS_ID_BEFORE" == "$(docker inspect "$REDIS_C" --format '{{.Id}}')" ]] || fail "Redis identity changed"
[[ "$REDIS_STARTED_BEFORE" == "$(docker inspect "$REDIS_C" --format '{{.State.StartedAt}}')" ]] || fail "Redis restarted"
[[ "$WORKER_ID_BEFORE" == "$(docker inspect "$DIRECTOR_WORKER_C" --format '{{.Id}}')" ]] || fail "Director worker identity changed"
[[ "$WORKER_STARTED_BEFORE" == "$(docker inspect "$DIRECTOR_WORKER_C" --format '{{.State.StartedAt}}')" ]] || fail "Director worker restarted"
[[ "$AUDIO_ID_BEFORE" == "$(docker inspect "$AUDIO_C" --format '{{.Id}}')" ]] || fail "Audio API was recreated"
[[ "$AUDIO_WORKER_ID_BEFORE" == "$(docker inspect "$AUDIO_WORKER_C" --format '{{.Id}}')" ]] || fail "Audio worker was recreated"
echo "DB_REDIS_UNCHANGED=PASS"
echo "DIRECTOR_WORKER_UNCHANGED=PASS"
echo "AUDIO_RUNTIME_UNCHANGED=PASS"

echo
echo "===== 11. FINALIZE ROLLBACK SLOT ====="
docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
CANDIDATE_STARTED=0
docker rm -f "$ROLLBACK_WEB" >/dev/null 2>&1 || true
WEB_SWAPPED=0
SUCCESS=1

cat <<EOF

============================================================
 PROD MULTI-PERSON AUDIO PRICING + UI HOTFIX PASS
============================================================
AUDIO_PAYLOAD_STATIC_CONTRACT=PASS
AUDIO_QUALITATIVE_VOLUME_SANITIZED=PASS
AUDIO_NUMERIC_CONTROLS_NORMALIZED=PASS
AUDIO_CREATIVE_DIRECTION_PRESERVED=PASS
WEB_UI_STATIC_CONTRACT=PASS
WEB_BUILD_AND_THEME_BUNDLE=PASS
WEB_CANDIDATE=PASS
DIRECTOR_HEALTH=PASS
DIRECTOR_ACTIVE_WORKSPACE=PASS
WEB_PRODUCTION_CUTOVER=PASS
DB_REDIS_UNCHANGED=PASS
DIRECTOR_WORKER_UNCHANGED=PASS
AUDIO_RUNTIME_UNCHANGED=PASS
backend_commit=$SOURCE_COMMIT
web_commit=$WEB_COMMIT
old_web_image=$WEB_IMAGE_BEFORE
new_web_image=$WEB_IMAGE
NEXT=REFRESH_EXISTING_MULTI_PERSON_PAGE_AND_CHECK_PRICE_AGAIN
EOF
