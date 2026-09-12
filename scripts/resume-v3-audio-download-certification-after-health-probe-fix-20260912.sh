#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
REMOTE="prasshanthshankar-afk/desifaces_backend"
BRANCH="fix/v3-audio-read-url-refresh-20260912"
AUDIO_C="df-v3-svc-audio"
AUDIO_W="df-v3-svc-audio-worker"
ASSISTANT_C="df-v3-svc-assistant"
DIRECTOR_C="df-v3-svc-director"
DB_C="desifaces-v3-db"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STATE="$HOME/.local/state/audio-download-resume-after-health-probe/$STAMP"
LOG="$STATE/run.log"
SRC="$STATE/source"
mkdir -p "$SRC"
chmod 700 "$STATE"
exec > >(tee "$LOG") 2>&1

fail(){
  echo "FAIL: $*"
  echo "AUDIO_DOWNLOAD_RESUME_AFTER_HEALTH_PROBE=FAIL_CLOSED"
  echo "log=$LOG"
  exit 1
}

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "wrong host"
command -v gh >/dev/null || fail "gh missing"
command -v docker >/dev/null || fail "docker missing"
for c in "$AUDIO_C" "$AUDIO_W" "$ASSISTANT_C" "$DIRECTOR_C" "$DB_C"; do
  docker inspect "$c" >/dev/null 2>&1 || fail "missing runtime $c"
done

echo "============================================================"
echo " desifaces DEV — RESUME AUDIO DOWNLOAD CERTIFICATION"
echo "============================================================"
echo "environment=DEV_ONLY"
echo "database_change=NONE"
echo "pricing_change=NONE"
echo "production_touch=NONE"
echo "resume_reason=PRIOR_HEALTH_PROBE_OMITTED_DOCKER_EXEC_STDIN"

AUDIO_W_IMAGE_BEFORE="$(docker inspect -f '{{.Image}}' "$AUDIO_W")"
ASSISTANT_IMAGE_BEFORE="$(docker inspect -f '{{.Image}}' "$ASSISTANT_C")"
DIRECTOR_IMAGE_BEFORE="$(docker inspect -f '{{.Image}}' "$DIRECTOR_C")"
DB_STARTED_BEFORE="$(docker inspect -f '{{.State.StartedAt}}' "$DB_C")"

# The previous run already repaired the top-level gender_translation packaging.
# Verify that exact runtime condition and do not repeat the repair.
READY=0
for i in $(seq 1 20); do
  state="$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}' "$AUDIO_C" 2>/dev/null || true)"
  if [[ "$state" == "running/healthy" || "$state" == "running/no-health" ]]; then
    READY=1
    break
  fi
  sleep 2
done
[[ "$READY" == "1" ]] || { docker logs --tail 160 "$AUDIO_C" 2>&1 || true; fail "Audio runtime is not healthy before resume"; }

docker exec -i "$AUDIO_C" python - <<'PY'
import gender_translation
from gender_translation import GenderTranslationError, normalize_gender, translate_with_gender
from app.services.tts_service import TTSService
assert callable(normalize_gender)
assert callable(translate_with_gender)
assert GenderTranslationError is not None
assert TTSService is not None
print('AUDIO_GENDER_TRANSLATION_RUNTIME_IMPORT=PASS')
PY

# IMPORTANT: docker exec requires -i for the here-doc. The prior certification
# omitted -i, so Python received EOF and HEALTH_CODE became empty even though
# the container log showed GET /api/health 200.
HEALTH_CODE="$(docker exec -i "$AUDIO_C" python - <<'PY'
import urllib.request, urllib.error
try:
    with urllib.request.urlopen('http://127.0.0.1:8004/api/health', timeout=5) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as exc:
    print('ERR:' + type(exc).__name__)
PY
)"
[[ "$HEALTH_CODE" == "200" ]] || { docker logs --tail 160 "$AUDIO_C" 2>&1 || true; fail "Audio health endpoint=$HEALTH_CODE"; }
echo "AUDIO_BASELINE_HEALTH=PASS"
echo "HEALTH_PROBE_CORRECTION=PASS"

[[ "$(docker inspect -f '{{.Image}}' "$AUDIO_W")" == "$AUDIO_W_IMAGE_BEFORE" ]] || fail "Audio worker changed before resume"
[[ "$(docker inspect -f '{{.Image}}' "$ASSISTANT_C")" == "$ASSISTANT_IMAGE_BEFORE" ]] || fail "Assistant changed before resume"
[[ "$(docker inspect -f '{{.Image}}' "$DIRECTOR_C")" == "$DIRECTOR_IMAGE_BEFORE" ]] || fail "Director changed before resume"
[[ "$(docker inspect -f '{{.State.StartedAt}}' "$DB_C")" == "$DB_STARTED_BEFORE" ]] || fail "DB restarted before resume"
echo "BASELINE_REPAIR_ISOLATION=PASS"

# Continue only the not-yet-run canonical Audio fresh-SAS + Web certification.
gh api "repos/$REMOTE/contents/scripts/deploy-certify-v3-audio-download-refresh-dev-v3-20260912.sh?ref=$BRANCH" --jq .content | base64 -d > "$SRC/v3.sh"
chmod +x "$SRC/v3.sh"
bash -n "$SRC/v3.sh" || fail "V3 certification script syntax invalid"

echo
echo "===== RESUME FRESH AUDIO SAS + WEB CERTIFICATION ====="
if ! bash "$SRC/v3.sh"; then
  echo "===== AUDIO STATE AFTER V3 FAILURE ====="
  docker inspect -f 'state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}} restarts={{.RestartCount}}' "$AUDIO_C" || true
  docker logs --tail 180 "$AUDIO_C" 2>&1 || true
  fail "fresh Audio SAS/Web certification failed"
fi

[[ "$(docker inspect -f '{{.Image}}' "$AUDIO_W")" == "$AUDIO_W_IMAGE_BEFORE" ]] || fail "Audio worker image changed"
[[ "$(docker inspect -f '{{.Image}}' "$ASSISTANT_C")" == "$ASSISTANT_IMAGE_BEFORE" ]] || fail "Assistant image changed"
[[ "$(docker inspect -f '{{.Image}}' "$DIRECTOR_C")" == "$DIRECTOR_IMAGE_BEFORE" ]] || fail "Director image changed"
[[ "$(docker inspect -f '{{.State.StartedAt}}' "$DB_C")" == "$DB_STARTED_BEFORE" ]] || fail "DB restarted"

echo "AUDIO_WORKER_RUNTIME_PRESERVED=PASS"
echo "ASSISTANT_RUNTIME_PRESERVED=PASS"
echo "DIRECTOR_RUNTIME_PRESERVED=PASS"
echo "DATABASE_RUNTIME_PRESERVED=PASS"
echo "============================================================"
echo " FINAL VERDICT"
echo "============================================================"
echo "AUDIO_BASELINE_HEALTH=PASS"
echo "HEALTH_PROBE_CORRECTION=PASS"
echo "AUDIO_FRESH_SAS_CERTIFICATION=PASS"
echo "AUDIO_DOWNLOAD_PROXY_CERTIFICATION=PASS"
echo "WEB_AUDIO_REHYDRATION_CERTIFICATION=PASS"
echo "DATABASE_CHANGE=NONE"
echo "PRICING_CHANGE=NONE"
echo "PRODUCTION_TOUCH=NONE"
echo "AUDIO_DOWNLOAD_RESUME_AFTER_HEALTH_PROBE=PASS"
echo "log=$LOG"
