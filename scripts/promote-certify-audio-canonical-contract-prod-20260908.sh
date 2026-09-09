#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-gpu"
REPO="prasshanthshankar-afk/desifaces_backend"
WORKSPACE="${WORKSPACE:-/home/azureuser/workspace/desifaces}"
AUDIO_C="${AUDIO_C:-df-svc-audio}"
WORKER_C="${WORKER_C:-df-svc-audio-worker}"
DB_C="${DB_C:-desifaces-db}"
REDIS_C="${REDIS_C:-desifaces-redis}"

host="$(hostname)"
[[ "$host" == "$EXPECTED_HOST" ]] || { echo "FAIL: run on $EXPECTED_HOST, current=$host"; exit 2; }
[[ -d "$WORKSPACE" ]] || { echo "FAIL: workspace missing: $WORKSPACE"; exit 3; }
command -v curl >/dev/null || { echo "FAIL: curl missing"; exit 4; }
command -v docker >/dev/null || { echo "FAIL: docker missing"; exit 6; }

echo "============================================================"
echo " desifaces PROD — AUDIO CANONICAL CONTRACT PROMOTION"
echo "============================================================"
echo "host=$host"
echo "workspace=$WORKSPACE"
echo "scope=svc-audio+worker only"
echo "db_change=NONE"
echo "redis_change=NONE"
echo "source_fetch=curl/raw.githubusercontent.com"

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
  [[ -s "$tmp" ]] || { echo "FAIL: fetched source is empty: $path"; rm -f "$tmp"; exit 7; }
  mv "$tmp" "$dest"
}

fetch_file services/svc-audio/app/app/api/routes/canonical_audio.py
fetch_file services/svc-audio/app/app/api/__init__.py

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
for candidate in "$PROJECT_DIR/infra/.env" "$WORKSPACE/infra/.env" "$PROJECT_DIR/.env" "$WORKSPACE/.env"; do
  if [[ -f "$candidate" ]]; then ENV_FILE="$candidate"; break; fi
done

if [[ -n "$ENV_FILE" ]]; then
  COMPOSE_ENV_ARGS=(--env-file "$ENV_FILE")
  echo "compose_env_file=$ENV_FILE"
else
  COMPOSE_ENV_ARGS=()
  while IFS= read -r c; do
    while IFS= read -r kv; do
      key="${kv%%=*}"; val="${kv#*=}"
      [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
      if [[ -z "${!key+x}" ]]; then export "$key=$val"; fi
    done < <(docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}')
  done < <(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}')
fi

cd "$PROJECT_DIR"
docker compose -p "$PROJECT" "${COMPOSE_ENV_ARGS[@]}" "${COMPOSE_ARGS[@]}" config "$AUDIO_SERVICE" "$WORKER_SERVICE" >/dev/null
echo "COMPOSE_INTERPOLATION=PASS"

docker compose -p "$PROJECT" "${COMPOSE_ENV_ARGS[@]}" "${COMPOSE_ARGS[@]}" build "$AUDIO_SERVICE" "$WORKER_SERVICE"
docker compose -p "$PROJECT" "${COMPOSE_ENV_ARGS[@]}" "${COMPOSE_ARGS[@]}" up -d --no-deps --force-recreate "$AUDIO_SERVICE" "$WORKER_SERVICE"

for i in $(seq 1 30); do
  state="$(docker inspect "$AUDIO_C" --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}' 2>/dev/null || true)"
  echo "wait=$i state=$state"
  [[ "$state" == "running/healthy" || "$state" == "running/no-health" ]] && break
  sleep 4
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
[[ "$AUTH_CODE" == "401" ]] || { echo "FAIL: auth guard expected 401, got $AUTH_CODE"; exit 22; }
echo "AUTH_GUARD=PASS"

[[ "$DB_ID_BEFORE" == "$(docker inspect "$DB_C" --format '{{.Id}}')" ]] || { echo "FAIL: DB identity changed"; exit 23; }
[[ "$DB_STARTED_BEFORE" == "$(docker inspect "$DB_C" --format '{{.State.StartedAt}}')" ]] || { echo "FAIL: DB restarted"; exit 24; }
[[ "$REDIS_ID_BEFORE" == "$(docker inspect "$REDIS_C" --format '{{.Id}}')" ]] || { echo "FAIL: Redis identity changed"; exit 25; }
echo "DB_REDIS_UNCHANGED=PASS"

PUBLIC_AUDIO_HEALTH="$(curl -sS -o /dev/null -w '%{http_code}' https://api.desifaces.ai/api/audio/health || true)"
echo "public_audio_health=$PUBLIC_AUDIO_HEALTH"

PUBLIC_WEB="$(curl -sS -o /dev/null -w '%{http_code}' https://web.desifaces.ai/auth/login || true)"
echo "public_web_login=$PUBLIC_WEB"
[[ "$PUBLIC_WEB" == "200" ]] || { echo "FAIL: public web unavailable"; exit 26; }

echo "============================================================"
echo " PROD AUDIO CANONICAL PROMOTION PASS"
echo "============================================================"
echo "SOURCE_SYNC=PASS"
echo "COMPOSE_INTERPOLATION=PASS"
echo "ROUTE_CONTRACT=PASS"
echo "AUTH_GUARD=PASS"
echo "DB_REDIS_UNCHANGED=PASS"
echo "AUDIO_IMAGE_BEFORE=$AUDIO_IMAGE_BEFORE"
echo "AUDIO_IMAGE_AFTER=$(docker inspect "$AUDIO_C" --format '{{.Image}}')"
echo "WORKER_IMAGE_BEFORE=$WORKER_IMAGE_BEFORE"
echo "WORKER_IMAGE_AFTER=$(docker inspect "$WORKER_C" --format '{{.Image}}')"
echo "backup=$BACKUP"
echo "NEXT=RETRY_EXISTING_PRODUCTION_MULTI_PERSON_STORY"
