#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE="$ROOT/scripts/v3-compose.sh"
[[ -x "$COMPOSE" ]] || { echo "V3_RUNTIME_UP=FAIL reason=missing_v3_compose"; exit 2; }

# Canonical launch contract:
# - base APIs/infrastructure
# - provider/background execution workers
# - Director/orchestration worker
# Do not use plain `v3-compose.sh up -d` for a complete launch runtime.
"$COMPOSE" \
  --profile v3-execution \
  --profile v3-orchestration \
  up -d

required=(
  desifaces-v3-db
  desifaces-v3-redis
  df-v3-svc-core
  df-v3-svc-face
  df-v3-svc-face-worker
  df-v3-svc-audio
  df-v3-svc-audio-worker
  df-v3-svc-fusion
  df-v3-svc-fusion-worker
  df-v3-svc-fusion-extension
  df-v3-svc-fusion-extension-worker
  df-v3-svc-fusion-extension-stitch-worker
  df-v3-svc-pricing
  df-v3-svc-director
  df-v3-svc-director-worker
)

missing=()
for c in "${required[@]}"; do
  if ! docker ps --format '{{.Names}}' | grep -qx "$c"; then
    missing+=("$c")
  fi
done

if ((${#missing[@]})); then
  echo "V3_RUNTIME_UP=FAIL"
  printf 'MISSING_REQUIRED_CONTAINERS=%s\n' "${missing[*]}"
  exit 3
fi

echo "V3_RUNTIME_UP=PASS"
echo "V3_EXECUTION_PROFILE=PASS"
echo "V3_ORCHESTRATION_PROFILE=PASS"
echo "FUSION_EXTENSION_API=PASS"
echo "FUSION_EXTENSION_WORKER=PASS"
echo "FUSION_EXTENSION_STITCH_WORKER=PASS"
echo "FUSION_WORKER=PASS"
echo "PRICING_SERVICE=PASS"
echo "DIRECTOR_WORKER=PASS"
