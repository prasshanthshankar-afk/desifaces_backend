#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }
TARGET_SHA="${TARGET_SHA:?TARGET_SHA is required}"
SHORT="${TARGET_SHA:0:12}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

echo "============================================================"
echo " desifaces DEV — COMMERCIAL ALIGNMENT V3"
echo "============================================================"
echo "target_sha=$TARGET_SHA"
echo "scope=PRICING_SKU_COGS_ONLY"
echo "generation_workflows=FROZEN"
echo "production=UNTOUCHED"

REPO=""
for p in "$HOME/workspace/desifaces-v3" "$HOME/workspace/desifaces_backend" "$HOME/workspace/desifaces-backend"; do
  if [[ -d "$p/.git" ]]; then REPO="$p"; break; fi
done
[[ -n "$REPO" ]] || { echo "FAIL: backend repo not found"; exit 2; }

WT="/tmp/df-commercial-${SHORT}"
rm -rf "$WT"
git -C "$REPO" fetch origin "$TARGET_SHA" >/dev/null 2>&1 || true
git -C "$REPO" worktree add --detach "$WT" "$TARGET_SHA" >/dev/null
cleanup(){ git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true; }
trap cleanup EXIT

MIG="$WT/migrations/2026_09_29_dev_commercial_sku_cogs_alignment.sql"
AUDIT="$WT/scripts/ops/certify-dev-studio-sku-cogs-e2e.sh"
[[ -f "$MIG" && -f "$AUDIT" ]] || { echo "FAIL: pinned migration/audit missing"; exit 2; }

echo
echo "===== 1. PINNED SOURCE CONTRACT ====="
grep -q 'FACE_MULTI_PERSON_I2I' "$WT/services/svc-face/app/app/services/multi_person_pricing_policy.py"
grep -q 'return max(1, self.natural_units)' "$WT/services/shared/python/desifaces_shared/pricing/multi_person.py"
grep -q '_VARIANT_CODE = "FUSION_MULTI_PERSON"' "$WT/services/svc-fusion-extension/app/app/api/routes/v3_scene_pricing.py"
grep -q '_PROVIDER = "provider-neutral"' "$WT/services/svc-fusion-extension/app/app/api/routes/v3_scene_pricing.py"
grep -q 'PREMIUM_CREDITS_PER_SECOND = 25' "$WT/services/svc-fusion-extension/app/app/services/premium_actual_seconds_pricing.py"
echo "COMMERCIAL_SOURCE_CONTRACT=PASS"

echo
echo "===== 2. BUILD CANDIDATES ====="
FACE_IMG="df-commercial-face:$SHORT"
FUSION_IMG="df-commercial-fusion:$SHORT"
EXT_IMG="df-commercial-fusion-extension:$SHORT"

docker build -f "$WT/services/svc-face/app/Dockerfile" -t "$FACE_IMG" "$WT" >/tmp/df-commercial-face-build.log 2>&1
echo "FACE_BUILD=PASS image=$FACE_IMG"

docker build -f "$WT/services/svc-fusion/app/Dockerfile" -t "$FUSION_IMG" "$WT" >/tmp/df-commercial-fusion-build.log 2>&1
echo "FUSION_BUILD=PASS image=$FUSION_IMG"

docker build -f "$WT/services/svc-fusion-extension/app/Dockerfile" -t "$EXT_IMG" "$WT" >/tmp/df-commercial-extension-build.log 2>&1
echo "FUSION_EXTENSION_BUILD=PASS image=$EXT_IMG"

echo
echo "===== 3. IN-IMAGE PRICING CERTIFICATION ====="
docker run --rm "$FACE_IMG" python -m py_compile \
  /app/app/services/multi_person_pricing_policy.py \
  /app/desifaces_shared/pricing/multi_person.py
echo "FACE_PRICING_PYCOMPILE=PASS"

docker run --rm "$FUSION_IMG" python -m py_compile \
  /app/desifaces_shared/pricing/multi_person.py
echo "FUSION_PRICING_PYCOMPILE=PASS"

docker run --rm "$EXT_IMG" python -m py_compile \
  /app/app/api/routes/v3_scene_pricing.py \
  /app/app/services/premium_actual_seconds_pricing.py
echo "FUSION_EXTENSION_PRICING_PYCOMPILE=PASS"

docker run --rm -i "$FUSION_IMG" python - <<'PY'
from desifaces_shared.pricing.multi_person import select_multi_person_pricing
s = select_multi_person_pricing(studio="fusion", participant_count_value=3, natural_units=2)
assert s is not None
assert s.billable_units == 2, s
assert s.variant_params == {"minutes":"2"}, s.variant_params
print("FUSION_NATURAL_UNITS_NO_PARTICIPANT_MULTIPLIER=PASS")
PY

docker run --rm "$FACE_IMG" sh -lc '
  grep -q "FACE_MULTI_PERSON_I2I" /app/app/services/multi_person_pricing_policy.py &&
  grep -q "provider=\"openai\"" /app/app/services/creator_orchestrator.py
'
echo "FACE_PRICING_SELECTOR_IMAGE=PASS"

