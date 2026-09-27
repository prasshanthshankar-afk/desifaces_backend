#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="/home/azureuser/workspace/desifaces-v3/infra/.env"
CANONICAL="desifaces-redis"
LEGACY="desifaces-v3-redis"
NETWORK="df-v3-net"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LEGACY_SAVED="desifaces-redis-legacy-${STAMP}"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-dev" ]] || fail "DEV host required"
[[ -f "$ENV_FILE" ]] || fail "canonical env missing"

REDIS_HOST="$(
  grep '^REDIS_URL=' "$ENV_FILE" \
  | sed -E 's#^REDIS_URL=redis://([^/@]+@)?([^/:]+).*#\2#'
)"
[[ "$REDIS_HOST" == "$CANONICAL" ]] || fail "REDIS_URL host is $REDIS_HOST, expected $CANONICAL"

for c in "$CANONICAL" "$LEGACY"; do
  docker inspect "$c" >/dev/null 2>&1 || fail "missing Redis container: $c"
  [[ "$(docker inspect -f '{{.State.Status}}' "$c")" == "running" ]] || fail "$c is not running"
  [[ "$(docker exec "$c" redis-cli DBSIZE)" == "0" ]] || fail "$c is not empty; refusing consolidation"
done

docker network inspect "$NETWORK" >/dev/null 2>&1 || fail "missing network: $NETWORK"

echo "REDIS_AUTHORITY=PASS canonical=$CANONICAL both_instances_empty=true"

docker update --restart=no "$LEGACY" >/dev/null
docker stop "$LEGACY" >/dev/null
docker rename "$LEGACY" "$LEGACY_SAVED"

docker network connect --alias "$CANONICAL" "$NETWORK" "$CANONICAL" 2>/dev/null || true

docker inspect "$CANONICAL" -f '{{json .NetworkSettings.Networks}}' | grep -q '"df-v3-net"' \
  || fail "canonical Redis is not attached to $NETWORK"

[[ "$(docker exec "$CANONICAL" redis-cli PING)" == "PONG" ]] \
  || fail "canonical Redis did not respond"

mapfile -t RUNNING_REDIS < <(
  docker ps --format '{{.Names}}' | grep -E '^desifaces(-v3)?-redis$' || true
)
(( ${#RUNNING_REDIS[@]} == 1 )) || fail "expected one running Redis, found: ${RUNNING_REDIS[*]:-none}"
[[ "${RUNNING_REDIS[0]}" == "$CANONICAL" ]] || fail "unexpected active Redis: ${RUNNING_REDIS[0]}"

echo "REDIS_RUNTIME_CONSOLIDATION=PASS"
echo "running_redis=$CANONICAL"
echo "legacy_redis=$LEGACY_SAVED"
echo "data_copy=NONE"
echo "env_change=NONE"
echo "production_touch=NONE"
