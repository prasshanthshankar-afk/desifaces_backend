#!/usr/bin/env bash
# DEV-only, read-only language/TTS catalog inventory. Run on desifaces-dev.
set -Eeuo pipefail
if [[ "$(hostname -s)" != "desifaces-dev" ]]; then
  echo "STOP: only desifaces-dev is allowed" >&2
  exit 2
fi
if [[ -z "${DEV_READONLY_DATABASE_URL:-}" ]]; then
  echo "STOP: set DEV_READONLY_DATABASE_URL for a read-only DEV database user" >&2
  exit 2
fi
if ! command -v psql >/dev/null 2>&1; then
  echo "STOP: psql client unavailable" >&2
  exit 2
fi
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
sql="$repo_root/scripts/ops/dev_global_speech_locale_inventory_readonly.sql"
if [[ ! -r "$sql" ]]; then
  echo "STOP: missing read-only inventory SQL" >&2
  exit 2
fi
echo "DEV_GLOBAL_LANGUAGE_AUDIT=START"
# Do not print connection URI; never enable psql echo of connection data.
PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=30000' \
  psql "$DEV_READONLY_DATABASE_URL" -X -v ON_ERROR_STOP=1 -P pager=off \
  -f "$sql"
echo "DEV_GLOBAL_LANGUAGE_AUDIT=SQL_COMPLETED"
echo "IMPORTANT: Catalog coverage is NOT real provider API/voice/translation certification."
