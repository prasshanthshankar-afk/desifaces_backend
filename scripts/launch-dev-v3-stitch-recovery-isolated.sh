#!/usr/bin/env bash
set -Eeuo pipefail

# DEV-only launcher for the certified stitch recovery when the active DEV checkout
# contains legitimate uncommitted work. The active checkout is never checked out,
# reset, stashed, cleaned, or modified by this launcher.

LIVE_ROOT="${LIVE_ROOT:-/home/azureuser/workspace/desifaces-v3}"
RECOVERY_ROOT="${RECOVERY_ROOT:-/home/azureuser/workspace/desifaces-v3-stitch-recovery-20260910}"
CORE_BRANCH="${CORE_BRANCH:-fix/stitch-worker-readiness-gate-20260910}"
CORE_PIN="${CORE_PIN:-08d711fc1c17193a49de477ef77991f006116036}"
WORKFLOW_ID="${1:-16099052-15b5-401f-a447-c5d989b7b8ad}"
STAGE_RUN_ID="${2:-cbf4b76a-21ec-4b17-951a-e0674a6f247f}"
STITCH_CONTAINER="${STITCH_WORKER_CONTAINER:-df-v3-svc-fusion-extension-stitch-worker}"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

pass() {
  echo "$1=PASS"
}

[[ "$(hostname -s)" == "desifaces-dev" ]] \
  || fail "DEV-only isolated launcher refuses host $(hostname -s)"
pass "DEV_HOST_IDENTITY"

[[ -d "$LIVE_ROOT/.git" ]] || fail "active V3 workspace not found: $LIVE_ROOT"
[[ -f "$LIVE_ROOT/infra/.env" ]] || fail "active V3 environment file missing: $LIVE_ROOT/infra/.env"
docker inspect "$STITCH_CONTAINER" >/dev/null 2>&1 \
  || fail "existing DEV stitch worker not found: $STITCH_CONTAINER"

# Record the active workspace state for evidence only. Do not mutate it.
echo "active_workspace=$LIVE_ROOT"
echo "active_workspace_head=$(git -C "$LIVE_ROOT" rev-parse HEAD)"
echo "active_workspace_tracked_changes_begin"
git -C "$LIVE_ROOT" status --short --untracked-files=all || true
echo "active_workspace_tracked_changes_end"
pass "ACTIVE_WORKSPACE_PRESERVED_UNTOUCHED"

# Ensure the certified recovery commit is locally available without changing HEAD.
if ! git -C "$LIVE_ROOT" cat-file -e "${CORE_PIN}^{commit}" 2>/dev/null; then
  git -C "$LIVE_ROOT" fetch origin "$CORE_BRANCH"
fi
git -C "$LIVE_ROOT" cat-file -e "${CORE_PIN}^{commit}" 2>/dev/null \
  || fail "certified recovery commit is unavailable: $CORE_PIN"
pass "CERTIFIED_RECOVERY_COMMIT_AVAILABLE"

# Build/recovery source lives in a separate clean Git worktree. Never remove an
# existing directory unless Git already recognizes it as this exact worktree.
if [[ -e "$RECOVERY_ROOT/.git" ]]; then
  actual="$(git -C "$RECOVERY_ROOT" rev-parse HEAD 2>/dev/null || true)"
  [[ "$actual" == "$CORE_PIN" ]] \
    || fail "existing recovery worktree is at unexpected commit: $actual"
  [[ -z "$(git -C "$RECOVERY_ROOT" status --porcelain --untracked-files=no)" ]] \
    || fail "existing recovery worktree has tracked modifications"
else
  [[ ! -e "$RECOVERY_ROOT" ]] \
    || fail "recovery path exists but is not a Git worktree: $RECOVERY_ROOT"
  git -C "$LIVE_ROOT" worktree add --detach "$RECOVERY_ROOT" "$CORE_PIN"
fi

[[ "$(git -C "$RECOVERY_ROOT" rev-parse HEAD)" == "$CORE_PIN" ]] \
  || fail "isolated worktree source pin mismatch"
[[ -z "$(git -C "$RECOVERY_ROOT" status --porcelain --untracked-files=no)" ]] \
  || fail "isolated worktree is not clean"
pass "ISOLATED_CERTIFIED_WORKTREE"

