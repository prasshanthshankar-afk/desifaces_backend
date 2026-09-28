#!/usr/bin/env bash
set -Eeuo pipefail

NAME="${1:-}"
shift || true

if [[ -z "$NAME" || "$#" -eq 0 ]]; then
  echo "usage: $0 <name> <command> [args...]" >&2
  exit 2
fi

LOG="/tmp/${NAME}.log"
PIDFILE="/tmp/${NAME}.pid"

if [[ -f "$PIDFILE" ]]; then
  OLD_PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "ALREADY_RUNNING pid=$OLD_PID log=$LOG"
    exit 3
  fi
fi

rm -f "$LOG" "$PIDFILE"

nohup timeout --signal=TERM --kill-after=15s 30m "$@" >"$LOG" 2>&1 < /dev/null &
PID=$!
printf '%s\n' "$PID" > "$PIDFILE"
disown "$PID" 2>/dev/null || true

echo "STARTED name=$NAME pid=$PID"
echo "LOG=$LOG"
echo "CHECK=tail -n 80 $LOG"
