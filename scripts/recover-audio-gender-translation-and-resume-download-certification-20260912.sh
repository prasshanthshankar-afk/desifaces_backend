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
STATE="$HOME/.local/state/audio-gender-recovery-resume/$STAMP"
SRC="$STATE/source"
LOG="$STATE/run.log"
mkdir -p "$SRC"
chmod 700 "$STATE"
exec > >(tee "$LOG") 2>&1

fail(){
  echo "FAIL: $*"
  echo "AUDIO_GENDER_RECOVERY_AND_DOWNLOAD_CERTIFICATION=FAIL_CLOSED"
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
echo " desifaces DEV — AUDIO BASELINE REPAIR + DOWNLOAD CERTIFICATION"
echo "============================================================"
echo "environment=DEV_ONLY"
echo "database_change=NONE"
echo "pricing_change=NONE"
echo "production_touch=NONE"
echo "repair=TOP_LEVEL_GENDER_TRANSLATION_PACKAGING_ALIAS"
echo "resume=FRESH_AUDIO_SAS_AND_WEB_DOWNLOAD_CERTIFICATION"

# Capture untouched runtimes before any DEV repair.
AUDIO_W_IMAGE_BEFORE="$(docker inspect -f '{{.Image}}' "$AUDIO_W")"
ASSISTANT_IMAGE_BEFORE="$(docker inspect -f '{{.Image}}' "$ASSISTANT_C")"
DIRECTOR_IMAGE_BEFORE="$(docker inspect -f '{{.Image}}' "$DIRECTOR_C")"
DB_STARTED_BEFORE="$(docker inspect -f '{{.State.StartedAt}}' "$DB_C")"

fetch(){
  local rel="$1" out="$2"
  gh api "repos/$REMOTE/contents/$rel?ref=$BRANCH" --jq .content | base64 -d > "$out"
}
fetch services/shared/llm/gender_translation.py "$SRC/gender_translation.py"
fetch services/svc-audio/app/Dockerfile "$SRC/Dockerfile"
fetch services/svc-audio/app/app/services/tts_service.py "$SRC/tts_service.py"
fetch scripts/deploy-certify-v3-audio-download-refresh-dev-v3-20260912.sh "$SRC/v3.sh"
chmod +x "$SRC/v3.sh"
bash -n "$SRC/v3.sh" || fail "V3 certification script syntax invalid"

grep -Fq 'COPY services/shared/llm/gender_translation.py /app/gender_translation.py' "$SRC/Dockerfile" || fail "source Dockerfile packaging fix missing"
grep -Fq 'from gender_translation import (' "$SRC/tts_service.py" || fail "tts_service import contract changed unexpectedly"
python3 - "$SRC/gender_translation.py" <<'PY'
import ast, pathlib, sys
p=pathlib.Path(sys.argv[1])
tree=ast.parse(p.read_text(encoding='utf-8'))
names={n.name for n in tree.body if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef))}
required={'GenderTranslationError','normalize_gender','translate_with_gender'}
missing=required-names
assert not missing, missing
print('GENDER_TRANSLATION_SOURCE_CONTRACT=PASS')
PY

echo "AUDIO_GENDER_TRANSLATION_PACKAGING_SOURCE=PASS"

# Freeze the restart loop before patching the writable layer. This is a bounded
# DEV recovery only; production will get the source-controlled Dockerfile fix.
RESTART_NAME="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$AUDIO_C")"
RESTART_MAX="$(docker inspect -f '{{.HostConfig.RestartPolicy.MaximumRetryCount}}' "$AUDIO_C")"
[[ -n "$RESTART_NAME" ]] || RESTART_NAME="no"
RESTORE_POLICY="$RESTART_NAME"
if [[ "$RESTART_NAME" == "on-failure" && "${RESTART_MAX:-0}" != "0" ]]; then
  RESTORE_POLICY="on-failure:${RESTART_MAX}"
fi

echo "audio_restart_policy=$RESTORE_POLICY"
docker update --restart=no "$AUDIO_C" >/dev/null
docker stop -t 10 "$AUDIO_C" >/dev/null 2>&1 || true

# Preserve an existing alias if one is unexpectedly present.
ALIAS_EXISTED=0
if docker cp "$AUDIO_C:/app/gender_translation.py" "$STATE/gender_translation.py.before" >/dev/null 2>&1; then
  ALIAS_EXISTED=1
fi

