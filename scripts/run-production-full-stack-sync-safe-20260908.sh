#!/usr/bin/env bash
set -Eeuo pipefail

REPO="prasshanthshankar-afk/desifaces_backend"
REF="audit/full-stack-sync-20260908"
SRC="scripts/apply-production-full-stack-sync-20260908.sh"
DEV_HOST="${DEV_HOST:-desifaces-dev}"
DEV_ROOT="/home/azureuser/workspace/desifaces-v3"
TMP="$(mktemp /tmp/desifaces-full-sync-safe.XXXXXX.sh)"
trap 'rm -f "$TMP"' EXIT

need(){ command -v "$1" >/dev/null 2>&1 || { echo "FAIL: missing required command: $1" >&2; exit 2; }; }
for x in gh python3 ssh base64 tr; do need "$x"; done
[[ "$(uname -s)" == "Darwin" ]] || { echo "FAIL: run from Mac release environment" >&2; exit 2; }

gh api "repos/$REPO/contents/$SRC?ref=$REF" --jq .content | base64 -d > "$TMP"

python3 - "$TMP" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
s=p.read_text()

# Fix nounset declaration defect if present.
old_decl='  local table="$1" where="$2" file="$RUN/md/${table}.csv"\n'
new_decl='  local table="$1"\n  local where="$2"\n  local file="$RUN/md/${table}.csv"\n'
if old_decl in s:
    s=s.replace(old_decl,new_decl,1)

# Replace unsafe nested-SSH filter transport with base64-safe transport.
old='''  ssh "$DEV_HOST" "DBC='$DB_C' DBU='$DB_USER' DBN='$DB_NAME' T='$table' W='$where' bash -s" > "$file" <<'REMOTE'\nset -Eeuo pipefail\nSQL="COPY (SELECT * FROM public.\\\"$T\\\""\n[[ -n "$W" ]] && SQL+=" WHERE $W"\nSQL+=") TO STDOUT WITH CSV HEADER"\ndocker exec "$DBC" psql -X -q -v ON_ERROR_STOP=1 -U "$DBU" -d "$DBN" -c "$SQL"\nREMOTE\n'''
new='''  local dbc64 dbu64 dbn64 t64 w64\n  dbc64="$(printf '%s' "$DB_C" | base64 | tr -d '\\n')"\n  dbu64="$(printf '%s' "$DB_USER" | base64 | tr -d '\\n')"\n  dbn64="$(printf '%s' "$DB_NAME" | base64 | tr -d '\\n')"\n  t64="$(printf '%s' "$table" | base64 | tr -d '\\n')"\n  w64="$(printf '%s' "$where" | base64 | tr -d '\\n')"\n  ssh "$DEV_HOST" "DBC64='$dbc64' DBU64='$dbu64' DBN64='$dbn64' T64='$t64' W64='$w64' bash -s" > "$file" <<'REMOTE'\nset -Eeuo pipefail\nDBC="$(printf '%s' "$DBC64" | base64 -d)"\nDBU="$(printf '%s' "$DBU64" | base64 -d)"\nDBN="$(printf '%s' "$DBN64" | base64 -d)"\nT="$(printf '%s' "$T64" | base64 -d)"\nW="$(printf '%s' "$W64" | base64 -d)"\nSQL="COPY (SELECT * FROM public.\\\"$T\\\""\n[[ -n "$W" ]] && SQL+=" WHERE $W"\nSQL+=") TO STDOUT WITH CSV HEADER"\ndocker exec "$DBC" psql -X -q -v ON_ERROR_STOP=1 -U "$DBU" -d "$DBN" -c "$SQL"\nREMOTE\n'''
if s.count(old) != 1:
    raise SystemExit(f"FAIL: expected one unsafe export transport block, found {s.count(old)}")
s=s.replace(old,new,1)
p.write_text(s)
PY

bash -n "$TMP"
! grep -Fq "W='$where'" "$TMP" 2>/dev/null || { echo "FAIL: unsafe where transport remains" >&2; exit 3; }
grep -Fq 'W64=' "$TMP"
grep -Fq 'base64 -d' "$TMP"
echo "SYNC_LAUNCHER_EXPORT_TRANSPORT_FIX=PASS"

# Read-only preflight: execute every filtered export predicate on dev before any
# production snapshot or mutation can occur.
echo "===== PREFLIGHT: DEV FILTERED EXPORT QUERIES ====="
ssh "$DEV_HOST" "DEV_ROOT='$DEV_ROOT' bash -s" <<'REMOTE'
set -Eeuo pipefail
ROOT="$DEV_ROOT"
DB_C=""
for c in desifaces-v3-db desifaces-db; do
  docker inspect "$c" >/dev/null 2>&1 && { DB_C="$c"; break; }
done
[[ -n "$DB_C" ]] || DB_C="$(docker ps --format '{{.Names}}' | grep -E '(^|-)db$|desifaces.*db' | head -1 || true)"
[[ -n "$DB_C" ]] || { echo 'FAIL: dev DB container not found'; exit 4; }
DB_USER="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_DB"')"

check(){
  local t="$1" w="$2" n
  n="$(docker exec "$DB_C" psql -X -A -t -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -c "SELECT count(*) FROM public.\"$t\" WHERE $w" | tr -d '[:space:]')"
  [[ "$n" =~ ^[0-9]+$ ]] || { echo "FAIL: preflight $t"; exit 5; }
  echo "PREFLIGHT|$t|rows=$n"
}
check tts_providers "provider_code = 'azure'"
check tts_provider_models "provider_code = 'azure'"
check tts_model_language_capabilities "provider_code = 'azure'"
check tts_model_locale_capabilities "provider_code = 'azure'"
check tts_voices "provider = 'azure'"
check tts_voice_model_capabilities "provider_code = 'azure'"
check tts_voice_locale_capabilities "voice_id IN (SELECT id FROM public.tts_voices WHERE provider='azure')"
echo "DEV_FILTERED_EXPORT_PREFLIGHT=PASS"
REMOTE

echo "SYNC_LAUNCHER_PREFLIGHT=PASS"
exec bash "$TMP"
