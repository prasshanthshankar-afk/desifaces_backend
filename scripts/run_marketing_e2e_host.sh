#!/usr/bin/env bash
set -euo pipefail

: "${DF_EMAIL:?Set DF_EMAIL}"
: "${DF_PASSWORD:?Set DF_PASSWORD}"

CORE_URL="${CORE_URL:-http://localhost:8000}"

# Find host-mapped marketing port (svc-marketing or df-svc-marketing)
mp="$(docker compose --env-file infra/.env port svc-marketing 8000 2>/dev/null || true)"
if [[ -z "$mp" ]]; then
  mp="$(docker compose --env-file infra/.env port df-svc-marketing 8000 2>/dev/null || true)"
fi

if [[ -z "$mp" ]]; then
  echo "ERROR: Could not find host port mapping for svc-marketing:8000." >&2
  echo "Try: docker compose --env-file infra/.env ps" >&2
  exit 2
fi

# mp like "0.0.0.0:8010" or "127.0.0.1:8010"
MARKETING_URL="${MARKETING_URL:-http://${mp#*:}:${mp##*:}}"
# safer parse:
HOST_PORT="${mp##*:}"
MARKETING_URL="http://localhost:${HOST_PORT}"

MODE="${MARKETING_MODE:-stage}"
RECIPE="${MARKETING_RECIPE:-FACE_AUDIO_VIDEO}"
INDUSTRY="${MARKETING_INDUSTRY:-creator_tools}"
LANG="${MARKETING_LANGUAGE:-en}"
TARGET_SECONDS="${MARKETING_TARGET_SECONDS:-10}"
TIMEOUT_S="${MARKETING_E2E_TIMEOUT_S:-1800}"
INTERVAL_S="${MARKETING_E2E_POLL_INTERVAL_S:-3}"
ONLY="${MARKETING_ONLY:-}"

ARGS=(
  --email "$DF_EMAIL"
  --password "$DF_PASSWORD"
  --core-url "$CORE_URL"
  --marketing-url "$MARKETING_URL"
  --mode "$MODE"
  --recipe "$RECIPE"
  --industry "$INDUSTRY"
  --language "$LANG"
  --target-seconds "$TARGET_SECONDS"
  --timeout "$TIMEOUT_S"
  --interval "$INTERVAL_S"
)

if [[ -n "$ONLY" ]]; then
  ARGS+=( --only "$ONLY" )
fi

echo "[run] core_url=$CORE_URL marketing_url=$MARKETING_URL mode=$MODE recipe=$RECIPE only=${ONLY:-<all>}"

python3 services/svc-marketing/app/app/scripts/e2e/df_e2e_marketing_channels.py "${ARGS[@]}"
