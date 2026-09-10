#!/usr/bin/env bash
set -Eeuo pipefail

# DEV-only, restart-safe completion path for a failed V3 scene whose provider
# children all succeeded but deterministic scene assembly did not complete.
#
# Guarantees:
# - refuses to run outside the desifaces-dev host
# - requires a clean committed source tree
# - verifies the exact failed workflow/stage and preserved provider-job lineage
# - rebuilds/recreates ONLY the V3 Fusion Extension stitch worker
# - certifies runtime readiness without depending on log text
# - invokes Director's canonical authenticated retry-stitch API
# - creates no child provider jobs and no child charges
# - proves original provider job ids are unchanged after recovery
# - leaves production untouched

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WORKFLOW_ID="${1:-16099052-15b5-401f-a447-c5d989b7b8ad}"
STAGE_RUN_ID="${2:-cbf4b76a-21ec-4b17-951a-e0674a6f247f}"
EXPECTED_PRESERVED_CHILDREN="${EXPECTED_PRESERVED_CHILDREN:-9}"
RECOVERY_TIMEOUT_SECONDS="${RECOVERY_TIMEOUT_SECONDS:-900}"

DB_CONTAINER="${DB_CONTAINER:-desifaces-v3-db}"
DIRECTOR_CONTAINER="${DIRECTOR_CONTAINER:-df-v3-svc-director}"
STITCH_CONTAINER="${STITCH_WORKER_CONTAINER:-df-v3-svc-fusion-extension-stitch-worker}"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

pass() {
  echo "$1=PASS"
}

