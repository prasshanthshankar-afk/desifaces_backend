#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/docker-compose.yml}"
ENV_FILE="${RUNTIME_ENV_FILE:-$ROOT/infra/.env}"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ -f "$COMPOSE_FILE" ]] || fail "missing compose file: $COMPOSE_FILE"
[[ -f "$ENV_FILE" ]] || fail "missing runtime env: $ENV_FILE"

echo "============================================================"
echo " desifaces — CANONICAL COMPOSE CONTRACT"
echo "============================================================"
echo "compose=$COMPOSE_FILE"
echo "project_root=$ROOT"

docker compose   --project-directory "$ROOT"   --env-file "$ENV_FILE"   -f "$COMPOSE_FILE"   --profile "*"   config -q

echo "COMPOSE_PARSE=PASS"

SERVICES="$(
  docker compose     --project-directory "$ROOT"     --env-file "$ENV_FILE"     -f "$COMPOSE_FILE"     --profile "*"     config --services
)"

if grep -Eiq '(^|[-_.])v[0-9]+($|[-_.])|v3' <<<"$SERVICES"; then
  echo "$SERVICES"
  fail "version-specific service name found"
fi

echo "SERVICE_NAMES_VERSION_NEUTRAL=PASS"

RENDERED="$(
  docker compose     --project-directory "$ROOT"     --env-file "$ENV_FILE"     -f "$COMPOSE_FILE"     --profile "*"     config
)"

BAD_CONTAINERS="$(
  awk '/container_name:/ {print $2}' <<<"$RENDERED" | grep -i 'v3' || true
)"
[[ -z "$BAD_CONTAINERS" ]] || {
  echo "$BAD_CONTAINERS"
  fail "version-specific container name found"
}
echo "CONTAINER_NAMES_VERSION_NEUTRAL=PASS"

NETWORK_NAME="$(
  awk '
    /^networks:/ {in_networks=1; next}
    in_networks && /^[^[:space:]]/ {in_networks=0}
    in_networks && /^[[:space:]]+name:[[:space:]]+/ {print $2; exit}
  ' <<<"$RENDERED"
)"
[[ "$NETWORK_NAME" == "df-net" ]] || fail "canonical network is not df-net: ${NETWORK_NAME:-missing}"
echo "CANONICAL_NETWORK=PASS"

[[ "$SERVICES" == *"svc-director"* ]] || fail "svc-director missing"
[[ "$SERVICES" == *"svc-director-worker"* ]] || fail "svc-director-worker missing"
[[ "$SERVICES" == *"svc-assistant"* ]] || fail "svc-assistant missing"
echo "CORE_ORCHESTRATION_SERVICES=PASS"

if grep -q 'docker-compose\.v3\.yml\|docker-compose\.assistant\.v3\.yml' "$COMPOSE_FILE"; then
  fail "version-specific compose reference remains"
fi
echo "SINGLE_COMPOSE_REFERENCE=PASS"

echo "============================================================"
echo " CANONICAL_COMPOSE_CONTRACT=PASS"
echo "============================================================"
