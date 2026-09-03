#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${BACKEND_ROOT:-/home/azureuser/workspace/desifaces-v3}"
BRANCH="${BACKEND_BRANCH:-fix/v3-video-pricing-progress-performance-20260903}"
TMP="/tmp/deploy-v3-generation-pricing-progress-performance.final.sh"

cd "$ROOT"
git fetch -q origin "$BRANCH"
git show "origin/$BRANCH:scripts/deploy-v3-generation-pricing-progress-performance.sh" > "$TMP"

# Release hardening performed before the underlying launcher runs:
# 1) use structural Face-progress patcher v2 rather than the brittle whole-method
#    literal replacement in v1;
# 2) rely on explicit Docker source regression/typecheck/Next build, not searching
#    optimized .next bundles for a source component symbol.
python3 - "$TMP" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
s=p.read_text()
old='python3 scripts/apply-v3-video-pricing-progress-performance-source.py\n'
new='python3 scripts/apply-v3-video-pricing-progress-performance-source-v2.py\n'
if old not in s:
    raise SystemExit('release source patcher invocation anchor missing')
s=s.replace(old,new,1)
needle='docker exec "$WEB_CONTAINER" sh -lc "grep -R -q \'GenerationProgress\' /app/.next" || fail "deployed web progress component not found in build output"\n'
if needle in s:
    s=s.replace(needle, '', 1)
p.write_text(s)
PY

# Fail before any DB/runtime mutation if the corrected source patcher is absent.
git cat-file -e "origin/$BRANCH:scripts/apply-v3-video-pricing-progress-performance-source-v2.py" 2>/dev/null || {
  echo 'FAIL: corrected source patcher v2 missing from release branch' >&2
  exit 2
}

exec bash "$TMP"