# Copy only the current DEV V3 environment identity into the isolated worktree.
# This is not tracked and therefore does not alter the certified source tree.
install -m 600 "$LIVE_ROOT/infra/.env" "$RECOVERY_ROOT/infra/.env"
pass "V3_ENVIRONMENT_HANDOFF"

# Force Compose in the isolated checkout to manage the exact same running DEV
# project. This avoids creating a second project merely because the path differs.
COMPOSE_PROJECT_NAME="$(
  docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' \
    "$STITCH_CONTAINER" 2>/dev/null || true
)"
[[ -n "$COMPOSE_PROJECT_NAME" ]] || fail "cannot resolve live Compose project name"
export COMPOSE_PROJECT_NAME
echo "compose_project=$COMPOSE_PROJECT_NAME"
pass "SAME_COMPOSE_PROJECT_TARGET"

# The active workspace has local Compose edits. Do not assume they are irrelevant.
# Render both definitions and prove the stitch-worker runtime contract is equivalent
# before the certified worktree is allowed to recreate that one container.
LIVE_CFG="$(mktemp /tmp/df-v3-live-stitch-compose.XXXXXX.json)"
CERT_CFG="$(mktemp /tmp/df-v3-cert-stitch-compose.XXXXXX.json)"
cleanup_cfg() {
  rm -f "$LIVE_CFG" "$CERT_CFG"
}
trap cleanup_cfg EXIT

(
  cd "$LIVE_ROOT"
  bash scripts/v3-compose.sh config --format json
) >"$LIVE_CFG"

(
  cd "$RECOVERY_ROOT"
  bash scripts/v3-compose.sh config --format json
) >"$CERT_CFG"

python3 - "$LIVE_CFG" "$CERT_CFG" "$LIVE_ROOT" "$RECOVERY_ROOT" <<'PY'
import json
import sys

live_path, cert_path, live_root, cert_root = sys.argv[1:]
service_name = "svc-fusion-extension-stitch-worker"

with open(live_path, encoding="utf-8") as f:
    live = json.load(f)
with open(cert_path, encoding="utf-8") as f:
    cert = json.load(f)

try:
    a = live["services"][service_name]
    b = cert["services"][service_name]
except KeyError as exc:
    raise SystemExit(f"FAIL: stitch-worker Compose service missing: {exc}")

# image is intentionally replaced by the recovery override; build path is the
# isolated certified source. All other runtime-facing fields must be equivalent.
ignored = {"image", "build"}

def normalize(value, root):
    if isinstance(value, dict):
        return {
            k: normalize(v, root)
            for k, v in value.items()
            if k not in ignored
        }
    if isinstance(value, list):
        return [normalize(v, root) for v in value]
    if isinstance(value, str):
        return value.replace(root, "<WORKTREE_ROOT>")
    return value

na = normalize(a, live_root)
nb = normalize(b, cert_root)

if na != nb:
    keys = sorted(set(na) | set(nb))
    changed = [k for k in keys if na.get(k) != nb.get(k)]
    print(
        "FAIL: active and certified stitch-worker runtime Compose definitions differ; "
        "changed_keys=" + ",".join(changed),
        file=sys.stderr,
    )
    raise SystemExit(1)

print("STITCH_WORKER_RUNTIME_COMPOSE_EQUIVALENCE=PASS")
PY

pass "ACTIVE_COMPOSE_CHANGES_SAFELY_ACCOUNTED_FOR"

# Run the already-certified recovery logic from the clean worktree. Its own gates
# still verify failed scene state, released pricing, exactly nine preserved children,
# immutable worker build, deterministic readiness, canonical Director retry, zero
# child dispatch/charges, unchanged provider ids, and final canonical media.
cd "$RECOVERY_ROOT"

echo "isolated_recovery_root=$RECOVERY_ROOT"
echo "certified_source_ref=$(git rev-parse HEAD)"
echo "workflow_id=$WORKFLOW_ID"
echo "stage_run_id=$STAGE_RUN_ID"

echo "===== CERTIFIED CORE RECOVERY ====="
bash scripts/complete-dev-v3-stitch-recovery.sh "$WORKFLOW_ID" "$STAGE_RUN_ID"

echo "ISOLATED_V3_STITCH_RECOVERY=PASS"
