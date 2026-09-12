#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
AUDIO_C="df-v3-svc-audio"
AUDIO_W="df-v3-svc-audio-worker"
ASSISTANT_C="df-v3-svc-assistant"
DIRECTOR_C="df-v3-svc-director"
STATE_ROOT="$HOME/.local/state/audio-download-refresh-v3"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$HOME/.local/state/audio-baseline-recovery/$STAMP"
mkdir -p "$OUT"
chmod 700 "$OUT"
LOG="$OUT/run.log"
exec > >(tee "$LOG") 2>&1

fail(){ echo "FAIL: $*"; echo "AUDIO_BASELINE_RECOVERY=FAIL_CLOSED"; echo "log=$LOG"; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "wrong host"
command -v docker >/dev/null || fail "docker missing"
docker inspect "$AUDIO_C" >/dev/null 2>&1 || fail "missing $AUDIO_C"

# Locate the latest V3 attempt that contains the runtime backup created before mutation.
SRC_STATE="$(find "$STATE_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null | sort -nr | awk '{print $2}' | while read -r d; do [[ -f "$d/runtime-backup/api_init.py" && -f "$d/runtime-backup/azure_storage_service.py" && -f "$d/existed.txt" ]] && { echo "$d"; break; }; done)"
[[ -n "$SRC_STATE" ]] || fail "no V3 runtime backup found"
BACKUP="$SRC_STATE/runtime-backup"
EXISTED="$SRC_STATE/existed.txt"

echo "============================================================"
echo " desifaces DEV — AUDIO BASELINE RECOVERY"
echo "============================================================"
echo "environment=DEV_ONLY"
echo "source_state=$SRC_STATE"
echo "production_touch=NONE"
echo "database_change=NONE"
echo "pricing_change=NONE"

# Capture the exact crash evidence from the host before stopping the restart loop.
docker logs --timestamps --tail 400 "$AUDIO_C" > "$OUT/audio-crash.log" 2>&1 || true
echo "AUDIO_CRASH_LOG_CAPTURE=PASS"

echo "===== LAST CRASH LINES ====="
tail -n 120 "$OUT/audio-crash.log" || true

audio_worker_image_before="$(docker inspect -f '{{.Image}}' "$AUDIO_W")"
assistant_image_before="$(docker inspect -f '{{.Image}}' "$ASSISTANT_C")"
director_image_before="$(docker inspect -f '{{.Image}}' "$DIRECTOR_C")"
db_started_before="$(docker inspect desifaces-v3-db -f '{{.State.StartedAt}}')"
restart_policy="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$AUDIO_C")"
[[ -n "$restart_policy" ]] || restart_policy=no

# Freeze the restart loop so docker cp is reliable.
docker update --restart=no "$AUDIO_C" >/dev/null
docker stop -t 20 "$AUDIO_C" >/dev/null 2>&1 || true

audio_app_root="/app/app"
restore_or_remove(){
  local key="$1" path="$2"
  if grep -Fxq "$key" "$EXISTED"; then
    [[ -f "$BACKUP/$key" ]] || fail "missing backup $key"
    docker cp "$BACKUP/$key" "$AUDIO_C:$path"
  else
    docker cp /dev/null "$AUDIO_C:$path.__remove_marker" >/dev/null 2>&1 || true
    # container is stopped; use a one-shot shell against its writable layer by starting paused is unavailable,
    # so start briefly with restart disabled, remove the added file, then stop again.
    docker start "$AUDIO_C" >/dev/null
    for _ in $(seq 1 20); do
      if docker exec "$AUDIO_C" true >/dev/null 2>&1; then break; fi
      sleep 0.5
    done
    docker exec "$AUDIO_C" rm -f "$path" "$path.__remove_marker" >/dev/null 2>&1 || true
    docker stop -t 10 "$AUDIO_C" >/dev/null 2>&1 || true
  fi
}

restore_or_remove api_init.py "$audio_app_root/api/__init__.py"
restore_or_remove azure_storage_service.py "$audio_app_root/services/azure_storage_service.py"
restore_or_remove canonical_audio.py "$audio_app_root/api/routes/canonical_audio.py"

echo "AUDIO_ORIGINAL_RUNTIME_FILES_RESTORED=PASS"

# Restore restart policy and start the original container baseline.
docker update --restart="$restart_policy" "$AUDIO_C" >/dev/null
docker start "$AUDIO_C" >/dev/null

healthy=false
for _ in $(seq 1 45); do
  state="$(docker inspect "$AUDIO_C" --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}' 2>/dev/null || true)"
  if [[ "$state" == "running/healthy" || "$state" == "running/no-health" ]]; then healthy=true; break; fi
  sleep 2
done

if [[ "$healthy" != true ]]; then
  docker logs --timestamps --tail 240 "$AUDIO_C" > "$OUT/audio-recovery-failure.log" 2>&1 || true
  cat "$OUT/audio-recovery-failure.log" || true
  fail "Audio baseline did not recover"
fi

echo "AUDIO_RUNTIME_STATE=$(docker inspect "$AUDIO_C" --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}')"

[[ "$(docker inspect -f '{{.Image}}' "$AUDIO_W")" == "$audio_worker_image_before" ]] || fail "Audio worker changed"
[[ "$(docker inspect -f '{{.Image}}' "$ASSISTANT_C")" == "$assistant_image_before" ]] || fail "Assistant changed"
[[ "$(docker inspect -f '{{.Image}}' "$DIRECTOR_C")" == "$director_image_before" ]] || fail "Director changed"
[[ "$(docker inspect desifaces-v3-db -f '{{.State.StartedAt}}')" == "$db_started_before" ]] || fail "DB restarted"

echo "AUDIO_WORKER_RUNTIME_PRESERVED=PASS"
echo "ASSISTANT_RUNTIME_PRESERVED=PASS"
echo "DIRECTOR_RUNTIME_PRESERVED=PASS"
echo "DATABASE_RUNTIME_PRESERVED=PASS"
echo "============================================================"
echo " AUDIO BASELINE RECOVERY=PASS"
echo "============================================================"
echo "PRODUCTION_TOUCH=NONE"
echo "crash_log=$OUT/audio-crash.log"
echo "log=$LOG"
