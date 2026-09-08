#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-gpu"
REPO="prasshanthshankar-afk/desifaces_backend"
WORKSPACE="/home/azureuser/workspace/desifaces"
AUDIO_C="df-svc-audio"
WORKER_C="df-svc-audio-worker"
DB_C="desifaces-db"
REDIS_C="desifaces-redis"

host="$(hostname)"
[[ "$host" == "$EXPECTED_HOST" ]] || { echo "FAIL: run on $EXPECTED_HOST, current=$host"; exit 2; }
[[ -d "$WORKSPACE" ]] || { echo "FAIL: production workspace missing: $WORKSPACE"; exit 3; }
command -v gh >/dev/null || { echo "FAIL: gh missing"; exit 4; }
gh auth status >/dev/null 2>&1 || { echo "FAIL: gh not authenticated"; exit 5; }
command -v docker >/dev/null || { echo "FAIL: docker missing"; exit 6; }

TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/home/azureuser/backups/audio-canonical-prod-$TS"
mkdir -p "$BACKUP"

DB_STARTED="$(docker inspect "$DB_C" --format '{{.State.StartedAt}}')"
DB_ID="$(docker inspect "$DB_C" --format '{{.Id}}')"
REDIS_ID="$(docker inspect "$REDIS_C" --format '{{.Id}}')"
OLD_AUDIO_IMAGE="$(docker inspect "$AUDIO_C" --format '{{.Image}}')"
OLD_WORKER_IMAGE="$(docker inspect "$WORKER_C" --format '{{.Image}}')"

echo "============================================================"
echo " desifaces PROD — AUDIO CANONICAL CONTRACT PROMOTION"
echo "============================================================"
echo "host=$host"
echo "workspace=$WORKSPACE"
echo "db_change=NONE"
echo "redis_change=NONE"
echo "director_change=NONE"
echo "web_change=NONE"
echo "old_audio_image=$OLD_AUDIO_IMAGE"
echo "old_worker_image=$OLD_WORKER_IMAGE"

fetch_file() {
  local path="$1"
  local dest="$WORKSPACE/$path"
  mkdir -p "$(dirname "$dest")"
  if [[ -f "$dest" ]]; then cp -a "$dest" "$BACKUP/$(echo "$path" | tr '/' '_')"; fi
  gh api "repos/$REPO/contents/$path?ref=main" --jq .content | base64 -d > "$dest"
}

fetch_file services/svc-audio/app/app/api/routes/canonical_audio.py
fetch_file services/svc-audio/app/app/api/__init__.py
fetch_file services/svc-audio/tests/test_canonical_audio_contract.py

echo "SOURCE_SYNC=PASS"

PROJECT="$(docker inspect "$AUDIO_C" --format '{{index .Config.Labels "com.docker.compose.project"}}')"
PROJECT_DIR="$(docker inspect "$AUDIO_C" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
CONFIG_FILES="$(docker inspect "$AUDIO_C" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')"
AUDIO_SERVICE="$(docker inspect "$AUDIO_C" --format '{{index .Config.Labels "com.docker.compose.service"}}')"
WORKER_SERVICE="$(docker inspect "$WORKER_C" --format '{{index .Config.Labels "com.docker.compose.service"}}')"

[[ -n "$PROJECT" && -n "$PROJECT_DIR" && -n "$CONFIG_FILES" && -n "$AUDIO_SERVICE" && -n "$WORKER_SERVICE" ]] || {
  echo "FAIL: compose labels incomplete"; exit 10;
}

