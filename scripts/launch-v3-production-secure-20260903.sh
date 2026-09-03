#!/usr/bin/env bash
set -Eeuo pipefail

# Immutable application source pins for the September 3 production launch.
export BACKEND_SHA="${BACKEND_SHA:-21267cf8f0a25622ceb83f74e3abb8aea5f11b6a}"
export WEB_SHA="${WEB_SHA:-38ee936c2e4c22697c78d30d54292879ef81c96c}"

need(){ command -v "$1" >/dev/null 2>&1 || { echo "FAIL: missing required command: $1" >&2; exit 2; }; }
need gh
need base64
need python3

BASE="$(mktemp /tmp/desifaces-v3-production-cutover.XXXXXX.sh)"
INGRESS="$(mktemp /tmp/desifaces-v3-public-ingress.XXXXXX.sh)"
trap 'rm -f "$BASE" "$INGRESS"' EXIT

gh api \
  'repos/prasshanthshankar-afk/desifaces_backend/contents/scripts/launch-v3-production-20260903.sh?ref=main' \
  --jq .content | base64 -d > "$BASE"
gh api \
  'repos/prasshanthshankar-afk/desifaces_backend/contents/scripts/ensure-v3-web-public-ingress-20260903.sh?ref=main' \
  --jq .content | base64 -d > "$INGRESS"

# RELEASE HOTFIX 2026-09-03:
# Bash reserves GROUPS as a special array containing the current Unix group IDs.
# The original launch script used GROUPS as an ordinary service-group array,
# which fails under `set -u` before runtime mutation. Repair only standalone
# GROUPS identifiers in the downloaded temporary launcher. This is idempotent:
# if the base source is already permanently repaired, nothing changes.
python3 - "$BASE" <<'PY'
from pathlib import Path
import re, sys
p = Path(sys.argv[1])
s = p.read_text()
pattern = re.compile(r'(?<![A-Za-z0-9_])GROUPS(?![A-Za-z0-9_])')
count = len(pattern.findall(s))
if count:
    s = pattern.sub('SERVICE_GROUPS', s)
    p.write_text(s)
if re.search(r'(?<![A-Za-z0-9_])GROUPS=', s) or '${GROUPS[' in s:
    raise SystemExit('FAIL: reserved Bash GROUPS identifier remains in temporary launcher')
if 'SERVICE_GROUPS=(pricing audio face fusion extension)' not in s:
    raise SystemExit('FAIL: corrected SERVICE_GROUPS declaration not found')
if '${SERVICE_GROUPS[@]}' not in s:
    raise SystemExit('FAIL: corrected SERVICE_GROUPS loop reference not found')
print(f'RESERVED_GROUPS_HOTFIX=PASS replacements={count}')
PY

bash -n "$BASE"
bash -n "$INGRESS"
echo "SECURE_LAUNCH_PINS=PASS backend_sha=$BACKEND_SHA web_sha=$WEB_SHA"

echo ""
echo "===== APPLICATION CUTOVER ====="
bash "$BASE"

echo ""
echo "===== PUBLIC HTTPS CUTOVER ====="
bash "$INGRESS"

echo ""
echo "============================================================"
echo " desifaces.ai SEPTEMBER 3 WEB LAUNCH PASS"
echo "============================================================"
echo "backend_sha=$BACKEND_SHA"
echo "web_sha=$WEB_SHA"
echo "public_web=https://web.desifaces.ai"
