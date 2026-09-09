#!/usr/bin/env bash
set -Eeuo pipefail

REPO="prasshanthshankar-afk/desifaces_backend"
SCRIPT_COMMIT="6aaef39c524ff4d5472c432936046a73fe4bbb1f"
SOURCE_COMMIT="6aaef39c524ff4d5472c432936046a73fe4bbb1f"
SCRIPT_PATH="scripts/promote-certify-multiperson-aspect-face-readurl-prod-20260909.sh"

payload="$(curl -fsSL "https://raw.githubusercontent.com/${REPO}/${SCRIPT_COMMIT}/${SCRIPT_PATH}")"

# The promotion script was packaged one commit before the final workflow-level
# aspect consistency guard. Replace only its immutable source pin; all deployment,
# rollback and certification logic remains byte-for-byte from SCRIPT_COMMIT.
printf '%s\n' "$payload" \
  | sed "s/^BACKEND_COMMIT=.*/BACKEND_COMMIT=\"${SOURCE_COMMIT}\"/" \
  | bash
