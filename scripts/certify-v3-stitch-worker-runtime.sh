#!/usr/bin/env bash
set -Eeuo pipefail

# Deterministic readiness certification for the V3 Fusion Extension stitch worker.
#
# This intentionally does NOT use a log line as a readiness signal. INFO logging
# is useful observability, but runtime acceptance must be based on executable
# invariants: the intended immutable image/command, coordinator enablement,
# artifact-refresh installation, database reachability, and process stability.

CONTAINER="${STITCH_WORKER_CONTAINER:-df-v3-svc-fusion-extension-stitch-worker}"
EXPECTED_IMAGE="${EXPECTED_IMAGE:-}"
MAX_RESTART_COUNT="${MAX_RESTART_COUNT:-0}"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

pass() {
  echo "$1=PASS"
}

command -v docker >/dev/null 2>&1 || fail "docker is not available"

docker inspect "$CONTAINER" >/dev/null 2>&1 \
  || fail "stitch worker container not found: $CONTAINER"

running_before="$(docker inspect --format '{{.State.Running}}' "$CONTAINER")"
[[ "$running_before" == "true" ]] \
  || fail "stitch worker is not running: $CONTAINER"
pass "STITCH_WORKER_PROCESS_RUNNING"

restart_before="$(docker inspect --format '{{.RestartCount}}' "$CONTAINER")"
[[ "$restart_before" =~ ^[0-9]+$ ]] \
  || fail "invalid restart count: $restart_before"
(( restart_before <= MAX_RESTART_COUNT )) \
  || fail "stitch worker restart count is $restart_before (allowed <= $MAX_RESTART_COUNT)"
pass "STITCH_WORKER_RESTART_COUNT"

configured_cmd="$(docker inspect --format '{{json .Config.Cmd}}' "$CONTAINER")"
[[ "$configured_cmd" == *"app.workers.stitch_worker"* ]] \
  || fail "container command is not the V3 stitch worker: $configured_cmd"
pass "STITCH_WORKER_COMMAND"

coordinator_env="$(
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" \
    | awk -F= '$1=="DF_V3_SCENE_COORDINATOR_ENABLED" {print tolower($2); exit}'
)"
case "$coordinator_env" in
  1|true|yes|on) ;;
  *) fail "DF_V3_SCENE_COORDINATOR_ENABLED is not enabled (value=${coordinator_env:-missing})" ;;
esac
pass "V3_SCENE_COORDINATOR_ENV"

if [[ -n "$EXPECTED_IMAGE" ]]; then
  expected_image_id="$(docker image inspect --format '{{.Id}}' "$EXPECTED_IMAGE" 2>/dev/null)" \
    || fail "expected image is not present locally: $EXPECTED_IMAGE"
  actual_image_id="$(docker inspect --format '{{.Image}}' "$CONTAINER")"
  [[ "$actual_image_id" == "$expected_image_id" ]] \
    || fail "worker image mismatch: actual=$actual_image_id expected=$expected_image_id"
  pass "STITCH_WORKER_IMMUTABLE_IMAGE"
fi

# Probe in the exact running container filesystem/environment. Importing
# stitch_worker must install the artifact-refresh finalizer binding. The DB probe
# proves the same configuration can acquire a pool and execute a query.
docker exec -i "$CONTAINER" python - <<'PY'
from __future__ import annotations

import asyncio

from app.db import get_db_pool
from app.workers import stitch_worker
from app.workers import v3_scene_artifact_refresh as artifact_refresh
from app.workers import v3_scene_coordinator as coordinator


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


require(
    stitch_worker.v3_scene_coordinator_loop
    is artifact_refresh.v3_scene_coordinator_loop,
    "stitch_worker_not_bound_to_artifact_refresh",
)
require(
    bool(getattr(coordinator, "_fresh_stitch_artifact_urls_installed", False)),
    "fresh_stitch_artifact_urls_not_installed",
)
require(
    coordinator._enabled(),
    "v3_scene_coordinator_not_enabled",
)


async def probe_database() -> None:
    pool = await asyncio.wait_for(get_db_pool(), timeout=15.0)
    try:
        async with pool.acquire() as conn:
            value = await asyncio.wait_for(conn.fetchval("select 1"), timeout=10.0)
        require(value == 1, f"database_probe_unexpected_result:{value!r}")
    finally:
        await pool.close()


asyncio.run(probe_database())
print("STITCH_ARTIFACT_REFRESH_BINDING=PASS")
print("V3_SCENE_COORDINATOR_RUNTIME_ENABLED=PASS")
print("STITCH_WORKER_DATABASE_PROBE=PASS")
PY
pass "STITCH_WORKER_RUNTIME_PROBE"

# Re-check state after the executable probe so a restart/crash during
# certification cannot be accepted accidentally.
running_after="$(docker inspect --format '{{.State.Running}}' "$CONTAINER")"
restart_after="$(docker inspect --format '{{.RestartCount}}' "$CONTAINER")"
[[ "$running_after" == "true" ]] \
  || fail "stitch worker stopped during certification"
[[ "$restart_after" == "$restart_before" ]] \
  || fail "stitch worker restarted during certification: before=$restart_before after=$restart_after"
pass "STITCH_WORKER_STABILITY"

# Observability only. Never roll back solely because an INFO log marker is absent.
if docker logs --since 10m "$CONTAINER" 2>&1 \
  | grep -Fq "V3 scene coordinator started"; then
  echo "V3_SCENE_COORDINATOR_STARTUP_LOG=OBSERVED"
else
  echo "V3_SCENE_COORDINATOR_STARTUP_LOG=NOT_OBSERVED_NON_BLOCKING"
fi

echo "V3_STITCH_WORKER_RUNTIME_CERTIFICATION=PASS"