require_uuid() {
  [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

if [[ "$(hostname -s)" != "desifaces-dev" ]]; then
  fail "DEV-only launcher refuses host $(hostname -s)"
fi
pass "DEV_HOST_IDENTITY"

require_uuid "$WORKFLOW_ID" || fail "invalid workflow id: $WORKFLOW_ID"
require_uuid "$STAGE_RUN_ID" || fail "invalid stage run id: $STAGE_RUN_ID"
[[ "$EXPECTED_PRESERVED_CHILDREN" =~ ^[1-9][0-9]*$ ]] \
  || fail "EXPECTED_PRESERVED_CHILDREN must be a positive integer"
[[ "$RECOVERY_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] \
  || fail "RECOVERY_TIMEOUT_SECONDS must be a positive integer"

command -v git >/dev/null 2>&1 || fail "git is unavailable"
command -v docker >/dev/null 2>&1 || fail "docker is unavailable"
[[ -x scripts/v3-compose.sh ]] || fail "scripts/v3-compose.sh missing or not executable"
[[ -f scripts/certify-v3-stitch-worker-runtime.sh ]] \
  || fail "deterministic stitch-worker certification script is missing"

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  fail "tracked working tree is modified; deploy a committed source tree only"
fi
SOURCE_REF="$(git rev-parse HEAD)"
echo "source_ref=$SOURCE_REF"
pass "COMMITTED_SOURCE_TREE"

# Reuse the repository's V3 environment identity gate. It refuses non-V3 DB/Redis
# configuration before Compose can touch containers.
bash scripts/v3-compose.sh config >/dev/null
pass "V3_COMPOSE_IDENTITY"

for container in "$DB_CONTAINER" "$DIRECTOR_CONTAINER" "$STITCH_CONTAINER"; do
  docker inspect "$container" >/dev/null 2>&1 \
    || fail "required DEV container not found: $container"
done

DB_USER="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$DB_CONTAINER" | awk -F= '$1=="POSTGRES_USER" {print $2; exit}')"
DB_NAME="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$DB_CONTAINER" | awk -F= '$1=="POSTGRES_DB" {print $2; exit}')"
[[ "$DB_USER" == "desifaces_v3_admin" && "$DB_NAME" == "desifaces_v3" ]] \
  || fail "database container is not the certified V3 DEV database"
pass "V3_DATABASE_IDENTITY"

psql_scalar() {
  local sql="$1"
  docker exec -i "$DB_CONTAINER" \
    psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" \
      -At -F '|' \
      -v workflow_id="$WORKFLOW_ID" \
      -v stage_run_id="$STAGE_RUN_ID" \
      -c "$sql"
}

PRECHECK_SQL="
with latest as (
  select a.attempt_id,a.state,a.metadata_json
  from public.v3_studio_stage_attempts a
  where a.stage_run_id=:'stage_run_id'::uuid
  order by a.attempt_no desc
  limit 1
), children as (
  select c.value as child,c.ordinality as ord
  from latest l,
       jsonb_array_elements(coalesce(l.metadata_json->'children','[]'::jsonb))
       with ordinality as c(value,ordinality)
)
select
  s.state,
  l.state,
  coalesce(l.metadata_json #>> '{parent_pricing,state}',''),
  (select count(*) from children),
  (select count(*) from children
     where lower(coalesce(child->>'status','')) in ('succeeded','completed','complete','ready')
       and coalesce(child->>'fusion_job_id','') <> ''),
  coalesce((select string_agg(child->>'fusion_job_id', ',' order by ord) from children),'')
from public.v3_studio_stage_runs s
join latest l on true
where s.stage_run_id=:'stage_run_id'::uuid
  and s.workflow_id=:'workflow_id'::uuid
  and s.stage_type='fusion'
  and s.scope_type='scene';
"

precheck="$(psql_scalar "$PRECHECK_SQL")"
[[ -n "$precheck" ]] || fail "workflow/stage pair not found or not a Fusion scene"
IFS='|' read -r stage_state attempt_state pricing_state child_total child_succeeded ORIGINAL_JOB_IDS <<<"$precheck"

[[ "$stage_state" == "failed" ]] || fail "scene must be failed before recovery; state=$stage_state"
[[ "$attempt_state" == "failed" ]] || fail "latest attempt must be failed; state=$attempt_state"
[[ "$pricing_state" == "released" ]] || fail "prior parent pricing must be released; state=$pricing_state"
[[ "$child_total" == "$EXPECTED_PRESERVED_CHILDREN" ]] \
  || fail "preserved child count mismatch: actual=$child_total expected=$EXPECTED_PRESERVED_CHILDREN"
[[ "$child_succeeded" == "$EXPECTED_PRESERVED_CHILDREN" ]] \
  || fail "not every preserved child is terminal-success with durable provider lineage: succeeded=$child_succeeded total=$child_total"
[[ -n "$ORIGINAL_JOB_IDS" ]] || fail "preserved Fusion job lineage is empty"
pass "FAILED_SCENE_STATE"
pass "RELEASED_PARENT_PRICING_STATE"
pass "PRESERVED_CHILD_LINEAGE"
echo "preserved_children=$child_total"

# ---------------------------------------------------------------------------
# Build immutable corrected stitch-worker image and cut over ONLY that worker.
# ---------------------------------------------------------------------------
SHORT_REF="${SOURCE_REF:0:12}"
NEW_IMAGE="desifaces-v3-stitch-worker-ready:${SHORT_REF}"
ROLLBACK_IMAGE="desifaces-v3-stitch-worker-rollback:${SHORT_REF}"
OLD_IMAGE_ID="$(docker inspect --format '{{.Image}}' "$STITCH_CONTAINER")"
[[ -n "$OLD_IMAGE_ID" ]] || fail "cannot resolve current stitch-worker image"
docker tag "$OLD_IMAGE_ID" "$ROLLBACK_IMAGE"

echo "new_image=$NEW_IMAGE"
echo "rollback_image=$ROLLBACK_IMAGE"

docker build \
  -f services/svc-fusion-extension/app/Dockerfile \
  -t "$NEW_IMAGE" \
  .
pass "STITCH_WORKER_IMAGE_BUILD"

# Pre-cutover source/image contract: this does not contact DB or providers.
# Keep stdin attached because python '-' reads this exact probe from stdin.
docker run --rm -i --entrypoint python "$NEW_IMAGE" - <<'PY'
from app.workers import stitch_worker
from app.workers import v3_scene_artifact_refresh as artifact_refresh
from app.workers import v3_scene_coordinator as coordinator

assert stitch_worker.v3_scene_coordinator_loop is artifact_refresh.v3_scene_coordinator_loop
assert bool(getattr(coordinator, "_fresh_stitch_artifact_urls_installed", False))
print("STITCH_WORKER_IMAGE_IMPORT_CONTRACT=PASS")
PY

OVERRIDE_FILE="$(mktemp --suffix=.yml /tmp/df-v3-stitch-worker.XXXXXX)"
cleanup() {
  rm -f "$OVERRIDE_FILE"
}
trap cleanup EXIT

write_override() {
  local image="$1"
  cat >"$OVERRIDE_FILE" <<YAML
services:
  svc-fusion-extension-stitch-worker:
    image: "$image"
YAML
}

cutover_worker() {
  bash scripts/v3-compose.sh \
    -f "$OVERRIDE_FILE" \
    --profile v3-execution \
    up -d --no-deps --no-build --force-recreate \
    svc-fusion-extension-stitch-worker
}

write_override "$NEW_IMAGE"
cutover_worker

for _ in $(seq 1 30); do
  if [[ "$(docker inspect --format '{{.State.Running}}' "$STITCH_CONTAINER" 2>/dev/null || true)" == "true" ]]; then
    break
  fi
  sleep 1
done

if ! EXPECTED_IMAGE="$NEW_IMAGE" \
     STITCH_WORKER_CONTAINER="$STITCH_CONTAINER" \
     MAX_RESTART_COUNT=0 \
     bash scripts/certify-v3-stitch-worker-runtime.sh; then
  echo "STITCH_WORKER_CERTIFICATION=FAILED_ROLLING_BACK" >&2
  write_override "$ROLLBACK_IMAGE"
  cutover_worker || true
  fail "new stitch worker failed deterministic readiness certification; rollback attempted"
fi
pass "STITCH_WORKER_ONLY_CUTOVER"

# ---------------------------------------------------------------------------
# Prove Director has the canonical stitch-only recovery route/runtime contract.
# ---------------------------------------------------------------------------
docker exec -i "$DIRECTOR_CONTAINER" python - <<'PY'
from app import studio_routes_runtime
from app.fusion_execution_parallel_dispatch import (
    ParallelOrphanReconciledParentPricedSceneFusionExecutionService,
)

paths = {getattr(route, "path", "") for route in studio_routes_runtime.router.routes}
expected = "/api/director/studio-workflows/{workflow_id}/fusion-stages/{stage_run_id}/retry-stitch"
assert expected in paths, "retry_stitch_route_missing"
assert bool(
    getattr(
        ParallelOrphanReconciledParentPricedSceneFusionExecutionService,
        "_preserved_child_url_refresh_installed",
        False,
    )
), "preserved_child_url_refresh_runtime_missing"
print("DIRECTOR_STITCH_ONLY_RECOVERY_RUNTIME=PASS")
PY

# ---------------------------------------------------------------------------
# Invoke the normal authenticated Director recovery API. The short-lived token is
# minted inside the DEV Director container for the workflow owner, is never printed,
# and is validated by the same Director JWT/account-resolution dependency as a
# browser token. Parent pricing still goes through the canonical preview/reserve API.
# ---------------------------------------------------------------------------
recovery_response="$(
  docker exec \
    -e DF_RECOVERY_WORKFLOW_ID="$WORKFLOW_ID" \
    -e DF_RECOVERY_STAGE_RUN_ID="$STAGE_RUN_ID" \
    -e DF_RECOVERY_EXPECTED_CHILDREN="$EXPECTED_PRESERVED_CHILDREN" \
    -i "$DIRECTOR_CONTAINER" python - <<'PY'
from __future__ import annotations

import asyncio
import json
import os
import time
from uuid import UUID

import asyncpg
import httpx
import jwt

from app.config import settings

workflow_id = UUID(os.environ["DF_RECOVERY_WORKFLOW_ID"])
stage_run_id = UUID(os.environ["DF_RECOVERY_STAGE_RUN_ID"])
expected_children = int(os.environ["DF_RECOVERY_EXPECTED_CHILDREN"])


async def main() -> None:
    conn = await asyncpg.connect(settings.DATABASE_URL)
    try:
        row = await conn.fetchrow(
            """
            select w.owner_user_id,s.state
            from public.v3_studio_workflows w
            join public.v3_studio_stage_runs s on s.workflow_id=w.workflow_id
            where w.workflow_id=$1 and s.stage_run_id=$2
              and s.stage_type='fusion' and s.scope_type='scene'
            """,
            workflow_id,
            stage_run_id,
        )
    finally:
        await conn.close()

    if not row:
        raise RuntimeError("recovery_workflow_stage_not_found")
    if str(row["state"]) != "failed":
        raise RuntimeError(f"recovery_stage_not_failed:{row['state']}")

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": str(row["owner_user_id"]),
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
            "iat": now,
            "exp": now + 300,
        },
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALG,
    )

    path = (
        f"/api/director/studio-workflows/{workflow_id}"
        f"/fusion-stages/{stage_run_id}/retry-stitch"
    )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8011",
        timeout=httpx.Timeout(180.0, connect=10.0),
    ) as client:
        response = await client.post(
            path,
            headers={"Authorization": f"Bearer {token}"},
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"retry_stitch_http_{response.status_code}:{response.text[:2000]}"
        )

    payload = dict(response.json() or {})
    if payload.get("retry_scope") != "stitch_only":
        raise RuntimeError(f"unexpected_retry_scope:{payload.get('retry_scope')}")
    if int(payload.get("new_child_dispatches") or 0) != 0:
        raise RuntimeError("stitch_only_retry_created_child_dispatch")
    if int(payload.get("new_child_charges") or 0) != 0:
        raise RuntimeError("stitch_only_retry_created_child_charge")
    if int(payload.get("preserved_child_count") or 0) != expected_children:
        raise RuntimeError(
            f"preserved_child_count_mismatch:{payload.get('preserved_child_count')}"
        )

    # Print only non-secret recovery evidence. Never print the JWT.
    print(json.dumps({
        "attempt_id": payload.get("attempt_id"),
        "attempt_count": payload.get("attempt_count"),
        "attempt_kind": payload.get("attempt_kind"),
        "retry_scope": payload.get("retry_scope"),
        "new_child_dispatches": payload.get("new_child_dispatches"),
        "new_child_charges": payload.get("new_child_charges"),
        "preserved_child_count": payload.get("preserved_child_count"),
    }, sort_keys=True))


