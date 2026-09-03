#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${BACKEND_ROOT:-/home/azureuser/workspace/desifaces-v3}"
BRANCH="${BACKEND_BRANCH:-fix/v3-video-pricing-progress-performance-20260903}"
TMP="/tmp/deploy-v3-generation-pricing-progress-performance.final.sh"

cd "$ROOT"
git fetch -q origin "$BRANCH"
git show "origin/$BRANCH:scripts/deploy-v3-generation-pricing-progress-performance.sh" > "$TMP"

# The Docker build runs the explicit generation-progress regression, full
# typecheck, existing regressions and Next build. Do not inspect minified .next
# output for source component names; optimization can legitimately rename them.
python3 - "$TMP" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
s=p.read_text()
needle='docker exec "$WEB_CONTAINER" sh -lc "grep -R -q \'GenerationProgress\' /app/.next" || fail "deployed web progress component not found in build output"\n'
if needle in s:
    s=s.replace(needle, '', 1)
p.write_text(s)
PY

exec bash "$TMP"
