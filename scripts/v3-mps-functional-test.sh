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

# Use the currently configured Director model when present. For this functional
# gate only, default to the current OpenAI general-purpose model if the project
# has not yet frozen a different Director model in infra/.env.
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
  docker logs --tail 80 df-v3-svc-director 2>&1 | sed -E 's/(sk-[A-Za-z0-9_-]{12})[A-Za-z0-9_-]*/\1...REDACTED/g' || true
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

docker exec df-v3-svc-director python -m app.tools.v3_mps_functional_test

if docker ps --format '{{.Names}}' | grep -Eq '^df-v3-.*(worker|scheduler)'; then
  echo "MPS_FUNCTIONAL_FAIL=v3_execution_worker_running"
  exit 1
fi
echo "MPS_FUNCTIONAL_EXECUTION_GUARD=PASS"

curl -fsS 127.0.0.1:8002/api/health >/dev/null
echo "MPS_FUNCTIONAL_V2_FUSION_COEXISTENCE=PASS"

echo "V3_MPS_LIVE_FUNCTIONAL_TEST=PASS"
