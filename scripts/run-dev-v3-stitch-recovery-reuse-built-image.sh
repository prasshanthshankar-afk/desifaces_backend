#!/usr/bin/env bash
set -Eeuo pipefail

# DEV-only continuation for the 2026-09-10 stitch recovery.
# Reuses the immutable image that already built successfully at CORE_PIN, while
# preserving all canonical scene/pricing/lineage/cutover/final-certification gates.
# It changes only three runtime mechanics in an untracked copy of the core script:
#   1) psql SQL is fed on stdin so -v variables expand correctly;
#   2) the already-built immutable image is required/reused instead of rebuilt;
#   3) the pre-cutover import-only probe receives non-secret placeholder settings.

LIVE_ROOT="${LIVE_ROOT:-/home/azureuser/workspace/desifaces-v3}"
RECOVERY_ROOT="${RECOVERY_ROOT:-/home/azureuser/workspace/desifaces-v3-stitch-recovery-20260910}"
CORE_PIN="${DF_CORE_PIN:-89888b691045d8cf79bba428b8e00eacd5ea058c}"
WORKFLOW_ID="${1:-16099052-15b5-401f-a447-c5d989b7b8ad}"
STAGE_RUN_ID="${2:-cbf4b76a-21ec-4b17-951a-e0674a6f247f}"
STITCH_CONTAINER="${STITCH_WORKER_CONTAINER:-df-v3-svc-fusion-extension-stitch-worker}"
EXPECTED_IMAGE="desifaces-v3-stitch-worker-ready:${CORE_PIN:0:12}"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "$1=PASS"; }

[[ "$(hostname -s)" == "desifaces-dev" ]] || fail "DEV-only runner refuses host $(hostname -s)"
[[ -d "$LIVE_ROOT/.git" ]] || fail "active V3 workspace missing"
[[ -f "$LIVE_ROOT/infra/.env" ]] || fail "active V3 env missing"
docker inspect "$STITCH_CONTAINER" >/dev/null 2>&1 || fail "DEV stitch worker missing"
pass "DEV_HOST_IDENTITY"

# Keep the active DEV checkout untouched. The isolated worktree is the only source
# checkout this runner may move.
git -C "$LIVE_ROOT" cat-file -e "${CORE_PIN}^{commit}" 2>/dev/null \
  || git -C "$LIVE_ROOT" fetch origin fix/stitch-worker-readiness-gate-20260910
git -C "$LIVE_ROOT" cat-file -e "${CORE_PIN}^{commit}" 2>/dev/null \
  || fail "core recovery pin unavailable: $CORE_PIN"
[[ -e "$RECOVERY_ROOT/.git" ]] || fail "isolated recovery worktree missing"
[[ -z "$(git -C "$RECOVERY_ROOT" status --porcelain --untracked-files=no)" ]] \
  || fail "isolated recovery worktree has tracked modifications"
git -C "$RECOVERY_ROOT" checkout --detach "$CORE_PIN" >/dev/null
[[ "$(git -C "$RECOVERY_ROOT" rev-parse HEAD)" == "$CORE_PIN" ]] \
  || fail "isolated recovery pin mismatch"
pass "ISOLATED_CERTIFIED_WORKTREE"

# Compose requires this path, while .dockerignore prevents the secret from entering
# Docker build contexts. No build is performed by this continuation anyway.
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

LIVE_CFG="$(mktemp /tmp/df-v3-live-stitch.XXXXXX.json)"
CERT_CFG="$(mktemp /tmp/df-v3-cert-stitch.XXXXXX.json)"
PATCHED="$RECOVERY_ROOT/scripts/.complete-dev-v3-stitch-recovery-reuse-image.sh"
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
with open(live_path, encoding="utf-8") as f:
    live = json.load(f)["services"][name]
with open(cert_path, encoding="utf-8") as f:
    cert = json.load(f)["services"][name]
ignored = {"image", "build"}
def norm(v, root):
    if isinstance(v, dict):
        return {k: norm(x, root) for k, x in v.items() if k not in ignored}
    if isinstance(v, list):
        return [norm(x, root) for x in v]
    if isinstance(v, str):
        return v.replace(root, "<WORKTREE_ROOT>")
    return v