asyncio.run(main())
PY
)"
echo "recovery=$recovery_response"
pass "CANONICAL_STITCH_ONLY_RETRY_DISPATCH"

# ---------------------------------------------------------------------------
# Wait for the background coordinator to finish the retry, then certify durable
# state and unchanged provider lineage. No HTTP sync call is required.
# ---------------------------------------------------------------------------
STATUS_SQL="
with latest as (
  select a.attempt_id,a.state,a.metadata_json
  from public.v3_studio_stage_attempts a
  where a.stage_run_id=:'stage_run_id'::uuid
  order by a.attempt_no desc
  limit 1
), children as (
  select c.value as child,c.ordinality as ord
  from latest l,
       jsonb_array_elements(coalesce(l.metadata_json->'children','[]'::jsonb))
       with ordinality as c(value,ordinality)
)
select
  s.state,
  l.state,
  coalesce(l.metadata_json #>> '{parent_pricing,state}',''),
  coalesce(l.metadata_json->>'dispatch_outcome',''),
  coalesce(l.metadata_json #>> '{background_coordinator,phase}',''),
  (select count(*) from children),
  coalesce((select string_agg(child->>'fusion_job_id', ',' order by ord) from children),''),
  coalesce(s.metadata_json->>'canonical_scene_video_media_id','')
from public.v3_studio_stage_runs s
join latest l on true
where s.stage_run_id=:'stage_run_id'::uuid
  and s.workflow_id=:'workflow_id'::uuid;
"

deadline=$((SECONDS + RECOVERY_TIMEOUT_SECONDS))
final_status=""
while (( SECONDS < deadline )); do
  final_status="$(psql_scalar "$STATUS_SQL")"
  IFS='|' read -r final_stage final_attempt final_pricing final_dispatch final_phase final_children FINAL_JOB_IDS final_media <<<"$final_status"

  if [[ "$final_stage" == "awaiting_review" \
        && "$final_attempt" == "succeeded" \
        && "$final_pricing" == "committed" \
        && "$final_phase" == "ready_for_review" \
        && -n "$final_media" ]]; then
    break
  fi

  if [[ "$final_stage" == "failed" || "$final_attempt" == "failed" ]]; then
    echo "recovery_state=$final_status" >&2
    docker logs --tail 160 "$STITCH_CONTAINER" >&2 || true
    fail "stitch-only recovery entered a failed state"
  fi
  sleep 2
done

IFS='|' read -r final_stage final_attempt final_pricing final_dispatch final_phase final_children FINAL_JOB_IDS final_media <<<"$final_status"
[[ "$final_stage" == "awaiting_review" ]] \
  || fail "recovery did not reach awaiting_review; state=$final_stage status=$final_status"
[[ "$final_attempt" == "succeeded" ]] \
  || fail "recovery attempt did not succeed; state=$final_attempt"
[[ "$final_pricing" == "committed" ]] \
  || fail "parent pricing did not commit; state=$final_pricing"
[[ "$final_dispatch" == "stitch_only_retry" ]] \
  || fail "recovery dispatch was not stitch-only; outcome=$final_dispatch"
[[ "$final_phase" == "ready_for_review" ]] \
  || fail "background coordinator did not reach ready_for_review; phase=$final_phase"
[[ "$final_children" == "$EXPECTED_PRESERVED_CHILDREN" ]] \
  || fail "final preserved child count changed: $final_children"
[[ "$FINAL_JOB_IDS" == "$ORIGINAL_JOB_IDS" ]] \
  || fail "provider child lineage changed during stitch-only recovery"
[[ -n "$final_media" ]] || fail "canonical scene media id is missing"

# One more runtime stability check after real work completed.
EXPECTED_IMAGE="$NEW_IMAGE" \
STITCH_WORKER_CONTAINER="$STITCH_CONTAINER" \
MAX_RESTART_COUNT=0 \
bash scripts/certify-v3-stitch-worker-runtime.sh

pass "SCENE_STITCH_RECOVERY"
pass "PRESERVED_PROVIDER_JOB_IDS_UNCHANGED"
pass "NEW_CHILD_DISPATCHES_ZERO"
pass "NEW_CHILD_CHARGES_ZERO"
pass "PARENT_PRICING_COMMITTED"
pass "CANONICAL_SCENE_MEDIA_CREATED"

echo "workflow_id=$WORKFLOW_ID"
echo "stage_run_id=$STAGE_RUN_ID"
echo "scene_state=$final_stage"
echo "attempt_state=$final_attempt"
echo "canonical_scene_video_media_id=$final_media"
echo "V3_STITCH_RECOVERY_COMPLETE=PASS"
