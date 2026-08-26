#!/usr/bin/env bash
set -euo pipefail

ROOT="$(
  cd "$(dirname "${BASH_SOURCE[0]}")/.." &&
  pwd
)"

ENV_FILE="$ROOT/infra/.env"
BASE_FILE="$ROOT/docker-compose.yml"
V3_FILE="$ROOT/docker-compose.v3.yml"

die() {
  echo "v3-compose: ERROR: $*" >&2
  exit 1
}

[ -f "$ENV_FILE" ] || die "missing $ENV_FILE"
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
# Pin V3 Fusion final media to one explicit storage-container contract.
#
# The legacy env file can carry AZURE_FINAL_VIDEO_CONTAINER or
# AZURE_VIDEO_OUTPUT_CONTAINER values from other environments. V3 must not inherit
# those implicitly: provider artifacts and stitched scene output share the V3
# canonical video container unless an explicit V3-only override is supplied.
###############################################################################

DF_V3_FUSION_OUTPUT_CONTAINER="${DF_V3_FUSION_OUTPUT_CONTAINER:-video-output}"

# Azure Blob container naming contract:
# - 3..63 characters
# - lowercase letters, numbers, and hyphens only
# - starts and ends with an alphanumeric character
# - no consecutive hyphens
#
# Bash [[ =~ ]] uses POSIX ERE; do not use PCRE constructs such as (?:...).
container_len=${#DF_V3_FUSION_OUTPUT_CONTAINER}
if (( container_len < 3 || container_len > 63 )) \
  || [[ ! "$DF_V3_FUSION_OUTPUT_CONTAINER" =~ ^[a-z0-9][a-z0-9-]*[a-z0-9]$ ]] \
  || [[ "$DF_V3_FUSION_OUTPUT_CONTAINER" == *--* ]]; then
  die "invalid DF_V3_FUSION_OUTPUT_CONTAINER: $DF_V3_FUSION_OUTPUT_CONTAINER"
fi

export DF_V3_FUSION_OUTPUT_CONTAINER
export AZURE_FINAL_VIDEO_CONTAINER="$DF_V3_FUSION_OUTPUT_CONTAINER"
export AZURE_VIDEO_OUTPUT_CONTAINER="$DF_V3_FUSION_OUTPUT_CONTAINER"


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