docker run --rm "$EXT_IMG" sh -lc '
  grep -q "_VARIANT_CODE = \"FUSION_MULTI_PERSON\"" /app/app/api/routes/v3_scene_pricing.py &&
  grep -q "_PROVIDER = \"provider-neutral\"" /app/app/api/routes/v3_scene_pricing.py &&
  grep -q "PREMIUM_CREDITS_PER_SECOND = 25" /app/app/services/premium_actual_seconds_pricing.py
'
echo "FUSION_PARENT_PRICING_IMAGE=PASS"

echo
echo "===== 4. DATABASE PREFLIGHT — ROLLBACK ONLY ====="
DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
PRICING_CONTAINER="${PRICING_CONTAINER:-df-svc-pricing}"
DB_URL="$(docker exec "$PRICING_CONTAINER" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DB_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DB_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

PREFLIGHT="/tmp/df-commercial-migration-preflight.sql"
{
  echo "BEGIN;"
  sed -e '/^[[:space:]]*BEGIN;[[:space:]]*$/d' -e '/^[[:space:]]*COMMIT;[[:space:]]*$/d' "$MIG"
  echo "ROLLBACK;"
} > "$PREFLIGHT"

"${PSQL[@]}" < "$PREFLIGHT" >/tmp/df-commercial-migration-preflight.log
echo "COMMERCIAL_MIGRATION_PREFLIGHT=PASS"

echo
echo "===== 5. SNAPSHOT CURRENT COMMERCIAL ROWS ====="
SNAP="/tmp/desifaces-commercial-before-${STAMP}.txt"
"${PSQL[@]}" -P pager=off -c "
select 'SKU' kind,code key,default_unit_credits::text value,provider_hint::text detail
from public.pricing_skus
where code in (
 'IMG_STD_RUN','IMG_HD_RUN','FACE_EDIT_PREMIUM_RUN','FACE_MULTI_PERSON','AUDIO_MULTI_PERSON',
 'FUSION_TALK_MIN','FUSION_MULTI_PERSON','LONGFORM_CINEMATIC_MIN','LONGFORM_TALK_MIN',
 'LONGFORM_TALK_ECONOMY_10S','LONGFORM_TALK_ECONOMY_20S','LONGFORM_TALK_ECONOMY_30S',
 'LONGFORM_TALK_PREMIUM_10S','LONGFORM_TALK_PREMIUM_20S','LONGFORM_TALK_PREMIUM_30S',
 'LONGFORM_TALK_PREMIUM_SECOND'
)
union all
select 'COST',sku_code||':'||component_code,variable_cost_money::text,cost_model
from public.pricing_sku_costs
where is_active=true
  and sku_code in (
 'IMG_STD_RUN','IMG_HD_RUN','FACE_EDIT_PREMIUM_RUN','FACE_MULTI_PERSON','AUDIO_MULTI_PERSON',
 'FUSION_TALK_MIN','FUSION_MULTI_PERSON','LONGFORM_CINEMATIC_MIN','LONGFORM_TALK_MIN',
 'LONGFORM_TALK_ECONOMY_10S','LONGFORM_TALK_ECONOMY_20S','LONGFORM_TALK_ECONOMY_30S',
 'LONGFORM_TALK_PREMIUM_10S','LONGFORM_TALK_PREMIUM_20S','LONGFORM_TALK_PREMIUM_30S',
 'LONGFORM_TALK_PREMIUM_SECOND'
)
order by 1,2;
" > "$SNAP"
echo "COMMERCIAL_SNAPSHOT=$SNAP"

echo
echo "===== 6. APPLY FORWARD-ONLY COMMERCIAL MIGRATION ====="
"${PSQL[@]}" < "$MIG" >/tmp/df-commercial-migration-apply.log
echo "COMMERCIAL_DB_ALIGNMENT=PASS"

declare -A ROLLBACK_IMAGE
declare -A CANDIDATE_FOR
CANDIDATE_FOR[df-svc-face]="$FACE_IMG"
CANDIDATE_FOR[df-svc-face-worker]="$FACE_IMG"
CANDIDATE_FOR[df-svc-fusion]="$FUSION_IMG"
CANDIDATE_FOR[df-svc-fusion-worker]="$FUSION_IMG"
CANDIDATE_FOR[df-svc-fusion-extension]="$EXT_IMG"
CANDIDATE_FOR[df-svc-fusion-extension-worker]="$EXT_IMG"

compose_recreate() {
  local c="$1"
  local project service wd files
  project="$(docker inspect "$c" --format '{{ index .Config.Labels "com.docker.compose.project" }}')"
  service="$(docker inspect "$c" --format '{{ index .Config.Labels "com.docker.compose.service" }}')"
  wd="$(docker inspect "$c" --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')"
  files="$(docker inspect "$c" --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}')"
  [[ -n "$project" && -n "$service" && -n "$wd" && -n "$files" ]] || {
    echo "FAIL: compose labels incomplete for $c"; return 1;
  }
  local args=(-p "$project")
  local IFS=','
  read -ra fs <<< "$files"
  for f in "${fs[@]}"; do args+=(-f "$f"); done
  ( cd "$wd" && docker compose "${args[@]}" up -d --no-deps --force-recreate --pull never --no-build "$service" )
}

