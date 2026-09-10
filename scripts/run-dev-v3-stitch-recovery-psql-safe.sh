#!/usr/bin/env bash
set -Eeuo pipefail

# Narrow DEV-only runtime shim for the stitch recovery SQL transport defect.
# The canonical recovery script uses psql -c with psql variables; on this runtime
# the :'name' tokens reach PostgreSQL literally. This shim creates an untracked
# copy of that exact script and changes ONLY psql_scalar() to feed SQL on stdin,
# where psql performs -v variable interpolation before sending SQL to the server.

LIVE_ROOT="${LIVE_ROOT:-/home/azureuser/workspace/desifaces-v3}"
RECOVERY_ROOT="${RECOVERY_ROOT:-/home/azureuser/workspace/desifaces-v3-stitch-recovery-20260910}"
RECOVERY_PIN="${DF_RECOVERY_PIN:?DF_RECOVERY_PIN is required}"
WORKFLOW_ID="${1:-16099052-15b5-401f-a447-c5d989b7b8ad}"
STAGE_RUN_ID="${2:-cbf4b76a-21ec-4b17-951a-e0674a6f247f}"
STITCH_CONTAINER="${STITCH_WORKER_CONTAINER:-df-v3-svc-fusion-extension-stitch-worker}"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "$1=PASS"; }

[[ "$(hostname -s)" == "desifaces-dev" ]] || fail "DEV-only runner refuses host $(hostname -s)"
[[ -d "$LIVE_ROOT/.git" ]] || fail "active V3 workspace missing"
[[ -f "$LIVE_ROOT/infra/.env" ]] || fail "active V3 env missing"
docker inspect "$STITCH_CONTAINER" >/dev/null 2>&1 || fail "DEV stitch worker missing"
pass "DEV_HOST_IDENTITY"

# Never mutate the active checkout. Advance only the isolated clean worktree.
git -C "$LIVE_ROOT" cat-file -e "${RECOVERY_PIN}^{commit}" 2>/dev/null \
  || git -C "$LIVE_ROOT" fetch origin fix/stitch-worker-readiness-gate-20260910
git -C "$LIVE_ROOT" cat-file -e "${RECOVERY_PIN}^{commit}" 2>/dev/null \
  || fail "recovery pin unavailable: $RECOVERY_PIN"

[[ -e "$RECOVERY_ROOT/.git" ]] || fail "isolated recovery worktree missing"
[[ -z "$(git -C "$RECOVERY_ROOT" status --porcelain --untracked-files=no)" ]] \
  || fail "isolated recovery worktree has tracked modifications"
git -C "$RECOVERY_ROOT" checkout --detach "$RECOVERY_PIN"
[[ "$(git -C "$RECOVERY_ROOT" rev-parse HEAD)" == "$RECOVERY_PIN" ]] \
  || fail "isolated recovery pin mismatch"
pass "ISOLATED_CERTIFIED_WORKTREE"

# Compose's base env_file requires infra/.env to exist. The committed .dockerignore
# must exclude it before we bridge the DEV env into the isolated worktree.
[[ -f "$RECOVERY_ROOT/.dockerignore" ]] || fail "certified .dockerignore missing"
grep -Fxq 'infra/.env' "$RECOVERY_ROOT/.dockerignore" \
  || fail "infra/.env is not excluded from Docker build context"
install -m 600 "$LIVE_ROOT/infra/.env" "$RECOVERY_ROOT/infra/.env"
export DF_V3_ENV_FILE="$LIVE_ROOT/infra/.env"
pass "V3_ENVIRONMENT_DOCKER_EXCLUSION"

COMPOSE_PROJECT_NAME="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$STITCH_CONTAINER")"
[[ -n "$COMPOSE_PROJECT_NAME" ]] || fail "cannot resolve live Compose project"
export COMPOSE_PROJECT_NAME
pass "SAME_COMPOSE_PROJECT_TARGET"

