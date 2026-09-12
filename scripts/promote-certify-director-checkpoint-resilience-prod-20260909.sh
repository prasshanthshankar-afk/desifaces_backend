#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-gpu"
WORKSPACE="${WORKSPACE:-/home/azureuser/workspace/desifaces}"
BASE_COMPOSE="${BASE_COMPOSE:-$WORKSPACE/docker-compose.yml}"
PROD_COMPOSE="${PROD_COMPOSE:-$WORKSPACE/deploy/production/docker-compose.v3-app.production.yml}"
ENV_FILE="${ENV_FILE:-$WORKSPACE/infra/.env}"
PROJECT="${PROJECT:-desifaces}"
DIRECTOR_C="${DIRECTOR_C:-df-v3-svc-director}"
WORKER_C="${WORKER_C:-df-v3-svc-director-worker}"
DB_C="${DB_C:-desifaces-db}"
REDIS_C="${REDIS_C:-desifaces-redis}"
SOURCE_COMMIT="6e65492a82b10ea861058162ee9c3d946bb733a0"
THREAD_ID="${THREAD_ID:-d2ea84db-b395-49ea-84f8-3d32c1c0cb29}"
REPO="prasshanthshankar-afk/desifaces_backend"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname)" == "$EXPECTED_HOST" ]] || fail "run on $EXPECTED_HOST, current=$(hostname)"
for x in curl docker python3; do command -v "$x" >/dev/null 2>&1 || fail "missing command: $x"; done
[[ -d "$WORKSPACE" ]] || fail "workspace missing: $WORKSPACE"
[[ -f "$BASE_COMPOSE" ]] || fail "base compose missing: $BASE_COMPOSE"
[[ -f "$PROD_COMPOSE" ]] || fail "production compose missing: $PROD_COMPOSE"
[[ -f "$ENV_FILE" ]] || fail "env missing: $ENV_FILE"
docker inspect "$DIRECTOR_C" >/dev/null 2>&1 || fail "Director container missing: $DIRECTOR_C"
docker inspect "$WORKER_C" >/dev/null 2>&1 || fail "Director worker missing: $WORKER_C"
docker inspect "$DB_C" >/dev/null 2>&1 || fail "DB container missing: $DB_C"
docker inspect "$REDIS_C" >/dev/null 2>&1 || fail "Redis container missing: $REDIS_C"

COMPOSE=(docker compose --project-directory "$WORKSPACE" -p "$PROJECT" --env-file "$ENV_FILE" -f "$BASE_COMPOSE" -f "$PROD_COMPOSE")

DB_ID_BEFORE="$(docker inspect "$DB_C" --format '{{.Id}}')"
DB_STARTED_BEFORE="$(docker inspect "$DB_C" --format '{{.State.StartedAt}}')"
REDIS_ID_BEFORE="$(docker inspect "$REDIS_C" --format '{{.Id}}')"
REDIS_STARTED_BEFORE="$(docker inspect "$REDIS_C" --format '{{.State.StartedAt}}')"
WORKER_ID_BEFORE="$(docker inspect "$WORKER_C" --format '{{.Id}}')"
WORKER_STARTED_BEFORE="$(docker inspect "$WORKER_C" --format '{{.State.StartedAt}}')"
WORKER_RESTART_BEFORE="$(docker inspect "$WORKER_C" --format '{{.RestartCount}}')"
DIRECTOR_IMAGE_BEFORE="$(docker inspect "$DIRECTOR_C" --format '{{.Image}}')"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/home/azureuser/backups/director-checkpoint-resilience-$TS"
mkdir -p "$BACKUP"

SOURCE_PATH="services/svc-director/app/app/main.py"
DEST="$WORKSPACE/$SOURCE_PATH"
cp -a "$DEST" "$BACKUP/main.py.before"

cat <<EOF
============================================================
 desifaces PROD — DIRECTOR CHECKPOINT RESILIENCE HOTFIX
============================================================
host=$(hostname)
workspace=$WORKSPACE
source_commit=$SOURCE_COMMIT
scope=svc-director-api-only
worker_restart=FORBIDDEN
db_change=NONE
redis_change=NONE
existing_thread=$THREAD_ID
backup=$BACKUP
EOF

