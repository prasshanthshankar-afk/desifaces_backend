#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/home/azureuser/workspace/desifaces}"
RELEASE_REF="release/v3-production-backend-closeout-20260904"
SCRIPT_COMMIT="6aaef39c524ff4d5472c432936046a73fe4bbb1f"
SOURCE_COMMIT="6aaef39c524ff4d5472c432936046a73fe4bbb1f"
WEB_COMMIT="0e832f2d14e8f7cfe1d74975ac1277c8f476bfdc"
SCRIPT_PATH="scripts/promote-certify-multiperson-aspect-face-readurl-prod-20260909.sh"

fail(){ echo "FAIL: $*" >&2; exit 1; }
command -v git >/dev/null 2>&1 || fail "git is required"
[[ -d "$ROOT/.git" ]] || fail "backend git repository not found at $ROOT"

WEB_SRC="${WEB_SRC:-}"
if [[ -z "$WEB_SRC" ]]; then
  for candidate in \
    /home/azureuser/workspace/desifaces-web/web \
    /home/azureuser/workspace/desifaces-web-review/web \
    /home/azureuser/workspace/desifaces_web/web; do
    if [[ -f "$candidate/Dockerfile" && -f "$candidate/components/MultiPersonDirector.tsx" ]]; then
      WEB_SRC="$candidate"
      break
    fi
  done
fi
[[ -n "$WEB_SRC" && -d "$WEB_SRC" ]] || fail "active production web source not found"
WEB_ROOT="$(git -C "$WEB_SRC" rev-parse --show-toplevel 2>/dev/null)" || fail "web git repository not found for $WEB_SRC"

# Fetch only repository metadata/objects. Working trees are not checked out or reset.
git -C "$ROOT" fetch --no-tags origin "$RELEASE_REF" >/dev/null
git -C "$WEB_ROOT" fetch --no-tags origin main >/dev/null

git -C "$ROOT" cat-file -e "${SCRIPT_COMMIT}^{commit}" || fail "promotion script commit unavailable: $SCRIPT_COMMIT"
git -C "$ROOT" cat-file -e "${SOURCE_COMMIT}^{commit}" || fail "backend source commit unavailable: $SOURCE_COMMIT"
git -C "$WEB_ROOT" cat-file -e "${WEB_COMMIT}^{commit}" || fail "web source commit unavailable: $WEB_COMMIT"

payload="$(git -C "$ROOT" show "${SCRIPT_COMMIT}:${SCRIPT_PATH}")"

# Keep the proven promotion/certification logic, but replace raw GitHub per-file
# downloads with reads from the authenticated local Git object databases. This
# preserves the immutable source pins while eliminating raw-content 404s.
patched="$(printf '%s\n' "$payload" | python3 -c '
import sys
s=sys.stdin.read()
source="6aaef39c524ff4d5472c432936046a73fe4bbb1f"
web="0e832f2d14e8f7cfe1d74975ac1277c8f476bfdc"
s=s.replace("BACKEND_COMMIT=\"f459ae128d0bf5e2b0d6b63dfaf476df11c4731b\"", f"BACKEND_COMMIT=\"{source}\"")
s=s.replace("WEB_COMMIT=\"0e832f2d14e8f7cfe1d74975ac1277c8f476bfdc\"", f"WEB_COMMIT=\"{web}\"")
s=s.replace(
    "  curl -fsSL \"https://raw.githubusercontent.com/$BACKEND_REPO/$BACKEND_COMMIT/$f\" -o \"$ROOT/$f\"",
    "  git -C \"$ROOT\" show \"$BACKEND_COMMIT:$f\" > \"$ROOT/$f\"",
)
s=s.replace(
    "  curl -fsSL \"https://raw.githubusercontent.com/$WEB_REPO/$WEB_COMMIT/web/$f\" -o \"$WEB_SRC/$f\"",
    "  git -C \"$(git -C \"$WEB_SRC\" rev-parse --show-toplevel)\" show \"$WEB_COMMIT:web/$f\" > \"$WEB_SRC/$f\"",
)
if "raw.githubusercontent.com/$BACKEND_REPO/$BACKEND_COMMIT/$f" in s or "raw.githubusercontent.com/$WEB_REPO/$WEB_COMMIT/web/$f" in s:
    raise SystemExit("FAIL: immutable-source raw URL replacement incomplete")
print(s, end="")
')"

printf '%s\n' "$patched" | bash
