#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

EXPECTED_BRANCH="feature/v3-multiperson-core-20260818"
CURRENT_BRANCH="$(git branch --show-current)"
if [ "$CURRENT_BRANCH" != "$EXPECTED_BRANCH" ]; then
  echo "MULTIPERSON_CERT_FAIL=wrong_branch:$CURRENT_BRANCH"
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "MULTIPERSON_CERT_FAIL=working_tree_not_clean"
  git status --short
  exit 1
fi

# Contract/unit proof first; schema is untouched if this fails.
docker run --rm \
  -v "$PWD:/repo" \
  -w /repo \
  desifaces-v3-svc-fusion \
  sh -lc 'pip install -q pytest && PYTHONPATH=/repo:/repo/services/shared:/repo/services/shared/python:/app python -m pytest -q test/test_v3_*.py'
echo "MULTIPERSON_STORY_UNIT_TESTS=PASS"

DB_NAME="$(docker exec desifaces-v3-db sh -lc 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select current_database()"')"
if [ "$DB_NAME" != "desifaces_v3" ]; then
  echo "MULTIPERSON_CERT_FAIL=wrong_database:$DB_NAME"
  exit 1
fi
echo "MULTIPERSON_STORY_DB_TARGET=PASS"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="$HOME/backups/desifaces-v3-multiperson-story/$STAMP"
mkdir -p "$BACKUP_DIR"
docker exec desifaces-v3-db sh -lc 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$BACKUP_DIR/desifaces_v3_pre_multiperson_story.dump"
sha256sum "$BACKUP_DIR/desifaces_v3_pre_multiperson_story.dump" > "$BACKUP_DIR/desifaces_v3_pre_multiperson_story.dump.sha256"
echo "MULTIPERSON_STORY_PRE_MIGRATION_BACKUP=PASS"

for migration in \
  migrations/2026_08_18_v3_multiperson_story_foundation.sql \
  migrations/2026_08_18_v3_multiperson_story_hardening.sql
do
  docker exec -i desifaces-v3-db sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$migration" >/dev/null
  echo "MULTIPERSON_STORY_MIGRATION_PASS=$migration"
done

# Rebuild only Fusion API because certification imports shared contracts/stores.
# Execution workers remain disabled.
COMPOSE_PARALLEL_LIMIT=1 ./scripts/v3-compose.sh up -d --no-deps --build svc-fusion
until curl -fsS 127.0.0.1:18002/api/health >/dev/null 2>&1; do sleep 2; done
echo "MULTIPERSON_STORY_V3_FUSION_HEALTH=PASS"

docker exec df-v3-svc-fusion python /repo/scripts/v3-multiperson-story-runtime-certify.py

if docker ps --format '{{.Names}}' | grep -Eq '^df-v3-.*(worker|scheduler)'; then
  echo "MULTIPERSON_CERT_FAIL=v3_execution_worker_running"
  exit 1
fi
if ! docker inspect df-v3-svc-fusion --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -q '^FUSION_RECOVERY_ENABLED=false$'; then
  echo "MULTIPERSON_CERT_FAIL=fusion_recovery_not_disabled"
  exit 1
fi
echo "MULTIPERSON_STORY_EXECUTION_GUARD=PASS"

curl -fsS 127.0.0.1:8002/api/health >/dev/null
echo "MULTIPERSON_STORY_V2_FUSION_COEXISTENCE=PASS"
diff <(curl -fsS 127.0.0.1:8002/openapi.json | jq -S '.paths') \
     <(curl -fsS 127.0.0.1:18002/openapi.json | jq -S '.paths') >/dev/null
echo "MULTIPERSON_STORY_PUBLIC_API_PARITY=PASS"

echo "MULTIPERSON_STORY_FOUNDATION_STATUS=CERTIFIED"
echo "V3_MULTIPERSON_STORY_FOUNDATION=PASS"
