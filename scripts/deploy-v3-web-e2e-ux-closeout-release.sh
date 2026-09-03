#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${BACKEND_ROOT:-/home/azureuser/workspace/desifaces-v3}"
BRANCH="feature/v3-web-e2e-ux-closeout-20260903"
TMP="/tmp/deploy-v3-web-e2e-ux-closeout.final.sh"

cd "$ROOT"
git fetch -q origin "$BRANCH"
git show "origin/$BRANCH:scripts/deploy-v3-web-e2e-ux-closeout.sh" > "$TMP"

# Add the legacy-Saved-Work compatibility patch to the source-only web stage.
# This executes before rollback snapshot or any runtime mutation.
python3 - "$TMP" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
s=p.read_text()
old='python3 scripts/apply-v3-face-voice-gender-handoff.py\npython3 scripts/test-v3-web-e2e-ux-closeout.py\n'
new='python3 scripts/apply-v3-face-voice-gender-handoff.py\npython3 scripts/apply-v3-voice-legacy-face-gender.py\npython3 scripts/test-v3-web-e2e-ux-closeout.py\n'
if old not in s:
    raise SystemExit('web closeout compatibility insertion anchor missing')
s=s.replace(old,new,1)
p.write_text(s)
PY

git cat-file -e "origin/$BRANCH:scripts/apply-v3-voice-legacy-face-gender.py" 2>/dev/null || {
  echo 'FAIL: legacy Face gender compatibility patch missing' >&2
  exit 2
}

exec bash "$TMP"
