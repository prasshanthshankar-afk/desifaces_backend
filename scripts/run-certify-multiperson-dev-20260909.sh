#!/usr/bin/env bash
set -Eeuo pipefail

echo "RETIRED: legacy Multi-Person certification could emit resolved environment secrets." >&2
echo "Use scripts/run-v3-integrity-hardening-dev-20260910.sh instead." >&2
exit 2