IFS=',' read -r -a cfgs <<< "$CONFIG_FILES"
COMPOSE_ARGS=()
for f in "${cfgs[@]}"; do
  [[ "$f" = /* ]] || f="$PROJECT_DIR/$f"
  COMPOSE_ARGS+=( -f "$f" )
done

ENV_FILE=""
for candidate in "$WORKSPACE/infra/.env" "$PROJECT_DIR/infra/.env" "$PROJECT_DIR/.env"; do
  if [[ -f "$candidate" ]]; then ENV_FILE="$candidate"; break; fi
done
[[ -n "$ENV_FILE" ]] || { echo "FAIL: production env file not found"; exit 11; }

echo "compose_project=$PROJECT"
echo "compose_env_file=$ENV_FILE"
echo "audio_service=$AUDIO_SERVICE"
echo "worker_service=$WORKER_SERVICE"

cd "$PROJECT_DIR"
docker compose -p "$PROJECT" --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" config "$AUDIO_SERVICE" "$WORKER_SERVICE" >/dev/null
echo "COMPOSE_INTERPOLATION=PASS"

docker compose -p "$PROJECT" --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" build "$AUDIO_SERVICE" "$WORKER_SERVICE"
docker compose -p "$PROJECT" --env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}" up -d --no-deps --force-recreate "$AUDIO_SERVICE" "$WORKER_SERVICE"

for i in $(seq 1 30); do
  state="$(docker inspect "$AUDIO_C" --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}' 2>/dev/null || true)"
  echo "wait=$i state=$state"
  [[ "$state" == "running/healthy" || "$state" == "running/no-health" ]] && break
  sleep 3
done

ROUTES="$(docker exec -i "$AUDIO_C" python - <<'PY'
from app.main import app
for r in sorted(app.routes, key=lambda x: getattr(x,'path','')):
    p=getattr(r,'path','')
    if 'audio/jobs' in p or 'audio/assets' in p:
        print(','.join(sorted(getattr(r,'methods',[]) or [])), p)
PY
)"
printf '%s\n' "$ROUTES"
grep -Fq '/api/audio/jobs/{job_id}/canonical-output' <<<"$ROUTES" || { echo "FAIL: canonical-output route missing"; exit 20; }
grep -Fq '/api/audio/assets/{media_id}/read-url' <<<"$ROUTES" || { echo "FAIL: read-url route missing"; exit 21; }

HTTP_CODE="$(docker exec -i "$AUDIO_C" python - <<'PY'
import urllib.request, urllib.error
url='http://127.0.0.1:8004/api/audio/jobs/00000000-0000-0000-0000-000000000000/canonical-output?project_id=00000000-0000-0000-0000-000000000000'
try:
    urllib.request.urlopen(url, timeout=5)
    print('200')
except urllib.error.HTTPError as e:
    print(e.code)
PY
)"
echo "canonical_unauth_http=$HTTP_CODE"
[[ "$HTTP_CODE" == "401" ]] || { echo "FAIL: expected 401, got $HTTP_CODE"; exit 22; }

HEALTH_CODE="$(docker exec "$AUDIO_C" python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8004/api/health',timeout=5).status)" 2>/dev/null || true)"
echo "audio_health_http=$HEALTH_CODE"
[[ "$HEALTH_CODE" == "200" ]] || { echo "FAIL: audio health not 200"; exit 23; }

[[ "$DB_STARTED" == "$(docker inspect "$DB_C" --format '{{.State.StartedAt}}')" ]] || { echo "FAIL: DB restarted"; exit 24; }
[[ "$DB_ID" == "$(docker inspect "$DB_C" --format '{{.Id}}')" ]] || { echo "FAIL: DB identity changed"; exit 25; }
[[ "$REDIS_ID" == "$(docker inspect "$REDIS_C" --format '{{.Id}}')" ]] || { echo "FAIL: Redis identity changed"; exit 26; }

NEW_AUDIO_IMAGE="$(docker inspect "$AUDIO_C" --format '{{.Image}}')"
NEW_WORKER_IMAGE="$(docker inspect "$WORKER_C" --format '{{.Image}}')"

echo "============================================================"
echo " AUDIO CANONICAL PROD PROMOTION PASS"
echo "============================================================"
echo "SOURCE_SYNC=PASS"
echo "COMPOSE_INTERPOLATION=PASS"
echo "CANONICAL_ROUTE=PASS"
echo "READ_URL_ROUTE=PASS"
echo "AUTH_GUARD=PASS"
echo "AUDIO_HEALTH=PASS"
echo "DB_UNCHANGED=PASS"
echo "REDIS_UNCHANGED=PASS"
echo "new_audio_image=$NEW_AUDIO_IMAGE"
echo "new_worker_image=$NEW_WORKER_IMAGE"
echo "backup=$BACKUP"
echo "NEXT=RETRY_EXISTING_PRODUCTION_MULTI_PERSON_STORY"
