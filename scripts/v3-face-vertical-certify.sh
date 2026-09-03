#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

EXPECTED_BRANCH="feature/v3-multiperson-core-20260818"
CURRENT_BRANCH="$(git branch --show-current)"
if [ "$CURRENT_BRANCH" != "$EXPECTED_BRANCH" ]; then
  echo "V3_FACE_VERTICAL_FAIL=wrong_branch:$CURRENT_BRANCH"
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "V3_FACE_VERTICAL_FAIL=working_tree_not_clean"
  git status --short
  exit 1
fi

echo "V3_FACE_VERTICAL_SOURCE=PASS"

# Build only API images. No Face provider worker is started by this certification.
COMPOSE_PARALLEL_LIMIT=1 ./scripts/v3-compose.sh build svc-face svc-director

echo "V3_FACE_VERTICAL_BUILD=PASS"

# svc-director and svc-face both expose a top-level Python package named `app`.
# Keep their tests in separate processes to prevent package shadowing.
docker run --rm -v "$PWD:/repo" -w /repo desifaces-v3-svc-director \
  sh -lc '
    pip install -q pytest &&
    PYTHONPATH=/repo:/repo/services/shared:/repo/services/shared/python:/repo/services/svc-director/app \
      python -m pytest -q \
        test/test_v3_participant_face_bridge.py \
        test/test_v3_participant_ref_normalization.py \
        test/test_v3_studio_hitl_workflow.py \
        test/test_v3_face_execution_policy.py &&
    PYTHONPATH=/repo:/repo/services/shared:/repo/services/shared/python:/repo/services/svc-director/app \
      python -m compileall -q \
        services/shared/df_contracts/v3 \
        services/shared/python/desifaces_shared/v3 \
        services/svc-director/app/app
  '
echo "V3_FACE_DIRECTOR_TESTS=PASS"

docker run --rm -v "$PWD:/repo" -w /repo desifaces-v3-svc-face \
  sh -lc '
    pip install -q pytest &&
    PYTHONPATH=/repo:/repo/services/shared:/repo/services/shared/python:/repo/services/svc-face/app \
      python -m pytest -q test/test_v3_face_creator_config_hydration.py &&
    PYTHONPATH=/repo:/repo/services/shared:/repo/services/shared/python:/repo/services/svc-face/app \
      python -m compileall -q \
        services/svc-face/app/app/api/routes/face_media.py \
        services/svc-face/app/app/services/azure_storage_service.py
  '
echo "V3_FACE_SERVICE_TESTS=PASS"

bash -n scripts/v3-mps2-visual-face-proof.sh
bash -n scripts/v3-face-vertical-certify.sh
echo "V3_FACE_SHELL_SYNTAX=PASS"

DB_NAME="$(docker exec desifaces-v3-db sh -lc 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select current_database()"')"
if [ "$DB_NAME" != "desifaces_v3" ]; then
  echo "V3_FACE_VERTICAL_FAIL=wrong_database:$DB_NAME"
  exit 1
fi
echo "V3_FACE_DB_TARGET=PASS"

docker exec -i desifaces-v3-db sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < migrations/2026_08_19_v3_studio_stage_attempts.sql >/dev/null

echo "V3_FACE_STAGE_ATTEMPT_MIGRATION=PASS"

SCHEMA_CHECK="$(docker exec desifaces-v3-db sh -lc 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select concat_ws('"'"':'"'"', to_regclass('"'"'public.v3_studio_stage_attempts'"'"') is not null, exists(select 1 from information_schema.columns where table_schema='"'"'public'"'"' and table_name='"'"'v3_studio_stage_attempts'"'"' and column_name='"'"'generation_id'"'"'), exists(select 1 from information_schema.columns where table_schema='"'"'public'"'"' and table_name='"'"'v3_studio_stage_attempts'"'"' and column_name='"'"'generation_job_id'"'"'));"')"
# psql text output renders PostgreSQL booleans as t/f (not true/false).
if [ "$SCHEMA_CHECK" != "t:t:t" ]; then
  echo "V3_FACE_VERTICAL_FAIL=attempt_schema:$SCHEMA_CHECK"
  exit 1
fi
echo "V3_FACE_C5_ATTEMPT_SCHEMA=PASS"

# Start API/control-plane services only. No provider generation worker.
COMPOSE_PARALLEL_LIMIT=1 ./scripts/v3-compose.sh up -d svc-core svc-pricing svc-face svc-director

for item in \
  "director:127.0.0.1:18011/api/health" \
  "face:127.0.0.1:18003/api/health" \
  "pricing:127.0.0.1:18009/api/health" \
  "core:127.0.0.1:18000/api/health"
do
  name="${item%%:*}"
  url="${item#*:}"
  ok=""
  for _ in $(seq 1 60); do
    if curl -fsS "$url" >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  if [ -z "$ok" ]; then
    echo "V3_FACE_VERTICAL_FAIL=${name}_health_timeout"
    exit 1
  fi
done
echo "V3_FACE_API_HEALTH=PASS"

# Prove storage configuration and creator masterdata are safe before any provider cost.
docker exec df-v3-svc-face python -m app.tools.v3_mps2_prepare_visual_storage
docker exec df-v3-svc-face python -m app.tools.v3_mps2_audit_creator_config_hydration
echo "V3_FACE_PREFLIGHTS=PASS"

# Production route assembly must expose the C5-aware Face API and durable MediaAsset read endpoint.
docker exec df-v3-svc-director python - <<'PY'
from app.main import app
paths={r.path for r in app.routes}
required={
  '/api/director/studio-workflows/{workflow_id}/face-stages/{stage_run_id}/pricing-preview',
  '/api/director/studio-workflows/{workflow_id}/face-stages/{stage_run_id}/dispatch',
  '/api/director/studio-workflows/{workflow_id}/face-stages/{stage_run_id}/sync',
  '/api/director/studio-reviews/{review_item_id}',
}
missing=sorted(required-paths)
if missing:
    raise SystemExit('V3_FACE_VERTICAL_FAIL=director_routes_missing:'+','.join(missing))
print('V3_FACE_DIRECTOR_ROUTES=PASS')
PY

docker exec df-v3-svc-face python - <<'PY'
from app.main import app
paths={r.path for r in app.routes}
required={'/api/face/assets/{media_asset_id}/read-url'}
missing=sorted(required-paths)
if missing:
    raise SystemExit('V3_FACE_VERTICAL_FAIL=face_routes_missing:'+','.join(missing))
print('V3_FACE_MEDIA_ROUTE=PASS')
PY

# This gate must never start paid provider execution.
if docker ps --format '{{.Names}}' | grep -Fxq 'df-v3-svc-face-worker'; then
  echo "V3_FACE_VERTICAL_FAIL=face_provider_worker_running"
  exit 1
fi
echo "V3_FACE_PROVIDER_COST_GUARD=PASS"

echo "V3_FACE_VERTICAL_ZERO_COST_CERTIFICATION=PASS"