if norm(live, live_root) != norm(cert, cert_root):
    raise SystemExit("FAIL: stitch-worker runtime Compose definition changed since certification")
print("STITCH_WORKER_RUNTIME_COMPOSE_EQUIVALENCE=PASS")
PY
pass "ACTIVE_COMPOSE_CHANGES_SAFELY_ACCOUNTED_FOR"

# The prior run completed this exact immutable build. Require it by exact tag before
# allowing the continuation; never fall back to building or to another tag.
docker image inspect "$EXPECTED_IMAGE" >/dev/null 2>&1 \
  || fail "previously built immutable stitch image missing: $EXPECTED_IMAGE"
echo "reused_image=$EXPECTED_IMAGE"
pass "STITCH_WORKER_IMAGE_REUSE"

CORE="$RECOVERY_ROOT/scripts/complete-dev-v3-stitch-recovery.sh"
[[ -f "$CORE" ]] || fail "canonical recovery script missing"

python3 - "$CORE" "$PATCHED" <<'PY'
from pathlib import Path
import sys
src = Path(sys.argv[1]).read_text()
out = Path(sys.argv[2])

old_psql = '''psql_scalar() {
  local sql="$1"
  docker exec -i "$DB_CONTAINER" \\
    psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" \\
      -At -F '|' \\
      -v workflow_id="$WORKFLOW_ID" \\
      -v stage_run_id="$STAGE_RUN_ID" \\
      -c "$sql"
}'''
new_psql = '''psql_scalar() {
  local sql="$1"
  printf '%s\\n' "$sql" | docker exec -i "$DB_CONTAINER" \\
    psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" \\
      -At -F '|' \\
      -v workflow_id="$WORKFLOW_ID" \\
      -v stage_run_id="$STAGE_RUN_ID"
}'''

old_build = '''docker build \\
  -f services/svc-fusion-extension/app/Dockerfile \\
  -t "$NEW_IMAGE" \\
  .
pass "STITCH_WORKER_IMAGE_BUILD"'''
new_build = '''docker image inspect "$NEW_IMAGE" >/dev/null 2>&1 \\
  || fail "previously built immutable stitch image missing: $NEW_IMAGE"
pass "STITCH_WORKER_IMAGE_REUSE"'''

old_probe = '''docker run --rm -i --entrypoint python "$NEW_IMAGE" - <<'PY'\n'''
new_probe = '''docker run --rm -i \\
  -e DATABASE_URL='postgresql://probe:probe@127.0.0.1:1/probe' \\
  -e JWT_SECRET='import-probe-only-not-runtime' \\
  -e AZURE_STORAGE_CONNECTION_STRING='DefaultEndpointsProtocol=https;AccountName=probe;AccountKey=cHJvYmU=;EndpointSuffix=core.windows.net' \\
  --entrypoint python "$NEW_IMAGE" - <<'PY'\n'''

for label, old in (("psql", old_psql), ("build", old_build), ("probe", old_probe)):
    count = src.count(old)
    if count != 1:
        raise SystemExit(f"FAIL: expected exactly one {label} patch target; found {count}")

src = src.replace(old_psql, new_psql, 1)
src = src.replace(old_build, new_build, 1)
src = src.replace(old_probe, new_probe, 1)
out.write_text(src)
PY

chmod 700 "$PATCHED"
bash -n "$PATCHED"
grep -Fq "printf '%s\\n' \"\$sql\" | docker exec -i" "$PATCHED" \
  || fail "psql stdin transport fix missing"
grep -Fq 'STITCH_WORKER_IMAGE_REUSE' "$PATCHED" \
  || fail "built-image reuse gate missing"
grep -Fq "JWT_SECRET='import-probe-only-not-runtime'" "$PATCHED" \
  || fail "non-secret import probe settings missing"
pass "RUNTIME_CONTINUATION_PATCH"

cd "$RECOVERY_ROOT"
bash "$PATCHED" "$WORKFLOW_ID" "$STAGE_RUN_ID"
pass "REUSED_IMAGE_V3_STITCH_RECOVERY"
