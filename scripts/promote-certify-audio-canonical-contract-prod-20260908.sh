#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-gpu"
REPO="prasshanthshankar-afk/desifaces_backend"
WORKSPACE="${WORKSPACE:-/home/azureuser/workspace/desifaces}"
COMPOSE_FILE="${COMPOSE_FILE:-$WORKSPACE/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-$WORKSPACE/infra/.env}"
PROJECT="${PROJECT:-desifaces}"
AUDIO_C="${AUDIO_C:-df-svc-audio}"
WORKER_C="${WORKER_C:-df-svc-audio-worker}"
DB_C="${DB_C:-desifaces-db}"
REDIS_C="${REDIS_C:-desifaces-redis}"

host="$(hostname)"
[[ "$host" == "$EXPECTED_HOST" ]] || { echo "FAIL: run on $EXPECTED_HOST, current=$host"; exit 2; }
[[ -d "$WORKSPACE" ]] || { echo "FAIL: workspace missing: $WORKSPACE"; exit 3; }
[[ -f "$COMPOSE_FILE" ]] || { echo "FAIL: active compose missing: $COMPOSE_FILE"; exit 4; }
[[ -f "$ENV_FILE" ]] || { echo "FAIL: active env missing: $ENV_FILE"; exit 5; }
command -v curl >/dev/null || { echo "FAIL: curl missing"; exit 6; }
command -v docker >/dev/null || { echo "FAIL: docker missing"; exit 7; }

echo "============================================================"
echo " desifaces PROD — AUDIO CANONICAL CONTRACT PROMOTION"
echo "============================================================"
echo "host=$host"
echo "workspace=$WORKSPACE"
echo "compose=$COMPOSE_FILE"
echo "env=$ENV_FILE"
echo "scope=svc-audio+worker only"
echo "db_change=NONE"
echo "redis_change=NONE"
echo "compose_source=ACTIVE_WORKSPACE_ONLY"

echo
DB_ID_BEFORE="$(docker inspect "$DB_C" --format '{{.Id}}')"
DB_STARTED_BEFORE="$(docker inspect "$DB_C" --format '{{.State.StartedAt}}')"
REDIS_ID_BEFORE="$(docker inspect "$REDIS_C" --format '{{.Id}}')"
AUDIO_IMAGE_BEFORE="$(docker inspect "$AUDIO_C" --format '{{.Image}}')"
WORKER_IMAGE_BEFORE="$(docker inspect "$WORKER_C" --format '{{.Image}}')"

echo "PRECHECK_RUNTIME=PASS"

cd "$WORKSPACE"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/home/azureuser/backups/audio-canonical-contract-prod-$TS"
mkdir -p "$BACKUP"

fetch_file() {
  local path="$1"
  local dest="$WORKSPACE/$path"
  local tmp="${dest}.promote-${TS}.tmp"
  mkdir -p "$(dirname "$dest")"
  if [[ -f "$dest" ]]; then cp -a "$dest" "$BACKUP/$(echo "$path" | tr '/' '_')"; fi
  curl -fsSL "https://raw.githubusercontent.com/$REPO/main/$path" -o "$tmp"
  [[ -s "$tmp" ]] || { echo "FAIL: fetched source is empty: $path"; rm -f "$tmp"; exit 8; }
  mv "$tmp" "$dest"
}

fetch_file services/svc-audio/app/app/api/routes/canonical_audio.py
fetch_file services/svc-audio/app/app/api/__init__.py

grep -Fq 'canonical_audio_router' services/svc-audio/app/app/api/__init__.py || { echo "FAIL: canonical router import missing"; exit 9; }
grep -Fq '/jobs/{job_id}/canonical-output' services/svc-audio/app/app/api/routes/canonical_audio.py || { echo "FAIL: canonical route source missing"; exit 10; }
echo "SOURCE_SYNC=PASS"

COMPOSE_ARGS=(--project-directory "$WORKSPACE" -p "$PROJECT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE")

docker compose "${COMPOSE_ARGS[@]}" config svc-audio svc-audio-worker >/dev/null
echo "COMPOSE_INTERPOLATION=PASS"

docker compose "${COMPOSE_ARGS[@]}" build svc-audio svc-audio-worker
docker compose "${COMPOSE_ARGS[@]}" up -d --no-deps --force-recreate svc-audio svc-audio-worker

