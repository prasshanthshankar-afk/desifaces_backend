#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

EXPECTED_BRANCH="feature/v3-c4-c6-foundation-closure-20260818"
CURRENT_BRANCH="$(git branch --show-current)"
if [ "$CURRENT_BRANCH" != "$EXPECTED_BRANCH" ]; then
  echo "C456_CERT_FAIL=wrong_branch:$CURRENT_BRANCH"
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "C456_CERT_FAIL=working_tree_not_clean"
  git status --short
  exit 1
fi

# Unit/contract tests run in an ephemeral container. No running service or host
# Python environment is modified.
docker run --rm \
  -v "$PWD:/repo" \
  -w /repo \
  desifaces-v3-svc-fusion \
  sh -lc 'pip install -q pytest && PYTHONPATH=/repo:/repo/services/shared:/repo/services/shared/python:/app python -m pytest -q test/test_v3_*.py'
echo "C4_C5_C6_UNIT_TESTS=PASS"

# Hard target guard: all migrations below go only to the certified V3 DB.
DB_NAME="$(docker exec desifaces-v3-db sh -lc 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select current_database()"')"
if [ "$DB_NAME" != "desifaces_v3" ]; then
  echo "C456_CERT_FAIL=wrong_database:$DB_NAME"
  exit 1
fi
echo "C4_C5_C6_DB_TARGET=PASS"

# Take an archival pre-migration V3 snapshot before additive/backfill changes.
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="$HOME/backups/desifaces-v3-c456/$STAMP"
mkdir -p "$BACKUP_DIR"
docker exec desifaces-v3-db sh -lc 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$BACKUP_DIR/desifaces_v3_pre_c456.dump"
sha256sum "$BACKUP_DIR/desifaces_v3_pre_c456.dump" > "$BACKUP_DIR/desifaces_v3_pre_c456.dump.sha256"
echo "C4_C5_C6_PRE_MIGRATION_BACKUP=PASS"

for migration in \
  migrations/2026_08_18_v3_media_lifecycle.sql \
  migrations/2026_08_18_v3_generation_persistence.sql \
  migrations/2026_08_18_v3_subscription_credit_integrity.sql
do
  docker exec -i desifaces-v3-db sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$migration" >/dev/null
  echo "MIGRATION_PASS=$migration"
done

# Rebuild only the APIs needed for certification. Execution profiles remain off.
COMPOSE_PARALLEL_LIMIT=1 ./scripts/v3-compose.sh up -d --no-deps --build svc-fusion svc-pricing
until curl -fsS 127.0.0.1:18002/api/health >/dev/null 2>&1; do sleep 2; done
until curl -fsS 127.0.0.1:18009/api/health >/dev/null 2>&1; do sleep 2; done
echo "C4_C5_C6_V3_API_HEALTH=PASS"

# C4/C5 synthetic write/read/state tests execute inside a transaction and roll back.
docker exec df-v3-svc-fusion python /repo/scripts/v3-c45-runtime-certify.py

# C6 synthetic renewal + duplicate callback + top-up-preservation tests also roll back.
docker exec df-v3-svc-pricing python -m app.tools.v3_c6_certify

# Development safety gates remain binding.
if ! docker inspect df-v3-svc-pricing --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -q '^DF_SUBSCRIPTION_RECONCILER_ENABLED=false$'; then
  echo "C456_CERT_FAIL=subscription_reconciler_not_disabled"
  exit 1
fi
if docker ps --format '{{.Names}}' | grep -Eq '^df-v3-.*(worker|scheduler)'; then
  echo "C456_CERT_FAIL=v3_execution_worker_running"
  exit 1
fi
echo "C4_C5_C6_EXECUTION_GUARD=PASS"

# Only the two touched V2 capabilities are checked; no global C2C rerun.
curl -fsS 127.0.0.1:8002/api/health >/dev/null
curl -fsS 127.0.0.1:8009/api/health >/dev/null
echo "C4_C5_C6_V2_COEXISTENCE=PASS"

# Public API contracts must remain unchanged by these internal foundations.
diff <(curl -fsS 127.0.0.1:8002/openapi.json | jq -S '.paths') <(curl -fsS 127.0.0.1:18002/openapi.json | jq -S '.paths') >/dev/null
diff <(curl -fsS 127.0.0.1:8009/openapi.json | jq -S '.paths') <(curl -fsS 127.0.0.1:18009/openapi.json | jq -S '.paths') >/dev/null
echo "C4_C5_C6_PUBLIC_API_PARITY=PASS"

echo "C4_STATUS=CERTIFIED"
echo "C5_STATUS=CERTIFIED"
echo "C6_STATUS=CERTIFIED"
echo "V3_C4_C5_C6_FOUNDATION_CERTIFICATION=PASS"
