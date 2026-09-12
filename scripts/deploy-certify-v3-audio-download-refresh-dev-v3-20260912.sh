#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
REMOTE="prasshanthshankar-afk/desifaces_backend"
BRANCH="fix/v3-audio-read-url-refresh-20260912"
AUDIO_C="df-v3-svc-audio"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STATE="$HOME/.local/state/audio-download-refresh-v3/$STAMP"
SRC="$STATE/source"
BACKUP="$STATE/runtime-backup"
LOG="$STATE/run.log"
mkdir -p "$SRC" "$BACKUP"
chmod 700 "$STATE"
exec > >(tee "$LOG") 2>&1

fail(){ echo "FAIL: $*"; echo "AUDIO_DOWNLOAD_REFRESH_V3=FAIL_CLOSED"; echo "log=$LOG"; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "wrong host"
command -v gh >/dev/null || fail "gh missing"
command -v docker >/dev/null || fail "docker missing"
docker inspect "$AUDIO_C" >/dev/null 2>&1 || fail "missing $AUDIO_C"
[[ "$(docker inspect -f '{{.State.Running}}' "$AUDIO_C")" == "true" ]] || fail "$AUDIO_C not running"

echo "============================================================"
echo " desifaces DEV — DURABLE AUDIO DOWNLOAD REFRESH V3"
echo "============================================================"
echo "environment=DEV_ONLY"
echo "database_change=NONE"
echo "pricing_change=NONE"
echo "production_touch=NONE"
echo "bootstrap=CANONICAL_AUDIO_ROUTE_IF_RUNTIME_PREDATES_CONTRACT"

fetch(){
  local rel="$1" out="$2"
  gh api "repos/$REMOTE/contents/$rel?ref=$BRANCH" --jq .content | base64 -d > "$out"
}
fetch services/svc-audio/app/app/api/routes/canonical_audio.py "$SRC/canonical_audio.py"
fetch services/svc-audio/app/app/services/azure_storage_service.py "$SRC/azure_storage_service.py"
fetch services/svc-audio/app/app/api/__init__.py "$SRC/api_init_reference.py"
fetch scripts/deploy-certify-v3-audio-download-refresh-dev-v2-20260912.sh "$SRC/v2.sh"
chmod +x "$SRC/v2.sh"
bash -n "$SRC/v2.sh" || fail "V2 certification script syntax invalid"

grep -q 'canonical_audio_router' "$SRC/api_init_reference.py" || fail "canonical router registration missing from source"
grep -q 'AzureStorageService().generate_read_url(storage_ref)' "$SRC/canonical_audio.py" || fail "fresh read-url source contract missing"
grep -q 'def generate_read_url' "$SRC/azure_storage_service.py" || fail "storage signer source contract missing"
echo "AUDIO_FRESH_SAS_SOURCE_CONTRACT=PASS"

# Resolve the installed Python package root from the package itself. Do not
# assume /app/app and do not import the not-yet-installed canonical module.
APP_ROOT="$(docker exec "$AUDIO_C" python -c 'import app,pathlib; print(pathlib.Path(next(iter(app.__path__))).resolve())')"
[[ "$APP_ROOT" == /* ]] || fail "unable to resolve installed app package root"
echo "audio_app_root=$APP_ROOT"

CANONICAL_PATH="$APP_ROOT/api/routes/canonical_audio.py"
STORAGE_PATH="$APP_ROOT/services/azure_storage_service.py"
API_INIT_PATH="$APP_ROOT/api/__init__.py"

EXISTED="$STATE/existed.txt"
: > "$EXISTED"
backup_runtime_file(){
  local key="$1" path="$2"
  if docker exec "$AUDIO_C" test -f "$path"; then
    echo "$key" >> "$EXISTED"
    docker cp "$AUDIO_C:$path" "$BACKUP/$key"
  fi
}
backup_runtime_file canonical_audio.py "$CANONICAL_PATH"
backup_runtime_file azure_storage_service.py "$STORAGE_PATH"
backup_runtime_file api_init.py "$API_INIT_PATH"

grep -Fxq api_init.py "$EXISTED" || fail "existing Audio API router initializer not found"
grep -Fxq azure_storage_service.py "$EXISTED" || fail "existing Audio storage service not found"

# Patch the *installed* API initializer minimally. Do not replace it wholesale
# with a newer source-tree initializer because this runtime may predate other
# routes as well. Only add canonical Audio import/include when absent.
cp "$BACKUP/api_init.py" "$SRC/api_init_runtime.py"
python3 - "$SRC/api_init_runtime.py" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); s=p.read_text()
imp='    from app.api.routes.canonical_audio import router as canonical_audio_router\n'
inc='    router.include_router(canonical_audio_router)\n'
if 'canonical_audio_router' not in s:
    import_anchor='    from app.api.routes.tts_jobs import router as tts_jobs_router\n'
    include_anchor='    router.include_router(tts_jobs_router)\n'
    if import_anchor not in s or include_anchor not in s:
        raise SystemExit('FAIL: installed Audio API initializer shape is not recognized')
    s=s.replace(import_anchor,import_anchor+imp,1)
    s=s.replace(include_anchor,include_anchor+inc,1)
if imp.strip() not in s or inc.strip() not in s:
    raise SystemExit('FAIL: canonical Audio router could not be registered minimally')
p.write_text(s)
PY

grep -q 'canonical_audio_router' "$SRC/api_init_runtime.py" || fail "runtime router patch missing"
echo "AUDIO_API_INIT_MINIMAL_PATCH=PASS"

SUCCESS=0
restore_one(){
  local key="$1" path="$2"
  if grep -Fxq "$key" "$EXISTED"; then
    docker cp "$BACKUP/$key" "$AUDIO_C:$path" >/dev/null 2>&1 || true
  else
    docker exec "$AUDIO_C" rm -f "$path" >/dev/null 2>&1 || true
  fi
}
rollback(){
  echo "AUDIO_BOOTSTRAP_ROLLBACK=START"
  restore_one canonical_audio.py "$CANONICAL_PATH"
  restore_one azure_storage_service.py "$STORAGE_PATH"
  restore_one api_init.py "$API_INIT_PATH"
  docker restart "$AUDIO_C" >/dev/null 2>&1 || true
  echo "AUDIO_BOOTSTRAP_ROLLBACK=APPLIED"
}
cleanup(){
  if [[ "$SUCCESS" != "1" ]]; then rollback; fi
}
trap cleanup EXIT

# Install exactly two owner-service implementation files plus the minimal route
# registration patch into the DEV container writable layer.
docker exec "$AUDIO_C" mkdir -p "$APP_ROOT/api/routes" "$APP_ROOT/services"
docker cp "$SRC/canonical_audio.py" "$AUDIO_C:$CANONICAL_PATH"
docker cp "$SRC/azure_storage_service.py" "$AUDIO_C:$STORAGE_PATH"
docker cp "$SRC/api_init_runtime.py" "$AUDIO_C:$API_INIT_PATH"
docker restart "$AUDIO_C" >/dev/null

READY=false
for _ in $(seq 1 30); do
  state="$(docker inspect "$AUDIO_C" --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}' 2>/dev/null || true)"
  if [[ "$state" == "running/healthy" || "$state" == "running/no-health" ]]; then READY=true; break; fi
  sleep 2
done
[[ "$READY" == "true" ]] || fail "Audio API did not recover after canonical-route bootstrap"

# Prove both importability and actual FastAPI route registration before running
# the full V2 certification. This closes the exact failure seen in the prior run.
docker exec -i "$AUDIO_C" python - <<'PY'
from app.main import app
from app.api.routes.canonical_audio import get_audio_asset_read_url
from app.services.azure_storage_service import AzureStorageService
paths={getattr(r,'path','') for r in app.routes}
assert '/api/audio/assets/{media_id}/read-url' in paths, sorted(p for p in paths if 'audio' in p)
assert '/api/audio/jobs/{job_id}/canonical-output' in paths
assert callable(get_audio_asset_read_url)
assert callable(AzureStorageService.generate_read_url)
print('AUDIO_CANONICAL_RUNTIME_BOOTSTRAP=PASS')
PY

echo "AUDIO_CANONICAL_ROUTE_REGISTRATION=PASS"

# V2 now sees the canonical module and can execute its bounded end-to-end
# certification: fresh SAS -> Azure read -> Web proxy -> Web rehydration/build.
bash "$SRC/v2.sh"

SUCCESS=1
trap - EXIT

echo "============================================================"
echo " AUDIO DOWNLOAD REFRESH V3=PASS"
echo "============================================================"
echo "AUDIO_CANONICAL_RUNTIME_BOOTSTRAP=PASS"
echo "AUDIO_FRESH_SAS_CERTIFICATION=PASS"
echo "DATABASE_CHANGE=NONE"
echo "PRICING_CHANGE=NONE"
echo "PRODUCTION_TOUCH=NONE"
echo "log=$LOG"
