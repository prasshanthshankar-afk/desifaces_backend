#!/usr/bin/env bash
set -Eeuo pipefail

echo "RETIRED: this recovery path depended on mutable local Compose files." >&2
echo "Use scripts/run-v3-integrity-hardening-dev-20260910.sh instead." >&2
exit 2
