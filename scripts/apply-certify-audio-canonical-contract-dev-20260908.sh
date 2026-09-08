#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
REPO="prasshanthshankar-afk/desifaces_backend"
WORKSPACE="${WORKSPACE:-/home/azureuser/workspace/desifaces-v3}"
AUDIO_C="${AUDIO_C:-df-v3-svc-audio}"
WORKER_C="${WORKER_C:-df-v3-svc-audio-worker}"

host="$(hostname)"
[[ "$host" == "$EXPECTED_HOST" ]] || { echo "FAIL: run on $EXPECTED_HOST, current=$host"; exit 2; }
[[ -d "$WORKSPACE" ]] || { echo "FAIL: workspace missing: $WORKSPACE"; exit 3; }
command -v gh >/dev/null || { echo "FAIL: gh missing"; exit 4; }
gh auth status >/dev/null 2>&1 || { echo "FAIL: gh not authenticated"; exit 5; }
command -v docker >/dev/null || { echo "FAIL: docker missing"; exit 6; }

echo "============================================================"
echo " desifaces DEV — AUDIO CANONICAL CONTRACT CERTIFICATION"
echo "============================================================"
echo "host=$host"
echo "workspace=$WORKSPACE"
echo "db_change=NONE"
echo "redis_change=NONE"

cd "$WORKSPACE"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/home/azureuser/backups/audio-canonical-contract-dev-$TS"
mkdir -p "$BACKUP"

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

echo "compose_project=$PROJECT"
echo "audio_service=$AUDIO_SERVICE"
echo "worker_service=$WORKER_SERVICE"

DB_STARTED="$(docker inspect desifaces-v3-db --format '{{.State.StartedAt}}')"

cd "$PROJECT_DIR"
docker compose -p "$PROJECT" "${COMPOSE_ARGS[@]}" build "$AUDIO_SERVICE" "$WORKER_SERVICE"
docker compose -p "$PROJECT" "${COMPOSE_ARGS[@]}" up -d --no-deps --force-recreate "$AUDIO_SERVICE" "$WORKER_SERVICE"

for i in $(seq 1 30); do
  state="$(docker inspect "$AUDIO_C" --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}' 2>/dev/null || true)"
  echo "wait=$i state=$state"
  [[ "$state" == "running/healthy" || "$state" == "running/no-health" ]] && break
  sleep 4
done

ROUTES="$(docker exec -i "$AUDIO_C" python - <<'PY'
from app.main import app
for r in sorted(app.routes, key=lambda x: getattr(x, 'path', '')):
    p=getattr(r,'path','')
    if 'audio/jobs' in p or 'audio/assets' in p:
        print(','.join(sorted(getattr(r,'methods',[]) or [])), p)
PY
)"
printf '%s\n' "$ROUTES"
grep -Fq '/api/audio/jobs/{job_id}/canonical-output' <<<"$ROUTES" || { echo "FAIL: canonical-output route missing"; exit 20; }
grep -Fq '/api/audio/assets/{media_id}/read-url' <<<"$ROUTES" || { echo "FAIL: read-url route missing"; exit 21; }

# Route must exist and be auth-protected: without token, 401 is expected (not 404).
HTTP_CODE="$(docker exec "$AUDIO_C" python - <<'PY'
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
[[ "$HTTP_CODE" == "401" ]] || { echo "FAIL: expected auth-protected route (401), got $HTTP_CODE"; exit 22; }

# Source-level regression tests are intentionally dependency-light.
docker exec "$AUDIO_C" python - <<'PY'
from app.api.routes.canonical_audio import _storage_ref_from_artifact
assert _storage_ref_from_artifact({'storage_path':'acct/job/variant_1.mp3'}) == 'acct/job/variant_1.mp3'
assert _storage_ref_from_artifact({}) == ''
print('CANONICAL_HELPERS=PASS')
PY

[[ "$DB_STARTED" == "$(docker inspect desifaces-v3-db --format '{{.State.StartedAt}}')" ]] || { echo "FAIL: DB restarted"; exit 23; }

echo "============================================================"
echo " AUDIO CANONICAL DEV CERTIFICATION PASS"
echo "============================================================"
echo "SOURCE_SYNC=PASS"
echo "CANONICAL_ROUTE=PASS"
echo "READ_URL_ROUTE=PASS"
echo "AUTH_GUARD=PASS"
echo "DB_UNCHANGED=PASS"
echo "backup=$BACKUP"
echo "NEXT=RUN_EXISTING_MULTI_PERSON_STORY_THROUGH_AUDIO_TO_FUSION"
