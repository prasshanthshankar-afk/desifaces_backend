#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${RUNTIME_ENV_FILE:-$ROOT/infra/.env}"
BASE_FILE="$ROOT/docker-compose.yml"
OVERRIDE_FILE="$ROOT/docker-compose.v3.yml"

die(){ echo "runtime-compose: ERROR: $*" >&2; exit 1; }

[[ -f "$ENV_FILE" ]] || die "missing runtime env: $ENV_FILE"
[[ -f "$BASE_FILE" ]] || die "missing $BASE_FILE"
[[ -f "$OVERRIDE_FILE" ]] || die "missing $OVERRIDE_FILE"

if [[ " $* " == *" down "* && ( " $* " == *" -v "* || " $* " == *" --volumes "* ) ]]; then
  die "volume-destructive shutdown is prohibited"
fi

docker network inspect df-net >/dev/null 2>&1 || docker network create df-net >/dev/null

docker compose   --env-file "$ENV_FILE"   -f "$BASE_FILE"   -f "$OVERRIDE_FILE"   "$@"