printf '\n===== 1. SYNC IMMUTABLE GIT SOURCE =====\n'
TMP="${DEST}.promote-${TS}.tmp"
curl -fsSL "https://raw.githubusercontent.com/$REPO/$SOURCE_COMMIT/$SOURCE_PATH" -o "$TMP"
[[ -s "$TMP" ]] || fail "fetched Director source is empty"
mv "$TMP" "$DEST"
echo "SOURCE_SYNC=PASS"

printf '\n===== 2. STATIC CONTRACT GATE =====\n'
python3 - "$DEST" <<'PY'
import ast
import pathlib
import sys
p = pathlib.Path(sys.argv[1])
s = p.read_text()
ast.parse(s)
required = (
    'from psycopg import Error as PsycopgError',
    '_TRANSIENT_CHECKPOINT_SQLSTATES',
    '"57P01"',
    'sqlstate.startswith("08")',
    'async def _aget_state_resilient',
    'snapshot = await _aget_state_resilient(graph, config)',
    'creative_director_state_temporarily_unavailable',
)
for marker in required:
    assert marker in s, marker
assert s.count('return await graph.aget_state(config)') == 2
print('STATIC_CONTRACT=PASS')
PY

printf '\n===== 3. COMPOSE INTERPOLATION =====\n'
cd "$WORKSPACE"
"${COMPOSE[@]}" config svc-director >/dev/null
echo "COMPOSE_INTERPOLATION=PASS"

printf '\n===== 4. BUILD DIRECTOR API IMAGE =====\n'
"${COMPOSE[@]}" build svc-director

printf '\n===== 5. PRE-DEPLOY RETRY BEHAVIOR PROOF =====\n'
"${COMPOSE[@]}" run --rm --no-deps -T --entrypoint python svc-director - <<'PY'
import asyncio
from psycopg.errors import AdminShutdown, UndefinedTable
from app.main import _aget_state_resilient

class RecoveringGraph:
    def __init__(self): self.calls = 0
    async def aget_state(self, config):
        self.calls += 1
        if self.calls == 1:
            raise AdminShutdown('forced transient checkpoint termination')
        return {'recovered': True}

class NonTransientGraph:
    def __init__(self): self.calls = 0
    async def aget_state(self, config):
        self.calls += 1
        raise UndefinedTable('forced non-transient checkpoint error')

async def main():
    recovering = RecoveringGraph()
    result = await _aget_state_resilient(recovering, {'configurable': {'thread_id': 'proof'}})
    assert result == {'recovered': True}
    assert recovering.calls == 2

    non_transient = NonTransientGraph()
    try:
        await _aget_state_resilient(non_transient, {'configurable': {'thread_id': 'proof'}})
    except UndefinedTable:
        pass
    else:
        raise AssertionError('non-transient error was masked')
    assert non_transient.calls == 1

    print('TRANSIENT_RETRY_BEHAVIOR=PASS')
    print('NON_TRANSIENT_FAIL_CLOSED=PASS')

asyncio.run(main())
PY

printf '\n===== 6. RECREATE DIRECTOR API ONLY =====\n'
"${COMPOSE[@]}" up -d --no-deps --force-recreate svc-director

printf '\n===== 7. WAIT FOR DIRECTOR API =====\n'
READY=0
for i in $(seq 1 30); do
  code="$(curl -sS -o /tmp/director-health.json -w '%{http_code}' http://127.0.0.1:18011/api/health || true)"
  echo "wait=$i http=$code"
  if [[ "$code" == "200" ]]; then READY=1; break; fi
  sleep 2
done
[[ "$READY" == "1" ]] || fail "Director API did not become ready"
python3 - <<'PY'
import json
p='/tmp/director-health.json'
data=json.load(open(p))
assert data.get('ok') is True, data
assert data.get('runtime_ready') is True, data
print('DIRECTOR_HEALTH=PASS')
PY

