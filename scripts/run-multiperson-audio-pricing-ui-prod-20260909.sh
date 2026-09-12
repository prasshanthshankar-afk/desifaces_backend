#!/usr/bin/env bash
set -Eeuo pipefail

REPO="prasshanthshankar-afk/desifaces_backend"
BRANCH="release/v3-production-backend-closeout-20260904"
PROMOTION_SCRIPT="promote-certify-multiperson-audio-pricing-ui-prod-20260909.sh"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "run on desifaces-gpu; current=$(hostname -s)"

# Canonical production web workspace used by the existing desifaces web deployment
# scripts is ~/workspace/desifaces-web-review with Docker build context in web/.
# Keep a deterministic candidate list, then fall back to bounded discovery so the
# promotion does not depend on a stale workstation-specific directory assumption.
CANDIDATES=(
  "${WEB_SRC:-}"
  "$HOME/workspace/desifaces-web-review/web"
  "$HOME/workspace/desifaces_web/web"
  "$HOME/workspace/desifaces-web/web"
  "$HOME/workspace/desifaces/web"
  "$HOME/workspace/desifaces/web-app/web"
)

WEB_SOURCE=""
for candidate in "${CANDIDATES[@]}"; do
  [[ -n "$candidate" ]] || continue
  if [[ -f "$candidate/Dockerfile" && -f "$candidate/components/MultiPersonDirector.tsx" && -d "$candidate/app/app/multi-person" ]]; then
    WEB_SOURCE="$candidate"
    break
  fi
done

if [[ -z "$WEB_SOURCE" ]]; then
  while IFS= read -r component; do
    candidate="$(dirname "$(dirname "$component")")"
    if [[ -f "$candidate/Dockerfile" && -d "$candidate/app/app/multi-person" ]]; then
      WEB_SOURCE="$candidate"
      break
    fi
  done < <(find "$HOME/workspace" -maxdepth 5 -type f -path '*/components/MultiPersonDirector.tsx' 2>/dev/null | sort)
fi

[[ -n "$WEB_SOURCE" ]] || {
  echo "Searched deterministic production candidates plus $HOME/workspace (maxdepth=5)." >&2
  fail "could not locate canonical desifaces web Docker build context"
}

echo "WEB_SOURCE=$WEB_SOURCE"
export WEB_SRC="$WEB_SOURCE"

TMP="$(mktemp /tmp/desifaces-multiperson-hotfix.XXXXXX.sh)"
trap 'rm -f "$TMP"' EXIT
curl -fsSL "https://raw.githubusercontent.com/$REPO/$BRANCH/scripts/$PROMOTION_SCRIPT" -o "$TMP"
exec bash "$TMP"
