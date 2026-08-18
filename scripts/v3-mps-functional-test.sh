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

# Respect an already-frozen Director model from the shell or V3 env file.
# Only the functional test defaults to gpt-5.6 when no project choice exists.
if [ -z "${DF_DIRECTOR_LLM_MODEL:-}" ] && [ -f infra/.env ]; then
  FILE_MODEL="$(awk -F= '$1 == "DF_DIRECTOR_LLM_MODEL" {sub(/^[^=]*=/, ""); print}' infra/.env | tail -n 1 | tr -d '\r' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
  if [ -n "$FILE_MODEL" ]; then
    export DF_DIRECTOR_LLM_MODEL="$FILE_MODEL"
  fi
fi
export DF_DIRECTOR_LLM_MODEL="${DF_DIRECTOR_LLM_MODEL:-gpt-5.6}"

COMPOSE_PARALLEL_LIMIT=1 ./scripts/v3-compose.sh up -d --no-deps --build svc-director

HEALTH=""
for _ in $(seq 1 45); do
  if HEALTH="$(curl -fsS 127.0.0.1:18011/api/health 2>/dev/null)"; then
    break
  fi
  sleep 2
done
if [ -z "$HEALTH" ]; then
  echo "MPS_FUNCTIONAL_FAIL=director_health_timeout"
  docker logs --tail 120 df-v3-svc-director 2>&1 | sed -E 's/(sk-[A-Za-z0-9_-]{12})[A-Za-z0-9_-]*/\1...REDACTED/g' || true
  exit 1
fi

echo "$HEALTH" | jq -e '.ok == true and .service == "svc-director"' >/dev/null
if ! echo "$HEALTH" | jq -e '.llm_configured == true and .runtime_ready == true' >/dev/null; then
  echo "MPS_FUNCTIONAL_FAIL=director_live_llm_not_ready"
  echo "$HEALTH" | jq '{ok,service,llm_configured,embedding_configured,review_required,runtime_ready,configuration_error}'
  echo "MPS_FUNCTIONAL_HINT=ensure_OPENAI_API_KEY_is_present_in_infra_env"
  exit 1
fi
if ! echo "$HEALTH" | jq -e '.review_required == true' >/dev/null; then
  echo "MPS_FUNCTIONAL_FAIL=director_review_required_false"
  exit 1
fi

echo "MPS_FUNCTIONAL_LIVE_DIRECTOR_PRECHECK=PASS"

set +e
docker exec df-v3-svc-director python -m app.tools.v3_mps_functional_test
FUNCTIONAL_RC=$?
set -e
if [ "$FUNCTIONAL_RC" -ne 0 ]; then
  echo "MPS_FUNCTIONAL_DIRECTOR_LOGS_BEGIN"
  docker logs --tail 180 df-v3-svc-director 2>&1 \
    | sed -E 's/(sk-[A-Za-z0-9_-]{12})[A-Za-z0-9_-]*/\1...REDACTED/g'
  echo "MPS_FUNCTIONAL_DIRECTOR_LOGS_END"
  exit "$FUNCTIONAL_RC"
fi

if docker ps --format '{{.Names}}' | grep -Eq '^df-v3-.*(worker|scheduler)'; then
  echo "MPS_FUNCTIONAL_FAIL=v3_execution_worker_running"
  exit 1
fi
echo "MPS_FUNCTIONAL_EXECUTION_GUARD=PASS"

curl -fsS 127.0.0.1:8002/api/health >/dev/null
echo "MPS_FUNCTIONAL_V2_FUSION_COEXISTENCE=PASS"

echo "V3_MPS_LIVE_FUNCTIONAL_TEST=PASS"