# Reconfirm the stitch worker definition against the active workspace using the
# execution profile. This is fast and prevents local Compose drift from being ignored.
LIVE_CFG="$(mktemp /tmp/df-v3-live-stitch.XXXXXX.json)"
CERT_CFG="$(mktemp /tmp/df-v3-cert-stitch.XXXXXX.json)"
PATCHED="$RECOVERY_ROOT/scripts/.complete-dev-v3-stitch-recovery-psql-safe.sh"
cleanup() {
  rm -f "$LIVE_CFG" "$CERT_CFG" "$PATCHED" "$RECOVERY_ROOT/infra/.env"
}
trap cleanup EXIT

(
  cd "$LIVE_ROOT"
  bash scripts/v3-compose.sh --profile v3-execution config --format json
) >"$LIVE_CFG"
(
  cd "$RECOVERY_ROOT"
  bash scripts/v3-compose.sh --profile v3-execution config --format json
) >"$CERT_CFG"

python3 - "$LIVE_CFG" "$CERT_CFG" "$LIVE_ROOT" "$RECOVERY_ROOT" <<'PY'
import json, sys
live_path, cert_path, live_root, cert_root = sys.argv[1:]
name = "svc-fusion-extension-stitch-worker"
with open(live_path, encoding="utf-8") as f: live = json.load(f)["services"][name]
with open(cert_path, encoding="utf-8") as f: cert = json.load(f)["services"][name]
ignored = {"image", "build"}
def norm(v, root):
    if isinstance(v, dict):
        return {k: norm(x, root) for k, x in v.items() if k not in ignored}
    if isinstance(v, list): return [norm(x, root) for x in v]
    if isinstance(v, str): return v.replace(root, "<WORKTREE_ROOT>")
    return v
if norm(live, live_root) != norm(cert, cert_root):
    raise SystemExit("FAIL: stitch-worker runtime Compose definition changed since certified comparison")
print("STITCH_WORKER_RUNTIME_COMPOSE_EQUIVALENCE=PASS")
PY
pass "ACTIVE_COMPOSE_CHANGES_SAFELY_ACCOUNTED_FOR"

CORE="$RECOVERY_ROOT/scripts/complete-dev-v3-stitch-recovery.sh"
[[ -f "$CORE" ]] || fail "canonical recovery script missing"

python3 - "$CORE" "$PATCHED" <<'PY'
from pathlib import Path
import sys
src = Path(sys.argv[1]).read_text()
out = Path(sys.argv[2])
old = '''psql_scalar() {
  local sql="$1"
  docker exec -i "$DB_CONTAINER" \\
    psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" \\
      -At -F '|' \\
      -v workflow_id="$WORKFLOW_ID" \\
      -v stage_run_id="$STAGE_RUN_ID" \\
      -c "$sql"
}'''
new = '''psql_scalar() {
  local sql="$1"
  printf '%s\\n' "$sql" | docker exec -i "$DB_CONTAINER" \\
    psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" \\
      -At -F '|' \\
      -v workflow_id="$WORKFLOW_ID" \\
      -v stage_run_id="$STAGE_RUN_ID"
}'''
count = src.count(old)
if count != 1:
    raise SystemExit(f"FAIL: expected exactly one canonical psql_scalar block; found {count}")
out.write_text(src.replace(old, new, 1))
PY
chmod 700 "$PATCHED"
bash -n "$PATCHED"
grep -Fq "printf '%s\\n' \"\$sql\" | docker exec -i" "$PATCHED" \
  || fail "stdin SQL transport patch not installed"
if grep -Fq -- '-c "$sql"' "$PATCHED"; then
  fail "unsafe psql -c SQL transport still present"
fi
pass "PSQL_VARIABLE_TRANSPORT_FIX"

cd "$RECOVERY_ROOT"
bash "$PATCHED" "$WORKFLOW_ID" "$STAGE_RUN_ID"
pass "PSQL_SAFE_V3_STITCH_RECOVERY"