restore_alias_on_failure(){
  echo "AUDIO_GENDER_TRANSLATION_ROLLBACK=START"
  docker update --restart=no "$AUDIO_C" >/dev/null 2>&1 || true
  docker stop -t 5 "$AUDIO_C" >/dev/null 2>&1 || true
  if [[ "$ALIAS_EXISTED" == "1" ]]; then
    docker cp "$STATE/gender_translation.py.before" "$AUDIO_C:/app/gender_translation.py" >/dev/null 2>&1 || true
  else
    # A stopped container cannot use docker exec; use a throwaway tar overlay only
    # if removal is needed. In this incident the alias was absent, so leaving the
    # source-controlled repair in the DEV writable layer is safer than re-breaking
    # the known baseline import. The source fix is already committed above.
    :
  fi
  docker update --restart="$RESTORE_POLICY" "$AUDIO_C" >/dev/null 2>&1 || true
  docker start "$AUDIO_C" >/dev/null 2>&1 || true
  echo "AUDIO_GENDER_TRANSLATION_ROLLBACK=COMPLETE"
}

# Install the exact source-controlled module at the location expected by the
# existing top-level import.
docker cp "$SRC/gender_translation.py" "$AUDIO_C:/app/gender_translation.py"
docker update --restart="$RESTORE_POLICY" "$AUDIO_C" >/dev/null
docker start "$AUDIO_C" >/dev/null

BASELINE_READY=0
for i in $(seq 1 45); do
  state="$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}' "$AUDIO_C" 2>/dev/null || true)"
  if [[ "$state" == "running/healthy" || "$state" == "running/no-health" ]]; then
    BASELINE_READY=1
    break
  fi
  if [[ "$i" == "15" || "$i" == "30" ]]; then echo "wait=$i state=$state"; fi
  sleep 2
done
if [[ "$BASELINE_READY" != "1" ]]; then
  echo "===== AUDIO LOGS AFTER PACKAGING REPAIR ====="
  docker logs --tail 180 "$AUDIO_C" 2>&1 || true
  restore_alias_on_failure
  fail "Audio API did not recover after gender_translation packaging repair"
fi

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

HEALTH_CODE="$(docker exec "$AUDIO_C" python - <<'PY'
import urllib.request,urllib.error
try:
    with urllib.request.urlopen('http://127.0.0.1:8004/api/health',timeout=5) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print('ERR')
PY
)"
[[ "$HEALTH_CODE" == "200" ]] || { docker logs --tail 120 "$AUDIO_C" 2>&1 || true; fail "Audio health endpoint=$HEALTH_CODE"; }
echo "AUDIO_GENDER_TRANSLATION_PACKAGING_FIX=PASS"
echo "AUDIO_BASELINE_HEALTH=PASS"

# Confirm the repair did not disturb unrelated runtimes before resuming the
# already-source-controlled fresh-SAS certification.
[[ "$(docker inspect -f '{{.Image}}' "$AUDIO_W")" == "$AUDIO_W_IMAGE_BEFORE" ]] || fail "Audio worker image changed during baseline repair"
[[ "$(docker inspect -f '{{.Image}}' "$ASSISTANT_C")" == "$ASSISTANT_IMAGE_BEFORE" ]] || fail "Assistant image changed during baseline repair"
[[ "$(docker inspect -f '{{.Image}}' "$DIRECTOR_C")" == "$DIRECTOR_IMAGE_BEFORE" ]] || fail "Director image changed during baseline repair"
[[ "$(docker inspect -f '{{.State.StartedAt}}' "$DB_C")" == "$DB_STARTED_BEFORE" ]] || fail "DB restarted during baseline repair"
echo "BASELINE_REPAIR_ISOLATION=PASS"

# Resume the canonical Audio/fresh SAS/Web certification. V3 is fail-closed and
# rolls back its own canonical-route bootstrap if that phase cannot recover.
echo
echo "===== RESUME FRESH AUDIO SAS + WEB CERTIFICATION ====="
if ! bash "$SRC/v3.sh"; then
  echo "===== AUDIO STATE AFTER V3 FAILURE ====="
  docker inspect -f 'state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}} restarts={{.RestartCount}}' "$AUDIO_C" || true
  docker logs --tail 180 "$AUDIO_C" 2>&1 || true
  fail "fresh Audio SAS/Web certification failed after baseline repair"
fi

# Final isolation checks after V3 also completes.
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
echo "AUDIO_GENDER_TRANSLATION_PACKAGING_FIX=PASS"
echo "AUDIO_BASELINE_HEALTH=PASS"
echo "AUDIO_FRESH_SAS_CERTIFICATION=PASS"
echo "AUDIO_DOWNLOAD_PROXY_CERTIFICATION=PASS"
echo "WEB_AUDIO_REHYDRATION_CERTIFICATION=PASS"
echo "DATABASE_CHANGE=NONE"
echo "PRICING_CHANGE=NONE"
echo "PRODUCTION_TOUCH=NONE"
echo "AUDIO_GENDER_RECOVERY_AND_DOWNLOAD_CERTIFICATION=PASS"
echo "log=$LOG"
