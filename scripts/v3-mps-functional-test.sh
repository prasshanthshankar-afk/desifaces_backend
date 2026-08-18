#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

EXPECTED_BRANCH="feature/v3-multiperson-core-20260818"
CURRENT_BRANCH="$(git branch --show-current)"
if [ "$CURRENT_BRANCH" != "$EXPECTED_BRANCH" ]; then
  echo "MPS_FUNCTIONAL_FAIL=wrong_branch:$CURRENT_BRANCH"
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "MPS_FUNCTIONAL_FAIL=working_tree_not_clean"
  git status --short
  exit 1
fi

if [ -z "${DF_DIRECTOR_LLM_MODEL:-}" ] && [ -f infra/.env ]; then
  FILE_MODEL="$(awk -F= '$1 == "DF_DIRECTOR_LLM_MODEL" {sub(/^[^=]*=/, ""); print}' infra/.env | tail -n 1 | tr -d '\r' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
  if [ -n "$FILE_MODEL" ]; then export DF_DIRECTOR_LLM_MODEL="$FILE_MODEL"; fi
fi
export DF_DIRECTOR_LLM_MODEL="${DF_DIRECTOR_LLM_MODEL:-gpt-5.6}"

# Fast targeted regression gate before touching DB/runtime.
docker run --rm -v "$PWD:/repo" -w /repo desifaces-v3-svc-fusion \
  sh -lc 'pip install -q pytest langgraph==1.2.9 && PYTHONPATH=/repo:/repo/services/shared:/repo/services/shared/python:/repo/services/svc-director/app python -m pytest -q test/test_v3_participant_ref_normalization.py test/test_v3_studio_hitl_workflow.py'
echo "MPS_FUNCTIONAL_TARGETED_UNIT=PASS"

DB_NAME="$(docker exec desifaces-v3-db sh -lc 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select current_database()"')"
if [ "$DB_NAME" != "desifaces_v3" ]; then
  echo "MPS_FUNCTIONAL_FAIL=wrong_database:$DB_NAME"
  exit 1
fi
echo "MPS_FUNCTIONAL_DB_TARGET=PASS"

for migration in \
  migrations/2026_08_18_v3_studio_hitl_workflow.sql \
  migrations/2026_08_18_v3_studio_hitl_hardening.sql
do
  docker exec -i desifaces-v3-db sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$migration" >/dev/null
  echo "MPS_FUNCTIONAL_MIGRATION_PASS=$migration"
done
echo "MPS_FUNCTIONAL_STUDIO_HITL_SCHEMA=PASS"

COMPOSE_PARALLEL_LIMIT=1 ./scripts/v3-compose.sh --profile v3-orchestration up -d --no-deps --build svc-director svc-director-worker

HEALTH=""
for _ in $(seq 1 45); do
  if HEALTH="$(curl -fsS 127.0.0.1:18011/api/health 2>/dev/null)"; then break; fi
  sleep 2
done
if [ -z "$HEALTH" ]; then
  echo "MPS_FUNCTIONAL_FAIL=director_health_timeout"
  docker logs --tail 120 df-v3-svc-director 2>&1 | sed -E 's/(sk-[A-Za-z0-9_-]{12})[A-Za-z0-9_-]*/\1...REDACTED/g' || true
  exit 1
fi

echo "$HEALTH" | jq -e '.ok == true and .service == "svc-director" and .execution_mode == "durable_queue"' >/dev/null
if ! echo "$HEALTH" | jq -e '.llm_configured == true and .runtime_ready == true' >/dev/null; then
  echo "MPS_FUNCTIONAL_FAIL=director_live_llm_not_ready"
  echo "$HEALTH" | jq '{ok,service,execution_mode,llm_configured,embedding_configured,review_required,runtime_ready,configuration_error}'
  echo "MPS_FUNCTIONAL_HINT=ensure_OPENAI_API_KEY_is_present_in_infra_env"
  exit 1
fi
if ! echo "$HEALTH" | jq -e '.review_required == true' >/dev/null; then
  echo "MPS_FUNCTIONAL_FAIL=director_review_required_false"
  exit 1
fi
if ! docker ps --format '{{.Names}}' | grep -Fxq 'df-v3-svc-director-worker'; then
  echo "MPS_FUNCTIONAL_FAIL=director_orchestration_worker_not_running"
  docker logs --tail 120 df-v3-svc-director-worker 2>&1 || true
  exit 1
fi

echo "MPS_FUNCTIONAL_LIVE_DIRECTOR_PRECHECK=PASS"
echo "MPS_FUNCTIONAL_ASYNC_RUNNER=PASS"

set +e
docker exec df-v3-svc-director python -m app.tools.v3_mps_functional_test
FUNCTIONAL_RC=$?
set -e
if [ "$FUNCTIONAL_RC" -ne 0 ]; then
  echo "MPS_FUNCTIONAL_DIRECTOR_API_LOGS_BEGIN"
  docker logs --tail 120 df-v3-svc-director 2>&1 | sed -E 's/(sk-[A-Za-z0-9_-]{12})[A-Za-z0-9_-]*/\1...REDACTED/g'
  echo "MPS_FUNCTIONAL_DIRECTOR_API_LOGS_END"
  echo "MPS_FUNCTIONAL_DIRECTOR_RUNNER_LOGS_BEGIN"
  docker logs --tail 220 df-v3-svc-director-worker 2>&1 | sed -E 's/(sk-[A-Za-z0-9_-]{12})[A-Za-z0-9_-]*/\1...REDACTED/g'
  echo "MPS_FUNCTIONAL_DIRECTOR_RUNNER_LOGS_END"
  exit "$FUNCTIONAL_RC"
fi

UNEXPECTED_WORKERS="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-.*(worker|scheduler)$' | grep -v '^df-v3-svc-director-worker$' || true)"
if [ -n "$UNEXPECTED_WORKERS" ]; then
  echo "MPS_FUNCTIONAL_FAIL=provider_execution_worker_running:$UNEXPECTED_WORKERS"
  exit 1
fi
echo "MPS_FUNCTIONAL_PROVIDER_EXECUTION_GUARD=PASS"

curl -fsS 127.0.0.1:8002/api/health >/dev/null
echo "MPS_FUNCTIONAL_V2_FUSION_COEXISTENCE=PASS"

echo "V3_MPS_LIVE_FUNCTIONAL_TEST=PASS"
