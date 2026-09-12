#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${V3_HOTFIX_STATE_DIR:-$HOME/.desifaces/v3-face-audio-hotfix}"
PID_FILE="$STATE_DIR/pid"
STATUS_FILE="$STATE_DIR/status"
SHA_FILE="$STATE_DIR/sha"
RC_FILE="$STATE_DIR/exit_code"
LOG="$STATE_DIR/run.log"

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
STATUS="$(cat "$STATUS_FILE" 2>/dev/null || true)"
SHA="$(cat "$SHA_FILE" 2>/dev/null || true)"
RC="$(cat "$RC_FILE" 2>/dev/null || true)"

if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  RUNTIME="RUNNING"
else
  RUNTIME="NOT_RUNNING"
fi

echo "runtime=$RUNTIME"
[ -n "$PID" ] && echo "pid=$PID"
[ -n "$STATUS" ] && echo "status=$STATUS"
[ -n "$SHA" ] && echo "sha=$SHA"
[ -n "$RC" ] && echo "exit_code=$RC"

echo "--- latest log ---"
tail -n 80 "$LOG" 2>/dev/null || echo "log not created yet"
