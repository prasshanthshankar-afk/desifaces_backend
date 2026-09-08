#!/usr/bin/env bash
set -Eeuo pipefail

REPO="prasshanthshankar-afk/desifaces_backend"
REF="audit/full-stack-sync-20260908"
SRC="scripts/apply-production-full-stack-sync-20260908.sh"
TMP="$(mktemp /tmp/desifaces-full-sync-fixed.XXXXXX.sh)"
trap 'rm -f "$TMP"' EXIT

command -v gh >/dev/null 2>&1 || { echo "FAIL: gh required" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "FAIL: python3 required" >&2; exit 2; }

gh api "repos/$REPO/contents/$SRC?ref=$REF" --jq .content | base64 -d > "$TMP"

python3 - "$TMP" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
s=p.read_text()
old='  local table="$1" where="$2" file="$RUN/md/${table}.csv"\n'
new='  local table="$1"\n  local where="$2"\n  local file="$RUN/md/${table}.csv"\n'
if s.count(old) != 1:
    raise SystemExit(f"FAIL: expected exactly one export_table declaration defect, found {s.count(old)}")
s=s.replace(old,new)
p.write_text(s)
PY

bash -n "$TMP"

grep -Fq 'local table="$1"' "$TMP"
grep -Fq 'local where="$2"' "$TMP"
grep -Fq 'local file="$RUN/md/${table}.csv"' "$TMP"
! grep -Fq 'local table="$1" where="$2" file="$RUN/md/${table}.csv"' "$TMP"

echo "SYNC_LAUNCHER_NOUNSET_FIX=PASS"
exec bash "$TMP"
