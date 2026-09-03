#!/usr/bin/env bash
set -Eeuo pipefail

# Immutable application source pins for the September 3 production launch.
export BACKEND_SHA="${BACKEND_SHA:-21267cf8f0a25622ceb83f74e3abb8aea5f11b6a}"
export WEB_SHA="${WEB_SHA:-38ee936c2e4c22697c78d30d54292879ef81c96c}"

need(){ command -v "$1" >/dev/null 2>&1 || { echo "FAIL: missing required command: $1" >&2; exit 2; }; }
need gh
need base64

TMP="$(mktemp /tmp/desifaces-v3-production-cutover.XXXXXX.sh)"
trap 'rm -f "$TMP"' EXIT

gh api \
  'repos/prasshanthshankar-afk/desifaces_backend/contents/scripts/launch-v3-production-20260903.sh?ref=main' \
  --jq .content | base64 -d > "$TMP"

bash -n "$TMP"
echo "SECURE_LAUNCH_PINS=PASS backend_sha=$BACKEND_SHA web_sha=$WEB_SHA"
exec bash "$TMP"
