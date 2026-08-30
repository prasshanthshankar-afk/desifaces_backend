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
SERVICE_ENV_FILE="$ROOT/infra/.env"
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
# Bridge Compose's service-level ./infra/.env reference to the preserved V3
# runtime environment when source executes from an isolated Git worktree.
#
# This is a symlink only: secrets are never copied into the source tree. The
# link exists only for the lifetime of this wrapper invocation and is removed
# on normal exit or signal. An existing real worktree-local env is never
# overwritten.
###############################################################################

SERVICE_ENV_LINK_OWNED=false
cleanup_service_env_link() {
  if [[ "$SERVICE_ENV_LINK_OWNED" == "true" && -L "$SERVICE_ENV_FILE" ]]; then
    rm -f "$SERVICE_ENV_FILE"
  fi
}

if [[ "$(readlink -f "$ENV_FILE")" != "$(readlink -f "$SERVICE_ENV_FILE" 2>/dev/null || printf '%s' "$SERVICE_ENV_FILE")" ]]; then
  mkdir -p "$(dirname "$SERVICE_ENV_FILE")"
  if [[ -e "$SERVICE_ENV_FILE" || -L "$SERVICE_ENV_FILE" ]]; then
    if [[ -L "$SERVICE_ENV_FILE" && "$(readlink -f "$SERVICE_ENV_FILE")" == "$(readlink -f "$ENV_FILE")" ]]; then
      SERVICE_ENV_LINK_OWNED=true
    else
      die "refusing to replace existing worktree-local service env: $SERVICE_ENV_FILE"
    fi
  else
    ln -s "$ENV_FILE" "$SERVICE_ENV_FILE"
    SERVICE_ENV_LINK_OWNED=true
  fi
  trap cleanup_service_env_link EXIT INT TERM
fi


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
# Canonical invocation. Do not exec: the EXIT trap must remove the temporary
# service-env symlink after Compose has consumed it.
###############################################################################

docker compose \
  --env-file "$ENV_FILE" \
  -f "$BASE_FILE" \
  -f "$V3_FILE" \
  "$@"