for i in $(seq 1 30); do
  state="$(docker inspect "$AUDIO_C" --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}' 2>/dev/null || true)"
  echo "wait=$i state=$state"
  [[ "$state" == "running/healthy" || "$state" == "running/no-health" ]] && break
  sleep 4
done

ACTIVE_DIR="$(docker inspect "$AUDIO_C" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
echo "compose_working_dir=$ACTIVE_DIR"
[[ "$ACTIVE_DIR" == "$WORKSPACE" ]] || { echo "FAIL: Audio points to wrong compose workspace: $ACTIVE_DIR"; exit 20; }
echo "ACTIVE_WORKSPACE=PASS"

ROUTES="$(docker exec -i "$AUDIO_C" python - <<'PY'
from app.main import app
for r in sorted(app.routes, key=lambda x: getattr(x, 'path', '')):
    p = getattr(r, 'path', '')
    if 'audio/jobs' in p or 'audio/assets' in p:
        print(','.join(sorted(getattr(r, 'methods', []) or [])), p)
PY
)"
printf '%s\n' "$ROUTES"
grep -Fq '/api/audio/jobs/{job_id}/canonical-output' <<<"$ROUTES" || { echo "FAIL: canonical-output route missing"; exit 21; }
grep -Fq '/api/audio/assets/{media_id}/read-url' <<<"$ROUTES" || { echo "FAIL: read-url route missing"; exit 22; }
echo "ROUTE_CONTRACT=PASS"

AUTH_CODE="$(docker exec -i "$AUDIO_C" python - <<'PY'
import urllib.request, urllib.error
url='http://127.0.0.1:8004/api/audio/jobs/00000000-0000-0000-0000-000000000000/canonical-output?project_id=00000000-0000-0000-0000-000000000000'
try:
    urllib.request.urlopen(url, timeout=5)
    print('200')
except urllib.error.HTTPError as e:
    print(e.code)
PY
)"
echo "canonical_unauth_http=$AUTH_CODE"
[[ "$AUTH_CODE" == "401" ]] || { echo "FAIL: auth guard expected 401, got $AUTH_CODE"; exit 23; }
echo "AUTH_GUARD=PASS"

[[ "$DB_ID_BEFORE" == "$(docker inspect "$DB_C" --format '{{.Id}}')" ]] || { echo "FAIL: DB identity changed"; exit 24; }
[[ "$DB_STARTED_BEFORE" == "$(docker inspect "$DB_C" --format '{{.State.StartedAt}}')" ]] || { echo "FAIL: DB restarted"; exit 25; }
[[ "$REDIS_ID_BEFORE" == "$(docker inspect "$REDIS_C" --format '{{.Id}}')" ]] || { echo "FAIL: Redis identity changed"; exit 26; }
echo "DB_REDIS_UNCHANGED=PASS"

PUBLIC_AUDIO_HEALTH="$(curl -sS -o /dev/null -w '%{http_code}' https://api.desifaces.ai/api/audio/health || true)"
echo "public_audio_health=$PUBLIC_AUDIO_HEALTH"
PUBLIC_WEB="$(curl -sS -o /dev/null -w '%{http_code}' https://web.desifaces.ai/auth/login || true)"
echo "public_web_login=$PUBLIC_WEB"
[[ "$PUBLIC_WEB" == "200" ]] || { echo "FAIL: public web unavailable"; exit 27; }

echo "============================================================"
echo " PROD AUDIO CANONICAL PROMOTION PASS"
echo "============================================================"
echo "SOURCE_SYNC=PASS"
echo "COMPOSE_INTERPOLATION=PASS"
echo "ACTIVE_WORKSPACE=PASS"
echo "ROUTE_CONTRACT=PASS"
echo "AUTH_GUARD=PASS"
echo "DB_REDIS_UNCHANGED=PASS"
echo "AUDIO_IMAGE_BEFORE=$AUDIO_IMAGE_BEFORE"
echo "AUDIO_IMAGE_AFTER=$(docker inspect "$AUDIO_C" --format '{{.Image}}')"
echo "WORKER_IMAGE_BEFORE=$WORKER_IMAGE_BEFORE"
echo "WORKER_IMAGE_AFTER=$(docker inspect "$WORKER_C" --format '{{.Image}}')"
echo "backup=$BACKUP"
echo "NEXT=TEST_PRODUCTION_MULTI_PERSON_STORY"
