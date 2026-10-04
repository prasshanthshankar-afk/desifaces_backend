#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_SHA="${1:-}"
EXPECTED_HOST="desifaces-dev"
OWNER="prasshanthshankar-afk"
REPO="desifaces_backend"
GHCR_OWNER="prasshanthshankar-afk"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host required"
[[ "$BACKEND_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "exact backend SHA required"
command -v docker >/dev/null || fail "docker missing"
command -v curl >/dev/null || fail "curl missing"

GHCR_TOKEN="${GHCR_TOKEN:-$(gh auth token 2>/dev/null || true)}"
[[ -n "$GHCR_TOKEN" ]] || fail "GHCR_TOKEN or authenticated gh required"

echo "============================================================"
echo " desifaces DEV — OMNIHUMAN GROUP CONVERSATION DEPLOY"
echo " backend_sha=$BACKEND_SHA"
echo " production_touch=NONE"
echo "============================================================"

BASE_SERVICES=(svc-director svc-fusion svc-fusion-extension)
WORKER_SERVICES=(
  svc-director-worker
  svc-fusion-worker
  svc-fusion-extension-worker
  svc-fusion-extension-stitch-worker
)

declare -A CONTAINER OLD_ID OLD_REF FAMILY NEW_ID
ACTIVE_SERVICES=()

one_running_container_for_service(){
  local svc="$1"
  mapfile -t rows < <(
    docker ps       --filter "label=com.docker.compose.service=$svc"       --format '{{.Names}}'
  )
  if (( ${#rows[@]} == 0 )); then
    return 1
  fi
  (( ${#rows[@]} == 1 )) || fail "multiple running containers for compose service $svc: ${rows[*]}"
  printf '%s' "${rows[0]}"
}

for svc in "${BASE_SERVICES[@]}"; do
  c="$(one_running_container_for_service "$svc")" || fail "required running DEV service missing: $svc"
  CONTAINER["$svc"]="$c"
  ACTIVE_SERVICES+=("$svc")
done

PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "${CONTAINER[svc-director]}")"
WORKDIR="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "${CONTAINER[svc-director]}")"
CONFIG_FILES="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "${CONTAINER[svc-director]}")"

[[ -n "$PROJECT" && -n "$WORKDIR" && -n "$CONFIG_FILES" ]] || fail "compose ownership metadata missing"

for svc in "${BASE_SERVICES[@]}" "${WORKER_SERVICES[@]}"; do
  if c="$(one_running_container_for_service "$svc" 2>/dev/null)"; then
    project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$c")"
    [[ "$project" == "$PROJECT" ]] || fail "compose project mismatch for $svc: $project != $PROJECT"
    CONTAINER["$svc"]="$c"
    if [[ ! " ${ACTIVE_SERVICES[*]} " =~ " $svc " ]]; then
      ACTIVE_SERVICES+=("$svc")
    fi
  fi
done

echo "compose_project=$PROJECT"
echo "compose_workdir=$WORKDIR"
echo "active_services=${ACTIVE_SERVICES[*]}"
echo "COMPOSE_OWNERSHIP=PASS"

COMPOSE=(docker compose -p "$PROJECT" --project-directory "$WORKDIR")
IFS=',' read -r -a compose_files <<< "$CONFIG_FILES"
for f in "${compose_files[@]}"; do
  [[ -f "$f" ]] || fail "active compose file missing: $f"
  COMPOSE+=(-f "$f")
done

# Do not interrupt genuine in-flight DEV provider work.
#
# Historical V3 rows can remain state='running'/'generating' after earlier failed
# experiments or interrupted browser workflows. They are not proof of current
# execution. The guard therefore distinguishes recently updated/live work from
# stale lifecycle rows while still failing closed for provider jobs and active
# stitches.
DIRECTOR_C="${CONTAINER[svc-director]}"
WORK_ACTIVITY="$(
docker exec -i "$DIRECTOR_C" python - <<'PY'
import asyncio, os
import asyncpg

RECENT = "15 minutes"

async def scalar(conn, sql):
    try:
        return int(await conn.fetchval(sql) or 0)
    except asyncpg.UndefinedTableError:
        return 0

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        live_attempts = await scalar(
            conn,
            """
            select count(*)
            from public.v3_studio_stage_attempts
            where state='running'
              and coalesce(updated_at,created_at) >= now() - interval '15 minutes'
            """,
        )
        stale_attempts = await scalar(
            conn,
            """
            select count(*)
            from public.v3_studio_stage_attempts
            where state='running'
              and coalesce(updated_at,created_at) < now() - interval '15 minutes'
            """,
        )
        live_stages = await scalar(
            conn,
            """
            select count(*)
            from public.v3_studio_stage_runs
            where state='generating'
              and coalesce(updated_at,created_at) >= now() - interval '15 minutes'
            """,
        )
        stale_stages = await scalar(
            conn,
            """
            select count(*)
            from public.v3_studio_stage_runs
            where state='generating'
              and coalesce(updated_at,created_at) < now() - interval '15 minutes'
            """,
        )
        live_fusion_jobs = await scalar(
            conn,
            """
            select count(*)
            from public.studio_jobs
            where studio_type='fusion'
              and status in ('pricing_pending','queued','running','processing')
              and coalesce(updated_at,created_at) >= now() - interval '15 minutes'
            """,
        )
        live_provider_runs = await scalar(
            conn,
            """
            select count(*)
            from public.provider_runs pr
            join public.studio_jobs j on j.id=pr.job_id
            where j.studio_type='fusion'
              and pr.provider_status in ('submitted','processing')
              and coalesce(pr.updated_at,pr.created_at) >= now() - interval '15 minutes'
            """,
        )
        live_stitches = await scalar(
            conn,
            """
            select count(*)
            from public.longform_jobs
            where status='stitching_running'
              and coalesce(updated_at,created_at) >= now() - interval '20 minutes'
            """,
        )
        stale_stitches = await scalar(
            conn,
            """
            select count(*)
            from public.longform_jobs
            where status='stitching_running'
              and coalesce(updated_at,created_at) < now() - interval '20 minutes'
            """,
        )

        print(
            "|".join(
                str(v)
                for v in (
                    live_attempts,
                    live_stages,
                    live_fusion_jobs,
                    live_provider_runs,
                    live_stitches,
                    stale_attempts,
                    stale_stages,
                    stale_stitches,
                )
            )
        )
    finally:
        await conn.close()

asyncio.run(main())
PY
)"

IFS='|' read -r   LIVE_ATTEMPTS   LIVE_STAGES   LIVE_FUSION_JOBS   LIVE_PROVIDER_RUNS   LIVE_STITCHES   STALE_ATTEMPTS   STALE_STAGES   STALE_STITCHES <<< "$WORK_ACTIVITY"

echo "live_v3_attempts=$LIVE_ATTEMPTS"
echo "live_v3_stages=$LIVE_STAGES"
echo "live_fusion_jobs=$LIVE_FUSION_JOBS"
echo "live_provider_runs=$LIVE_PROVIDER_RUNS"
echo "live_stitches=$LIVE_STITCHES"
echo "stale_v3_attempts=$STALE_ATTEMPTS"
echo "stale_v3_stages=$STALE_STAGES"
echo "stale_stitches=$STALE_STITCHES"

[[ "$LIVE_ATTEMPTS" == "0" ]] || fail "recent V3 attempt activity exists"
[[ "$LIVE_STAGES" == "0" ]] || fail "recent V3 stage generation exists"
[[ "$LIVE_FUSION_JOBS" == "0" ]] || fail "recent Fusion job activity exists"
[[ "$LIVE_PROVIDER_RUNS" == "0" ]] || fail "external Fusion provider work is active"
[[ "$LIVE_STITCHES" == "0" ]] || fail "active longform stitching exists"

echo "ACTIVE_WORK_GUARD=PASS"
echo "STALE_LIFECYCLE_ROWS_DO_NOT_BLOCK_DEPLOY=PASS"

# FAL is mandatory for OmniHuman + SAM2 in both Fusion API and worker.
for svc in svc-fusion svc-fusion-worker; do
  [[ -n "${CONTAINER[$svc]:-}" ]] || continue
  c="${CONTAINER[$svc]}"
  docker exec "$c" sh -lc '[ -n "${FAL_KEY:-${FAL_API_KEY:-}}" ]'     || fail "FAL credential missing in $c"
done
echo "FAL_RUNTIME_CREDENTIAL=PASS"

printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_OWNER" --password-stdin >/dev/null
trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT

declare -A SOURCE_IMAGE
SOURCE_IMAGE[director]="ghcr.io/$GHCR_OWNER/desifaces-svc-director:$BACKEND_SHA"
SOURCE_IMAGE[fusion]="ghcr.io/$GHCR_OWNER/desifaces-svc-fusion:$BACKEND_SHA"
SOURCE_IMAGE[extension]="ghcr.io/$GHCR_OWNER/desifaces-svc-fusion-extension:$BACKEND_SHA"

for family in director fusion extension; do
  docker pull "${SOURCE_IMAGE[$family]}" >/dev/null
  NEW_ID["$family"]="$(docker image inspect -f '{{.Id}}' "${SOURCE_IMAGE[$family]}")"
  [[ -n "${NEW_ID[$family]}" ]] || fail "unable to resolve pulled image: $family"
done
echo "IMMUTABLE_IMAGES_PULL=PASS"

family_for_service(){
  case "$1" in
    svc-director|svc-director-worker) echo director ;;
    svc-fusion|svc-fusion-worker) echo fusion ;;
    svc-fusion-extension|svc-fusion-extension-worker|svc-fusion-extension-stitch-worker) echo extension ;;
    *) return 1 ;;
  esac
}

for svc in "${ACTIVE_SERVICES[@]}"; do
  c="${CONTAINER[$svc]}"
  OLD_ID["$svc"]="$(docker inspect -f '{{.Image}}' "$c")"
  OLD_REF["$svc"]="$(docker inspect -f '{{.Config.Image}}' "$c")"
  family="$(family_for_service "$svc")"
  FAMILY["$svc"]="$family"
  [[ -n "${OLD_ID[$svc]}" && -n "${OLD_REF[$svc]}" ]] || fail "image snapshot failed for $svc"
done

# Render active compose config and prove it still points at the same image refs
# used by the running containers. This prevents accidental environment/config
# cutover from another compose definition.
CONFIG_JSON="$("${COMPOSE[@]}" config --format json)"
export CONFIG_JSON
python3 - "${ACTIVE_SERVICES[@]}" <<'PY'
import json, os, subprocess, sys
cfg=json.loads(os.environ["CONFIG_JSON"])
services=cfg.get("services") or {}
for svc in sys.argv[1:]:
    container=subprocess.check_output(
        ["docker","ps","--filter",f"label=com.docker.compose.service={svc}","--format","{{.Names}}"],
        text=True,
    ).strip()
    current=subprocess.check_output(["docker","inspect","-f","{{.Config.Image}}",container],text=True).strip()
    desired=str((services.get(svc) or {}).get("image") or "").strip()
    if desired != current:
        raise SystemExit(f"compose image drift for {svc}: desired={desired!r} current={current!r}")
print("COMPOSE_IMAGE_CONTRACT=PASS")
PY
unset CONFIG_JSON

for svc in "${ACTIVE_SERVICES[@]}"; do
  docker tag "${SOURCE_IMAGE[${FAMILY[$svc]}]}" "${OLD_REF[$svc]}"
done
echo "CANDIDATE_IMAGE_TAGGING=PASS"

# Internal COGS only; customer 18-credit/sec +20% pricebook is unchanged.
DB_C="desifaces-db"
docker inspect "$DB_C" >/dev/null 2>&1 || fail "DEV DB container missing: $DB_C"
DB_USER="$(docker inspect "$DB_C" --format '{{range .Config.Env}}{{println .}}{{end}}' | awk -F= '$1=="POSTGRES_USER"{print substr($0,index($0,"=")+1); exit}')"
DB_NAME="$(docker inspect "$DB_C" --format '{{range .Config.Env}}{{println .}}{{end}}' | awk -F= '$1=="POSTGRES_DB"{print substr($0,index($0,"=")+1); exit}')"
DB_USER="${DB_USER:-postgres}"
DB_NAME="${DB_NAME:-postgres}"

MIGRATION_URL="https://raw.githubusercontent.com/$OWNER/$REPO/$BACKEND_SHA/migrations/2026_10_04_group_conversation_omnihuman_cogs.sql"
curl -fsSL "$MIGRATION_URL" | docker exec -i "$DB_C" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" >/tmp/df-omnihuman-cogs.log
echo "OMNIHUMAN_COGS_MIGRATION=PASS"

RUNTIME_CHANGED=0
rollback(){
  rc=$?
  if (( rc == 0 )); then return 0; fi
  set +e
  echo "===== AUTOMATIC DEV ROLLBACK ====="
  for svc in "${ACTIVE_SERVICES[@]}"; do
    docker tag "${OLD_ID[$svc]}" "${OLD_REF[$svc]}" >/dev/null 2>&1 || true
  done
  if (( RUNTIME_CHANGED == 1 )); then
    "${COMPOSE[@]}" up -d --no-deps --no-build --force-recreate "${ACTIVE_SERVICES[@]}" >/dev/null 2>&1 || true
  fi
  echo "DEV_ROLLBACK=ATTEMPTED"
  exit "$rc"
}
trap rollback ERR

RUNTIME_CHANGED=1
"${COMPOSE[@]}" up -d --no-deps --no-build --force-recreate "${ACTIVE_SERVICES[@]}"

for _ in $(seq 1 90); do
  ready=1
  for svc in "${ACTIVE_SERVICES[@]}"; do
    c="$(one_running_container_for_service "$svc" 2>/dev/null || true)"
    [[ -n "$c" ]] || { ready=0; continue; }
    state="$(docker inspect -f '{{.State.Status}}' "$c")"
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$c")"
    [[ "$state" == "running" ]] || ready=0
    [[ "$health" == "healthy" || "$health" == "no-healthcheck" ]] || ready=0
  done
  (( ready == 1 )) && break
  sleep 2
done
(( ready == 1 )) || fail "candidate services did not become ready"

for svc in "${ACTIVE_SERVICES[@]}"; do
  c="$(one_running_container_for_service "$svc")"
  CONTAINER["$svc"]="$c"
  actual="$(docker inspect -f '{{.Image}}' "$c")"
  expected="${NEW_ID[${FAMILY[$svc]}]}"
  [[ "$actual" == "$expected" ]] || fail "image identity mismatch for $svc"
done
echo "IMMUTABLE_IMAGE_PARITY=PASS"

# Source/runtime contracts.
docker exec "${CONTAINER[svc-director]}" python - <<'PY'
from pathlib import Path
src=Path("/app/app/fusion_input_performance.py").read_text()
assert 'provider_name = "omnihuman_v15"' in src
assert 'provider_name = "kling"' not in src
assert '"static_video"' in src and '"cinematic_video"' in src
assert '"director_choice"' in src
assert '"push_in"' in src and '"push_out"' in src
assert '"arc_left"' in src and '"arc_right"' in src
assert "creative_director_scene_direction" in src
assert 'getattr(turn, "dialogue_text", None)' in src
print("DIRECTOR_OMNIHUMAN_CAMERA_CONTRACT=PASS")
PY

for svc in svc-fusion svc-fusion-worker; do
  [[ -n "${CONTAINER[$svc]:-}" ]] || continue
  docker exec "${CONTAINER[$svc]}" python - <<'PY'
from PIL import Image
from app.services.providers.omnihuman_adapter import OmniHumanAdapter
a=OmniHumanAdapter()
assert a.provider_name=="omnihuman_v15"
assert a.model_id=="fal-ai/bytedance/omnihuman/v1.5"
assert a.speaker_mask_model_id=="fal-ai/sam2/image"
print("OMNIHUMAN_SAM2_RUNTIME=PASS")
PY
done

EXT_HASH=""
for svc in svc-fusion-extension svc-fusion-extension-worker svc-fusion-extension-stitch-worker; do
  [[ -n "${CONTAINER[$svc]:-}" ]] || continue
  c="${CONTAINER[$svc]}"
  hash="$(docker exec "$c" sha256sum /app/app/api/routes/v3_scene_pricing.py | awk '{print $1}')"
  if [[ -z "$EXT_HASH" ]]; then EXT_HASH="$hash"; else [[ "$hash" == "$EXT_HASH" ]] || fail "fusion-extension pricing runtime split-brain"; fi
done
echo "FUSION_EXTENSION_RUNTIME_PARITY=PASS"

docker exec "${CONTAINER[svc-fusion-extension]}" python - <<'PY'
from app.api.routes.v3_scene_pricing import _SHARED_SCENE_PROVIDER
assert _SHARED_SCENE_PROVIDER=="omnihuman_v15"
print("OMNIHUMAN_PARENT_PRICING_PROVIDER=PASS")
PY

if [[ -n "${CONTAINER[svc-fusion-extension-stitch-worker]:-}" ]]; then
docker exec "${CONTAINER[svc-fusion-extension-stitch-worker]}" python - <<'PY'
from pathlib import Path
src=Path("/app/app/workers/v3_scene_coordinator.py").read_text()
assert 'if not stitched_media_id:' in src
assert "Retry path: reuse the already assembled media" in src
assert '"stitched_media_id": stitched_media_id' in src
print("STITCH_ONCE_PRICING_RETRY_ONLY=PASS")
PY
fi

COGS="$(
docker exec -i "$DB_C" psql -X -At -F '|' -U "$DB_USER" -d "$DB_NAME" <<'SQL'
select sku_code,component_code,variable_cost_money
from public.pricing_sku_costs
where sku_code='GROUP_TALK_PREMIUM_SECOND'
  and component_code='fal_omnihuman_v15_variable'
  and is_active=true
order by effective_from desc
limit 1;
SQL
)"
echo "cogs=$COGS"
[[ "$COGS" == "GROUP_TALK_PREMIUM_SECOND|fal_omnihuman_v15_variable|0.16000000" ]] || fail "OmniHuman COGS row mismatch"
echo "OMNIHUMAN_COGS_RUNTIME=PASS"

trap - ERR
docker logout ghcr.io >/dev/null 2>&1 || true
trap - EXIT

echo "============================================================"
echo " OMNIHUMAN GROUP CONVERSATION BACKEND DEV DEPLOY=PASS"
echo "============================================================"
echo "BACKEND_SHA=$BACKEND_SHA"
echo "PRODUCTION_TOUCH=NONE"
