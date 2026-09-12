#!/usr/bin/env bash
set -euo pipefail

SRC="${V3_HOTFIX_SRC:-$HOME/workspace/desifaces-v3}"
BRANCH="fix/v3-director-face-premium-context-20260830"
STATE_DIR="${V3_HOTFIX_STATE_DIR:-$HOME/.desifaces/v3-face-audio-hotfix}"
PID_FILE="$STATE_DIR/pid"
LOG="$STATE_DIR/run.log"
LAUNCH_LOG="$STATE_DIR/launch.log"
RUNNER="$STATE_DIR/runner.sh"
STATUS_SCRIPT="$STATE_DIR/status.sh"

mkdir -p "$STATE_DIR"

if [ -f "$PID_FILE" ]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "ALREADY_RUNNING pid=$OLD_PID"
    echo "status: bash $STATUS_SCRIPT"
    exit 0
  fi
fi

cd "$SRC"
git fetch origin "$BRANCH" >> "$LAUNCH_LOG" 2>&1

# Materialize the exact runner and status helper from the remote branch without
# switching/resetting the user's active V3 workspace.
git show "origin/$BRANCH:scripts/v3-face-audio-hotfix-runner.sh" > "$RUNNER"
git show "origin/$BRANCH:scripts/v3-face-audio-hotfix-status.sh" > "$STATUS_SCRIPT"
chmod +x "$RUNNER" "$STATUS_SCRIPT"

: > "$LOG"
rm -f "$STATE_DIR/exit_code" "$STATE_DIR/status" "$STATE_DIR/sha"

nohup env \
  V3_HOTFIX_SRC="$SRC" \
  V3_HOTFIX_STATE_DIR="$STATE_DIR" \
  bash "$RUNNER" >/dev/null 2>&1 < /dev/null &
PID=$!
printf '%s\n' "$PID" > "$PID_FILE"
disown "$PID" 2>/dev/null || true

sleep 1

if kill -0 "$PID" 2>/dev/null; then
  echo "STARTED pid=$PID"
else
  echo "START_FAILED pid=$PID"
  tail -n 40 "$LOG" 2>/dev/null || true
  exit 1
fi

echo "terminal is free; deployment continues detached"
echo "status: bash $STATUS_SCRIPT"
