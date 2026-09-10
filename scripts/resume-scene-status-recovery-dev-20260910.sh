#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
TARGET_SHA="97715a32bd88af8170bdb58729cbdaf5559d7bde"
WEB_CONTAINER="df-v3-web"
PORT="13000"
ORIGINAL_COMMIT="eef1d00aecf8e0cbf8e2214f1a77c0d3c2404cef"
ORIGINAL_PATH="scripts/deploy-certify-scene-status-recovery-dev-20260910.sh"
RAW_BASE="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend"

fail(){ echo "FAIL: $*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
for x in docker curl pgrep awk sort tail; do need "$x"; done
[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "run only on $EXPECTED_HOST; current=$(hostname -s)"

echo "============================================================"
echo " desifaces DEV — RESTART-SAFE SCENE STATUS WEB RESUME"
echo "============================================================"
echo "host=$(hostname -s)"
echo "environment=DEV_ONLY"
echo "production_touch=FORBIDDEN"
echo "backend_restart=NONE"
echo "provider_generation=NONE"
echo "target_web_sha=$TARGET_SHA"

# If the interrupted deployment actually survived the terminal disconnect, do
# not race it. Wait for it to finish, then evaluate the resulting web slot.
PIDS="$(pgrep -f '[d]eploy-certify-scene-status-recovery-dev-20260910.sh' || true)"
if [[ -n "$PIDS" ]]; then
  echo "PREVIOUS_DEPLOYMENT_PROCESS=RUNNING"
  for i in $(seq 1 120); do
    sleep 2
    PIDS="$(pgrep -f '[d]eploy-certify-scene-status-recovery-dev-20260910.sh' || true)"
    [[ -z "$PIDS" ]] && break
    (( i % 15 == 0 )) && echo "waiting_for_previous_deployment=${i}x2s"
  done
  [[ -z "$PIDS" ]] || fail "previous deployment is still running after 4 minutes; no concurrent recovery attempted"
  echo "PREVIOUS_DEPLOYMENT_PROCESS=FINISHED"
else
  echo "PREVIOUS_DEPLOYMENT_PROCESS=NONE"
fi

http_ok(){
  [[ "$(curl -sS --connect-timeout 2 --max-time 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/auth/login" 2>/dev/null || true)" == "200" ]]
}

web_sha(){
  docker inspect "$WEB_CONTAINER" --format '{{index .Config.Labels "desifaces.web_sha"}}' 2>/dev/null || true
}

web_status(){
  docker inspect "$WEB_CONTAINER" --format '{{.State.Status}}' 2>/dev/null || true
}

TARGET_ACTIVE=0
if docker inspect "$WEB_CONTAINER" >/dev/null 2>&1; then
  echo "current_web_status=$(web_status)"
  echo "current_web_sha=$(web_sha)"
  if [[ "$(web_status)" == "running" && "$(web_sha)" == "$TARGET_SHA" ]] && http_ok; then
    TARGET_ACTIVE=1
  fi
else
  echo "current_web_status=missing"
fi

if (( TARGET_ACTIVE == 1 )); then
  echo "TARGET_WEB_ALREADY_ACTIVE=PASS"
  # Remove only stale scene-status candidate/rollback slots. The active web is kept.
  mapfile -t stale < <(docker ps -a --format '{{.Names}}' | grep -E '^df-v3-web-(candidate|rollback)-scene-status-' || true)
  if ((${#stale[@]})); then
    docker rm -f "${stale[@]}" >/dev/null 2>&1 || true
    echo "STALE_SCENE_STATUS_SLOTS_CLEANED=${#stale[@]}"
  else
    echo "STALE_SCENE_STATUS_SLOTS_CLEANED=0"
  fi
  cat <<EOF

============================================================
 DEV SCENE STATUS WEB RESUME PASS
============================================================
TARGET_WEB_ALREADY_ACTIVE=PASS
DEV_WEB_HTTP=PASS
web_sha=$TARGET_SHA
BACKEND_RUNTIME_TOUCHED=NO
PRODUCTION_TOUCH=NONE
NEXT=HARD_REFRESH_EXISTING_STORY_AND_VERIFY_AUTHORITATIVE_SCENE_STATUS
EOF
  exit 0
fi

# Reconcile an interrupted cutover. If the canonical web slot is absent/unhealthy
# and a rollback slot exists, restore the newest rollback before retrying.
mapfile -t rollbacks < <(docker ps -a --format '{{.Names}}' | grep '^df-v3-web-rollback-scene-status-' | sort || true)
if ((${#rollbacks[@]})); then
  latest_rollback="${rollbacks[-1]}"
  echo "rollback_slot_found=$latest_rollback"
  if ! docker inspect "$WEB_CONTAINER" >/dev/null 2>&1 || [[ "$(web_status)" != "running" ]] || ! http_ok; then
    docker rm -f "$WEB_CONTAINER" >/dev/null 2>&1 || true
    docker rename "$latest_rollback" "$WEB_CONTAINER"
    docker update --restart=unless-stopped "$WEB_CONTAINER" >/dev/null 2>&1 || true
    docker start "$WEB_CONTAINER" >/dev/null 2>&1 || true
    sleep 2
    http_ok || fail "restored rollback web did not become healthy on 127.0.0.1:${PORT}"
    echo "INTERRUPTED_WEB_SLOT_ROLLBACK=RESTORED"
  fi
fi

# Clean only orphaned candidate containers from this exact deployment family.
mapfile -t candidates < <(docker ps -a --format '{{.Names}}' | grep '^df-v3-web-candidate-scene-status-' || true)
if ((${#candidates[@]})); then
  docker rm -f "${candidates[@]}" >/dev/null 2>&1 || true
  echo "ORPHANED_CANDIDATES_CLEANED=${#candidates[@]}"
else
  echo "ORPHANED_CANDIDATES_CLEANED=0"
fi

# Remove stale rollback slots only when the canonical web is healthy. They are
# disposable checkpoints from the interrupted attempt, not application data.
if docker inspect "$WEB_CONTAINER" >/dev/null 2>&1 && [[ "$(web_status)" == "running" ]] && http_ok; then
  mapfile -t old_rollbacks < <(docker ps -a --format '{{.Names}}' | grep '^df-v3-web-rollback-scene-status-' || true)
  if ((${#old_rollbacks[@]})); then
    docker rm -f "${old_rollbacks[@]}" >/dev/null 2>&1 || true
    echo "STALE_ROLLBACK_SLOTS_CLEANED=${#old_rollbacks[@]}"
  fi
fi

# Resume the exact immutable, syntax-checked deployment. It performs its own
# backend invariants and automatic web rollback.
TMP="$(mktemp /tmp/desifaces-scene-status-resume-XXXXXX.sh)"
cleanup(){ rm -f "$TMP"; }
trap cleanup EXIT
curl -fsSL "$RAW_BASE/$ORIGINAL_COMMIT/$ORIGINAL_PATH" -o "$TMP"
bash -n "$TMP"
echo "ORIGINAL_DEPLOYMENT_SYNTAX_GATE=PASS"
bash "$TMP"
