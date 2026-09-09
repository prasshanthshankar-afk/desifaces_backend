#!/usr/bin/env bash
set -Eeuo pipefail

URL="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/release/v3-production-backend-closeout-20260904/scripts/run-multiperson-aspect-face-readurl-prod-20260909.py"

command -v curl >/dev/null 2>&1 || { echo "FAIL: curl is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "FAIL: python3 is required" >&2; exit 1; }

curl -fsSL "$URL" | python3
