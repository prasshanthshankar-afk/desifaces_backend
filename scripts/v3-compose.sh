#!/usr/bin/env bash
set -euo pipefail

ROOT="$(
  cd "$(dirname "${BASH_SOURCE[0]}")/.." &&
  pwd
)"

# Source code may execute from an isolated Git worktree while secrets/runtime
# identity remain in the established live V3 workspace. Callers can bind that
# preserved runtime environment explicitly without copying secrets into Git.
ENV_FILE="${V3_ENV_FILE:-$ROOT/infra/.env}"
BASE_FILE="$ROOT/docker-compose.yml"
V3_FILE="$ROOT/docker-compose.v3.yml"

die() {
  echo "v3-compose: ERROR: $*" >&2
  exit 1
}

[ -f "$ENV_FILE" ] || die "missing V3 runtime env: $ENV_FILE"
[ -f "$BASE_FILE" ] || die "missing $BASE_FILE"
[ -f "$V3_FILE" ] || die "missing $V3_FILE"


###############################################################################
# Validate V3 environment identity before Compose sees anything.
###############################################################################

python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
from urllib.parse import urlparse
import sys

path = Path(sys.argv[1])

env = {}

for raw in path.read_text().splitlines():
    line = raw.strip()

    if not line or line.startswith("#") or "=" not in line:
        continue

    k, v = line.split("=", 1)
    env[k.strip()] = v.strip()

required = (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "DATABASE_URL",
    "REDIS_URL",
)

missing = [k for k in required if not env.get(k)]

if missing:
    raise SystemExit(
        "v3-compose: missing required V3 variables: "
        + ", ".join(missing)
    )

if env["POSTGRES_DB"] != "desifaces_v3":
    raise SystemExit(
        "v3-compose: refusing non-V3 POSTGRES_DB"
    )

if env["POSTGRES_USER"] != "desifaces_v3_admin":
    raise SystemExit(
        "v3-compose: refusing non-V3 POSTGRES_USER"
    )

db = urlparse(env["DATABASE_URL"])

if (
    db.hostname != "desifaces-db"
    or db.port != 5432
    or db.path.lstrip("/") != "desifaces_v3"
    or db.username != "desifaces_v3_admin"
    or not db.password
):
    raise SystemExit(
        "v3-compose: DATABASE_URL does not identify V3"
    )

redis = urlparse(env["REDIS_URL"])

if (
    redis.hostname != "desifaces-redis"
    or redis.port != 6379
    or redis.path.lstrip("/") != "0"
):
    raise SystemExit(
        "v3-compose: REDIS_URL does not identify V3"
    )
PY


###############################################################################
# Disallow the two most dangerous destructive forms.
###############################################################################

ARGS=" $* "

if [[ "$ARGS" == *" down "* && "$ARGS" == *" -v "* ]]; then
  die "'down -v' is prohibited for the V3 environment"
fi

if [[ "$ARGS" == *" down "* && "$ARGS" == *" --volumes "* ]]; then
  die "'down --volumes' is prohibited for the V3 environment"
fi


###############################################################################
# Canonical invocation.
###############################################################################

exec docker compose \
  --env-file "$ENV_FILE" \
  -f "$BASE_FILE" \
  -f "$V3_FILE" \
  "$@"
