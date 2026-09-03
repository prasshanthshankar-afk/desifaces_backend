#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

echo "===== V3 MULTI-PERSON STATIC GATES ====="
PYTHONPYCACHEPREFIX=/tmp/desifaces-v3-pycache python3 -m py_compile \
  services/svc-director/app/app/audio_execution.py \
  services/svc-director/app/app/fusion_execution.py \
  services/svc-director/app/app/studio_progression.py \
  services/svc-director/app/app/studio_e2e_routes.py \
  services/svc-director/app/app/studio_routes_runtime.py \
  services/svc-audio/app/app/api/routes/v3_audio_output.py \
  services/svc-fusion/app/app/services/v3_pricing_bridge.py \
  services/svc-fusion-extension/app/app/api/routes/v3_scene_stitch.py

git diff --check

echo "===== BUILD ONLY AFFECTED V3 APIs ====="
./scripts/v3-compose.sh build svc-director svc-audio svc-fusion svc-fusion-extension
./scripts/v3-compose.sh up -d svc-director svc-audio svc-fusion svc-fusion-extension

# Execution workers are explicit in V3. Starting an already-running worker is
# idempotent and does not restart unrelated services.
./scripts/v3-compose.sh --profile v3-execution up -d svc-face-worker svc-audio-worker svc-fusion-worker

echo "===== WAIT FOR OPENAPI ====="
for spec in \
  "director:18011:/openapi.json" \
  "audio:18004:/openapi.json" \
  "fusion:18002:/openapi.json" \
  "fusion-extension:18006:/openapi.json"; do
  IFS=: read -r name port path <<<"$spec"
  ok=0
  for _ in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:${port}${path}" >/tmp/"${name}"-openapi.json 2>/dev/null; then
      ok=1
      break
    fi
    sleep 2
  done
  if [ "$ok" -ne 1 ]; then
    echo "ERROR: ${name} OpenAPI did not become ready" >&2
    exit 1
  fi
  echo "${name}=READY"
done

echo "===== CONTRACT ROUTES ====="
jq -e '.paths["/api/director/studio-workflows/{workflow_id}/audio-stages/{stage_run_id}/pricing-preview"]' /tmp/director-openapi.json >/dev/null
jq -e '.paths["/api/director/studio-workflows/{workflow_id}/audio-stages/{stage_run_id}/dispatch"]' /tmp/director-openapi.json >/dev/null
jq -e '.paths["/api/director/studio-workflows/{workflow_id}/fusion-stages/{stage_run_id}/pricing-preview"]' /tmp/director-openapi.json >/dev/null
jq -e '.paths["/api/director/studio-workflows/{workflow_id}/fusion-stages/{stage_run_id}/dispatch"]' /tmp/director-openapi.json >/dev/null
jq -e '.paths["/api/director/studio-workflows/{workflow_id}/advance"]' /tmp/director-openapi.json >/dev/null
jq -e '.paths["/api/audio/jobs/{job_id}/canonical-output"]' /tmp/audio-openapi.json >/dev/null
jq -e '.paths["/api/audio/assets/{media_id}/read-url"]' /tmp/audio-openapi.json >/dev/null
jq -e '.paths["/jobs/pricing/preview"]' /tmp/fusion-openapi.json >/dev/null
jq -e '.paths["/jobs"]' /tmp/fusion-openapi.json >/dev/null
jq -e '.paths["/api/longform/v3/scene-stitch"]' /tmp/fusion-extension-openapi.json >/dev/null

echo "ROUTES=PASS"

echo "===== FUSION CONFIRMED-QUOTE BRIDGE ====="
docker exec -i df-v3-svc-fusion python - <<'PY'
from app.services.v3_pricing_bridge import ConfirmedFusionJobCreate
fields = getattr(ConfirmedFusionJobCreate, "model_fields", {})
assert "pricing_confirmation" in fields
print("FUSION_PRICING_CONFIRMATION=PASS")
PY

echo "===== V3 MULTI-PERSON DEPLOYMENT READY ====="
./scripts/v3-compose.sh ps svc-director svc-audio svc-fusion svc-fusion-extension
