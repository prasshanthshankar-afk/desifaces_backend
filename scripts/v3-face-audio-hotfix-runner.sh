#!/usr/bin/env bash
set -euo pipefail

SRC="${V3_HOTFIX_SRC:-$HOME/workspace/desifaces-v3}"
BRANCH="fix/v3-director-face-premium-context-20260830"
STATE_DIR="${V3_HOTFIX_STATE_DIR:-$HOME/.desifaces/v3-face-audio-hotfix}"
WORKTREE="${V3_HOTFIX_WORKTREE:-/tmp/desifaces-v3-face-audio-hotfix}"
LOG="$STATE_DIR/run.log"
STATUS_FILE="$STATE_DIR/status"
SHA_FILE="$STATE_DIR/sha"
RC_FILE="$STATE_DIR/exit_code"

mkdir -p "$STATE_DIR"

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
set_status() { printf '%s %s\n' "$(stamp)" "$1" > "$STATUS_FILE"; }

cleanup_on_exit() {
  local rc=$?
  printf '%s\n' "$rc" > "$RC_FILE"
  if [ "$rc" -eq 0 ]; then
    set_status "PASSED"
  else
    set_status "FAILED"
  fi
}
trap cleanup_on_exit EXIT

set_status "STARTING"

{
  echo "============================================================"
  echo " V3 FACE + AUDIO DETACHED HOTFIX RUNNER"
  echo "============================================================"
  echo "started_utc=$(stamp)"
  echo "source_workspace=$SRC"
  echo "branch=$BRANCH"

  cd "$SRC"

  echo
  echo "=== FETCH HOTFIX BRANCH ==="
  git fetch origin "$BRANCH"
  SHA="$(git rev-parse "origin/$BRANCH")"
  printf '%s\n' "$SHA" > "$SHA_FILE"
  echo "hotfix_sha=$SHA"

  echo
  echo "=== PREPARE ISOLATED WORKTREE ==="
  git worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
  rm -rf "$WORKTREE"
  git worktree prune
  git worktree add --detach "$WORKTREE" "$SHA"

  test -f "$SRC/infra/.env" || {
    echo "FAIL: source V3 env missing: $SRC/infra/.env"
    exit 1
  }
  cp -p "$SRC/infra/.env" "$WORKTREE/infra/.env"

  cd "$WORKTREE"
  test "$(git rev-parse HEAD)" = "$SHA"
  git diff --quiet -- .
  git diff --cached --quiet -- .
  test -f scripts/v3-face-audio-hotfix-certify.sh

  # Do not chmod or otherwise mutate any tracked file after the clean-tree gate.
  # The certifier is intentionally invoked through bash so its committed file
  # mode remains untouched and its own clean-tree gate sees an immutable tree.
  set_status "DEPLOYING"

  echo
  echo "=== RUN LIVE HOTFIX CERTIFICATION ==="
  bash scripts/v3-face-audio-hotfix-certify.sh

  echo
  echo "=== COMPLETE ==="
  echo "PASS: Face premium routing + Audio recovery deployment certified"
  echo "HEAD=$SHA"
  echo "completed_utc=$(stamp)"

  cd "$SRC"
  git worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
} >> "$LOG" 2>&1