health_wait() {
  local c="$1"
  for _ in $(seq 1 60); do
    local running health
    running="$(docker inspect "$c" --format '{{.State.Running}}' 2>/dev/null || true)"
    health="$(docker inspect "$c" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || true)"
    if [[ "$running" == "true" && ( "$health" == "healthy" || "$health" == "none" ) ]]; then
      return 0
    fi
    if [[ "$health" == "unhealthy" ]]; then
      return 1
    fi
    sleep 2
  done
  return 1
}

rollback_runtime() {
  echo
  echo "===== RUNTIME ROLLBACK ====="
  for c in "${!ROLLBACK_IMAGE[@]}"; do
    docker inspect "$c" >/dev/null 2>&1 || continue
    live_ref="$(docker inspect "$c" --format '{{.Config.Image}}')"
    docker tag "${ROLLBACK_IMAGE[$c]}" "$live_ref" || true
    compose_recreate "$c" || true
  done
}

echo
echo "===== 7. TARGETED PRICING-OWNER CUTOVER ====="
for c in df-svc-face df-svc-face-worker df-svc-fusion df-svc-fusion-worker df-svc-fusion-extension df-svc-fusion-extension-worker; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "SKIP_MISSING_CONTAINER=$c"; continue; }
  old_id="$(docker inspect "$c" --format '{{.Image}}')"
  live_ref="$(docker inspect "$c" --format '{{.Config.Image}}')"
  [[ "$live_ref" != *@* ]] || { echo "FAIL: digest-only live image ref unsupported for safe retag: $c $live_ref"; rollback_runtime; exit 1; }
  rb="df-commercial-rollback-${c#df-}:$STAMP"
  docker tag "$old_id" "$rb"
  ROLLBACK_IMAGE[$c]="$rb"
  docker tag "${CANDIDATE_FOR[$c]}" "$live_ref"
done

set +e
CUTOVER_FAIL=0
for c in df-svc-face df-svc-face-worker df-svc-fusion df-svc-fusion-worker df-svc-fusion-extension df-svc-fusion-extension-worker; do
  docker inspect "$c" >/dev/null 2>&1 || continue
  if ! compose_recreate "$c"; then CUTOVER_FAIL=1; break; fi
  if ! health_wait "$c"; then echo "FAIL: health $c"; CUTOVER_FAIL=1; break; fi
  echo "CUTOVER_HEALTH=PASS container=$c image=$(docker inspect "$c" --format '{{.Config.Image}}')"
done
set -e
if [[ "$CUTOVER_FAIL" != "0" ]]; then
  rollback_runtime
  echo "COMMERCIAL_RUNTIME_CUTOVER=FAIL_ROLLED_BACK"
  exit 1
fi
echo "COMMERCIAL_RUNTIME_CUTOVER=PASS"

echo
echo "===== 8. LIVE SOURCE CONTRACT ====="
docker exec df-svc-face sh -lc 'grep -q "FACE_MULTI_PERSON_I2I" /app/app/services/multi_person_pricing_policy.py'
docker exec -i df-svc-fusion python - <<'PY'
from desifaces_shared.pricing.multi_person import select_multi_person_pricing
s=select_multi_person_pricing(studio="fusion",participant_count_value=3,natural_units=2)
assert s and s.billable_units==2
print("LIVE_FUSION_NATURAL_UNITS=PASS")
PY
docker exec df-svc-fusion-extension sh -lc '
  grep -q "_VARIANT_CODE = \"FUSION_MULTI_PERSON\"" /app/app/api/routes/v3_scene_pricing.py &&
  grep -q "_PROVIDER = \"provider-neutral\"" /app/app/api/routes/v3_scene_pricing.py &&
  grep -q "PREMIUM_CREDITS_PER_SECOND = 25" /app/app/services/premium_actual_seconds_pricing.py
'
echo "LIVE_COMMERCIAL_SOURCE_CONTRACT=PASS"

echo
echo "===== 9. FINAL ALL-STUDIO SKU / COGS GATE ====="
set +e
bash "$AUDIT" 2>&1 | tee /tmp/df-studio-sku-cogs-e2e-post-alignment.log
AUDIT_RC=${PIPESTATUS[0]}
set -e

echo
echo "============================================================"
echo " COMMERCIAL ALIGNMENT FINAL"
echo "============================================================"
echo "migration_preflight=PASS"
echo "db_alignment=PASS"
echo "runtime_cutover=PASS"
echo "audit_rc=$AUDIT_RC"
echo "snapshot=$SNAP"
echo "generation_workflows=FROZEN"
echo "production=UNTOUCHED"

if [[ "$AUDIT_RC" != "0" ]]; then
  echo "COMMERCIAL_ALIGNMENT=FAIL_CERTIFICATION"
  exit "$AUDIT_RC"
fi

echo "COMMERCIAL_ALIGNMENT=PASS"
echo "============================================================"
