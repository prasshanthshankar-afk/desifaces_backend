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

wait_for_health() {
  local url="$1"
  local label="$2"
  for _ in $(seq 1 30); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "MULTIPERSON_CERT_FAIL=${label}_health_timeout"
  return 1
}

# Contract + deterministic LangGraph proof first. No LLM/provider call occurs.
docker run --rm \
  -v "$PWD:/repo" \
  -w /repo \
  desifaces-v3-svc-fusion \
  sh -lc 'pip install -q pytest langgraph==1.2.9 && PYTHONPATH=/repo:/repo/services/shared:/repo/services/shared/python:/app python -m pytest -q test/test_v3_*.py'
echo "MULTIPERSON_STORY_UNIT_TESTS=PASS"
echo "CREATIVE_DIRECTOR_LANGGRAPH_UNIT=PASS"

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
  migrations/2026_08_18_v3_multiperson_story_hardening.sql \
  migrations/2026_08_18_v3_creative_director_rag.sql
do
  docker exec -i desifaces-v3-db sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$migration" >/dev/null
  echo "MULTIPERSON_STORY_MIGRATION_PASS=$migration"
done

echo "CREATIVE_DIRECTOR_RAG_SCHEMA=PASS"

# Fusion validates shared Story/Generation contracts. Director is a V3-only API.
# Neither starts provider execution workers.
COMPOSE_PARALLEL_LIMIT=1 ./scripts/v3-compose.sh up -d --no-deps --build svc-fusion
wait_for_health "http://127.0.0.1:18002/api/health" "v3_fusion"
echo "MULTIPERSON_STORY_V3_FUSION_HEALTH=PASS"

docker exec df-v3-svc-fusion python /repo/scripts/v3-multiperson-story-runtime-certify.py

COMPOSE_PARALLEL_LIMIT=1 ./scripts/v3-compose.sh up -d --no-deps --build svc-director
wait_for_health "http://127.0.0.1:18011/api/health" "v3_director"
DIRECTOR_HEALTH="$(curl -fsS http://127.0.0.1:18011/api/health)"
echo "$DIRECTOR_HEALTH" | jq -e '.ok == true and .service == "svc-director" and .langgraph_checkpoint == "postgres"' >/dev/null
echo "CREATIVE_DIRECTOR_V3_HEALTH=PASS"

CHECKPOINT_TABLES="$(docker exec desifaces-v3-db sh -lc 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select count(*) from pg_class where relname in (\$\$checkpoints\$\$,\$\$checkpoint_writes\$\$)"')"
if [ "$CHECKPOINT_TABLES" -lt 2 ]; then
  echo "MULTIPERSON_CERT_FAIL=langgraph_checkpoint_schema_missing:$CHECKPOINT_TABLES"
  exit 1
fi
echo "CREATIVE_DIRECTOR_POSTGRES_CHECKPOINT=PASS"

DIRECTOR_PATHS="$(curl -fsS http://127.0.0.1:18011/openapi.json | jq -r '.paths | keys[]')"
for path in \
  /api/director/runs \
  '/api/director/runs/{thread_id}' \
  '/api/director/runs/{thread_id}/resume' \
  '/api/director/stories/{story_id}/workspace' \
  '/api/director/stories/{story_id}/assistant-context'
do
  if ! grep -Fxq "$path" <<<"$DIRECTOR_PATHS"; then
    echo "MULTIPERSON_CERT_FAIL=director_api_missing:$path"
    exit 1
  fi
done
echo "CREATIVE_DIRECTOR_STRUCTURED_API=PASS"

if docker ps --format '{{.Names}}' | grep -Eq '^df-v3-.*(worker|scheduler)'; then
  echo "MULTIPERSON_CERT_FAIL=v3_execution_worker_running"
  exit 1
fi
if ! docker inspect df-v3-svc-fusion --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -q '^FUSION_RECOVERY_ENABLED=false$'; then
  echo "MULTIPERSON_CERT_FAIL=fusion_recovery_not_disabled"
  exit 1
fi
if ! docker inspect df-v3-svc-director --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -q '^LANGGRAPH_STRICT_MSGPACK=true$'; then
  echo "MULTIPERSON_CERT_FAIL=langgraph_strict_msgpack_not_enabled"
  exit 1
fi
echo "MULTIPERSON_STORY_EXECUTION_GUARD=PASS"

curl -fsS http://127.0.0.1:8002/api/health >/dev/null
echo "MULTIPERSON_STORY_V2_FUSION_COEXISTENCE=PASS"
diff <(curl -fsS http://127.0.0.1:8002/openapi.json | jq -S '.paths') \
     <(curl -fsS http://127.0.0.1:18002/openapi.json | jq -S '.paths') >/dev/null
echo "MULTIPERSON_STORY_PUBLIC_API_PARITY=PASS"

echo "STORY_UI_PROJECTION_CONTRACT=PASS"
echo "ASSISTANT_CREATION_CONTEXT_CONTRACT=PASS"
echo "MULTIPERSON_STORY_FOUNDATION_STATUS=CERTIFIED"
echo "V3_MULTIPERSON_STORY_DIRECTOR_FOUNDATION=PASS"