printf '\n===== 8. ACTIVE WORKSPACE PROVENANCE =====\n'
ACTIVE_DIR="$(docker inspect "$DIRECTOR_C" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
CONFIG_FILES="$(docker inspect "$DIRECTOR_C" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')"
echo "compose_working_dir=$ACTIVE_DIR"
echo "compose_config_files=$CONFIG_FILES"
[[ "$ACTIVE_DIR" == "$WORKSPACE" ]] || fail "Director points to wrong workspace: $ACTIVE_DIR"
case "$CONFIG_FILES" in
  *"$BASE_COMPOSE"*"$PROD_COMPOSE"*) ;;
  *) fail "Director compose provenance is not active production configuration" ;;
esac
echo "ACTIVE_WORKSPACE=PASS"

printf '\n===== 9. EXISTING COMPLETED RUN DURABILITY =====\n'
docker exec -i "$DIRECTOR_C" env THREAD_ID="$THREAD_ID" python - <<'PY'
import asyncio
import os
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from app.db import close_pools, open_business_pool, open_checkpoint_pool

thread_id = os.environ['THREAD_ID']

async def main():
    business = await open_business_pool()
    row = await business.fetchrow(
        "select state, story_id is not null as has_story from public.v3_director_runs where thread_id=$1",
        thread_id,
    )
    assert row is not None, 'existing Director run missing'
    state = str(row['state'])
    assert state == 'awaiting_review', state

    checkpoint_pool = await open_checkpoint_pool()
    saver = AsyncPostgresSaver(checkpoint_pool)
    saved = await saver.aget_tuple({'configurable': {'thread_id': thread_id}})
    assert saved is not None, 'existing Director checkpoint missing'

    print(f'EXISTING_RUN_STATE={state}')
    print('EXISTING_RUN_CHECKPOINT=PASS')
    await close_pools()

asyncio.run(main())
PY

printf '\n===== 10. NON-TARGET SERVICES UNCHANGED =====\n'
[[ "$DB_ID_BEFORE" == "$(docker inspect "$DB_C" --format '{{.Id}}')" ]] || fail "DB identity changed"
[[ "$DB_STARTED_BEFORE" == "$(docker inspect "$DB_C" --format '{{.State.StartedAt}}')" ]] || fail "DB restarted"
[[ "$REDIS_ID_BEFORE" == "$(docker inspect "$REDIS_C" --format '{{.Id}}')" ]] || fail "Redis identity changed"
[[ "$REDIS_STARTED_BEFORE" == "$(docker inspect "$REDIS_C" --format '{{.State.StartedAt}}')" ]] || fail "Redis restarted"
[[ "$WORKER_ID_BEFORE" == "$(docker inspect "$WORKER_C" --format '{{.Id}}')" ]] || fail "Director worker was recreated"
[[ "$WORKER_STARTED_BEFORE" == "$(docker inspect "$WORKER_C" --format '{{.State.StartedAt}}')" ]] || fail "Director worker restarted"
[[ "$WORKER_RESTART_BEFORE" == "$(docker inspect "$WORKER_C" --format '{{.RestartCount}}')" ]] || fail "Director worker restart count changed during promotion"
echo "DB_REDIS_UNCHANGED=PASS"
echo "DIRECTOR_WORKER_UNCHANGED=PASS"

echo "DIRECTOR_IMAGE_BEFORE=$DIRECTOR_IMAGE_BEFORE"
echo "DIRECTOR_IMAGE_AFTER=$(docker inspect "$DIRECTOR_C" --format '{{.Image}}')"

echo
cat <<'EOF'
============================================================
 PROD DIRECTOR CHECKPOINT RESILIENCE PASS
============================================================
SOURCE_SYNC=PASS
STATIC_CONTRACT=PASS
COMPOSE_INTERPOLATION=PASS
TRANSIENT_RETRY_BEHAVIOR=PASS
NON_TRANSIENT_FAIL_CLOSED=PASS
DIRECTOR_HEALTH=PASS
ACTIVE_WORKSPACE=PASS
EXISTING_RUN_STATE=awaiting_review
EXISTING_RUN_CHECKPOINT=PASS
DB_REDIS_UNCHANGED=PASS
DIRECTOR_WORKER_UNCHANGED=PASS
NEXT=REFRESH_EXISTING_MULTI_PERSON_RUN_DO_NOT_CREATE_NEW_RUN
EOF
