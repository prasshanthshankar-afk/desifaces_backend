#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

EXPECTED_BRANCH="feature/v3-c4-c6-foundation-closure-20260818"
CURRENT_BRANCH="$(git branch --show-current)"
if [ "$CURRENT_BRANCH" != "$EXPECTED_BRANCH" ]; then
  echo "C6_CERT_FAIL=wrong_branch:$CURRENT_BRANCH"
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "C6_CERT_FAIL=working_tree_not_clean"
  git status --short
  exit 1
fi

DB_NAME="$(docker exec desifaces-v3-db sh -lc 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select current_database()"')"
if [ "$DB_NAME" != "desifaces_v3" ]; then
  echo "C6_CERT_FAIL=wrong_database:$DB_NAME"
  exit 1
fi
echo "C6_DB_TARGET=PASS"

# C4/C5 and all C6 migrations were already applied by the prior closure run.
# Rebuild only Pricing so this resume uses the corrected certifier/integrity code.
COMPOSE_PARALLEL_LIMIT=1 ./scripts/v3-compose.sh up -d --no-deps --build svc-pricing
until curl -fsS 127.0.0.1:18009/api/health >/dev/null 2>&1; do sleep 2; done
echo "C6_V3_PRICING_HEALTH=PASS"

# Synthetic cycle tests and the active-period integrity sweep run inside a DB
# transaction and roll back all certification mutations.
docker exec df-v3-svc-pricing python -m app.tools.v3_c6_certify

# Development safety remains binding during certification.
if ! docker inspect df-v3-svc-pricing --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -q '^DF_SUBSCRIPTION_RECONCILER_ENABLED=false$'; then
  echo "C6_CERT_FAIL=subscription_reconciler_not_disabled"
  exit 1
fi
if docker ps --format '{{.Names}}' | grep -Eq '^df-v3-.*(worker|scheduler)'; then
  echo "C6_CERT_FAIL=v3_execution_worker_running"
  exit 1
fi
echo "C6_EXECUTION_GUARD=PASS"

# V2 pricing remains available and the public contract is unchanged.
curl -fsS 127.0.0.1:8009/api/health >/dev/null
echo "C6_V2_PRICING_COEXISTENCE=PASS"
diff <(curl -fsS 127.0.0.1:8009/openapi.json | jq -S '.paths') <(curl -fsS 127.0.0.1:18009/openapi.json | jq -S '.paths') >/dev/null
echo "C6_PUBLIC_API_PARITY=PASS"

echo "C6_STATUS=CERTIFIED"
echo "V3_C6_RESUME_CERTIFICATION=PASS"
