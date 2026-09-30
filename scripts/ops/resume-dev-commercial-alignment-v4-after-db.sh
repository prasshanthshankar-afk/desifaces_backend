#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }
TARGET_SHA="${TARGET_SHA:?TARGET_SHA is required}"
SHORT="${TARGET_SHA:0:12}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

echo "============================================================"
echo " desifaces DEV — RESUME COMMERCIAL V4 AFTER DB ALIGNMENT"
echo "============================================================"
echo "target_sha=$TARGET_SHA"
echo "db_migration=ALREADY_APPLIED"
echo "generation_workflows=FROZEN"
echo "production=UNTOUCHED"

REPO=""
for p in "$HOME/workspace/desifaces-v3" "$HOME/workspace/desifaces_backend" "$HOME/workspace/desifaces-backend"; do
  if [[ -d "$p/.git" ]]; then REPO="$p"; break; fi
done
[[ -n "$REPO" ]] || { echo "FAIL: backend repo not found"; exit 2; }

WT="/tmp/df-commercial-v4-resume-$SHORT"
rm -rf "$WT"
git -C "$REPO" fetch origin "$TARGET_SHA" >/dev/null 2>&1 || true
git -C "$REPO" worktree add --detach "$WT" "$TARGET_SHA" >/dev/null
cleanup(){ git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true; }
trap cleanup EXIT

AUDIT="$WT/scripts/ops/certify-dev-narrow-launch-sku-cogs-v4.sh"
[[ -f "$AUDIT" ]] || { echo "FAIL: pinned audit missing"; exit 2; }

FACE_IMG="df-commercial-v4-face:$SHORT"
FUSION_IMG="df-commercial-v4-fusion:$SHORT"
EXT_IMG="df-commercial-v4-fusion-extension:$SHORT"

for img in "$FACE_IMG" "$FUSION_IMG" "$EXT_IMG"; do
  docker image inspect "$img" >/dev/null 2>&1 || { echo "FAIL: candidate image missing: $img"; exit 2; }
done
echo "CANDIDATE_IMAGES=PASS"

# Prove the DB change that already committed is present before touching runtime.
DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
PRICING_CONTAINER="${PRICING_CONTAINER:-df-svc-pricing}"
DB_URL="$(docker exec "$PRICING_CONTAINER" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DB_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DB_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

DB_GATE="$("${PSQL[@]}" -Atq -c "
select case when
  (select default_unit_credits from public.pricing_skus where code='IMG_STD_RUN')=6
  and (select default_unit_credits from public.pricing_skus where code='FACE_EDIT_PREMIUM_RUN')=12
  and (select default_unit_credits from public.pricing_skus where code='FACE_MULTI_PERSON')=8
  and (select default_unit_credits from public.pricing_skus where code='FACE_MULTI_PERSON_I2I')=15
  and (select default_unit_credits from public.pricing_skus where code='FUSION_TALK_MIN')=275
  and (select default_unit_credits from public.pricing_skus where code='FUSION_MULTI_PERSON')=330
then 1 else 0 end;
")"
[[ "$DB_GATE" == "1" ]] || { echo "FAIL: V4 DB alignment is not present; stop"; exit 3; }
echo "COMMERCIAL_V4_DB_ALREADY_ALIGNED=PASS"

declare -A CANDIDATE_FOR
CANDIDATE_FOR[df-svc-face]="$FACE_IMG"
CANDIDATE_FOR[df-svc-face-worker]="$FACE_IMG"
CANDIDATE_FOR[df-svc-fusion]="$FUSION_IMG"
CANDIDATE_FOR[df-svc-fusion-worker]="$FUSION_IMG"
CANDIDATE_FOR[df-svc-fusion-extension]="$EXT_IMG"
CANDIDATE_FOR[df-svc-fusion-extension-worker]="$EXT_IMG"

# Resolve one canonical Compose project/env from an existing target container.
DONOR=""
for c in df-svc-face df-svc-face-worker df-svc-fusion df-svc-fusion-worker df-svc-fusion-extension df-svc-fusion-extension-worker; do
  if docker inspect "$c" >/dev/null 2>&1; then DONOR="$c"; break; fi
done
[[ -n "$DONOR" ]] || { echo "FAIL: no target runtime container found"; exit 4; }

PROJECT="$(docker inspect "$DONOR" --format '{{index .Config.Labels "com.docker.compose.project"}}')"
PROJECT_DIR="$(docker inspect "$DONOR" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
CONFIG_FILES="$(docker inspect "$DONOR" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')"
[[ -n "$PROJECT" && -n "$PROJECT_DIR" && -d "$PROJECT_DIR" && -n "$CONFIG_FILES" ]] || {
  echo "FAIL: compose ownership labels incomplete"; exit 4;
}

IFS=',' read -r -a CFG_ARR <<< "$CONFIG_FILES"
COMPOSE_FILES=()
for f in "${CFG_ARR[@]}"; do
  [[ "$f" = /* ]] || f="$PROJECT_DIR/$f"
  [[ -f "$f" ]] || { echo "FAIL: compose file missing: $f"; exit 4; }
  COMPOSE_FILES+=( -f "$f" )
done

ENV_FILE=""
for candidate in "$PROJECT_DIR/infra/.env" "$PROJECT_DIR/.env" "$HOME/workspace/desifaces-runtime/infra/.env" "$HOME/workspace/desifaces-runtime/.env"; do
  if [[ -f "$candidate" && -s "$candidate" ]]; then ENV_FILE="$candidate"; break; fi
done

COMPOSE_ENV_ARGS=()
if [[ -n "$ENV_FILE" ]]; then
  COMPOSE_ENV_ARGS=(--env-file "$ENV_FILE")
  echo "COMPOSE_ENV_SOURCE=FILE path=$ENV_FILE"
else
  echo "COMPOSE_ENV_SOURCE=RUNNING_CONTAINERS"
  while IFS= read -r c; do
    while IFS= read -r kv; do
      key="${kv%%=*}"
      val="${kv#*=}"
      [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
      if [[ -z "${!key+x}" ]]; then export "$key=$val"; fi
    done < <(docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}')
  done < <(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}')

  for required in POSTGRES_DB POSTGRES_PASSWORD DATABASE_URL REDIS_URL JWT_SECRET AZURE_STORAGE_CONNECTION_STRING; do
    [[ -n "${!required:-}" ]] || { echo "FAIL: unable to recover required compose variable: $required"; exit 5; }
  done
fi

COMPOSE=(docker compose --project-directory "$PROJECT_DIR" -p "$PROJECT" "${COMPOSE_ENV_ARGS[@]}" "${COMPOSE_FILES[@]}")
"${COMPOSE[@]}" config -q </dev/null
echo "COMPOSE_INTERPOLATION=PASS"

# Snapshot running identities and live image refs before retagging.
STATE="/tmp/desifaces-commercial-v4-runtime-before-$STAMP.tsv"
: > "$STATE"
for c in "${!CANDIDATE_FOR[@]}"; do
  docker inspect "$c" >/dev/null 2>&1 || continue
  printf '%s\t%s\t%s\n'     "$c"     "$(docker inspect "$c" --format '{{.Image}}')"     "$(docker inspect "$c" --format '{{.Config.Image}}')" >> "$STATE"
done
echo "RUNTIME_SNAPSHOT=$STATE"

declare -A ROLLBACK_IMAGE

compose_recreate() {
  local c="$1"
  local service
  service="$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.service"}}')"
  [[ -n "$service" ]] || { echo "FAIL: compose service missing for $c"; return 1; }
  "${COMPOSE[@]}" up -d --no-deps --force-recreate --pull never --no-build "$service"
}

health_wait() {
  local c="$1"
  for _ in $(seq 1 60); do
    running="$(docker inspect "$c" --format '{{.State.Running}}' 2>/dev/null || true)"
    health="$(docker inspect "$c" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || true)"
    if [[ "$running" == "true" && ( "$health" == "healthy" || "$health" == "none" ) ]]; then return 0; fi
    [[ "$health" == "unhealthy" ]] && return 1
    sleep 2
  done
  return 1
}

rollback_runtime() {
  echo "===== RUNTIME ROLLBACK ====="
  while IFS=$'\t' read -r c old_id live_ref; do
    [[ -n "$c" ]] || continue
    docker tag "$old_id" "$live_ref" || true
  done < "$STATE"

  while IFS=$'\t' read -r c old_id live_ref; do
    [[ -n "$c" ]] || continue
    compose_recreate "$c" || true
    health_wait "$c" || true
  done < "$STATE"
}

echo
echo "===== TARGETED RUNTIME CUTOVER ====="
while IFS=$'\t' read -r c old_id live_ref; do
  [[ -n "$c" ]] || continue
  [[ "$live_ref" != *@* ]] || { echo "FAIL: digest-only live ref for $c"; exit 6; }
  rb="df-commercial-v4-resume-rollback-${c#df-}:$STAMP"
  docker tag "$old_id" "$rb"
  ROLLBACK_IMAGE[$c]="$rb"
  docker tag "${CANDIDATE_FOR[$c]}" "$live_ref"
done < "$STATE"

CUTOVER_FAIL=0
set +e
while IFS=$'\t' read -r c old_id live_ref; do
  [[ -n "$c" ]] || continue
  compose_recreate "$c" || { CUTOVER_FAIL=1; break; }
  health_wait "$c" || { echo "FAIL: health $c"; CUTOVER_FAIL=1; break; }
  echo "CUTOVER_HEALTH=PASS container=$c image_id=$(docker inspect "$c" --format '{{.Image}}')"
done < "$STATE"
set -e

if [[ "$CUTOVER_FAIL" != "0" ]]; then
  rollback_runtime
  echo "COMMERCIAL_V4_RUNTIME_CUTOVER=FAIL_ROLLED_BACK"
  exit 7
fi
echo "COMMERCIAL_V4_RUNTIME_CUTOVER=PASS"

echo
echo "===== LIVE PRICING CONTRACT ====="
docker exec -i df-svc-face python - <<'PY'
from pathlib import Path
policy=Path("/app/app/services/multi_person_pricing_policy.py").read_text()
orch=Path("/app/app/services/creator_orchestrator.py").read_text()
assert "FACE_MULTI_PERSON_I2I" in policy
assert 'provider="openai"' in orch
print("LIVE_FACE_PRICING_SELECTOR=PASS")
PY

docker exec -i df-svc-fusion python - <<'PY'
from desifaces_shared.pricing.multi_person import select_multi_person_pricing
s=select_multi_person_pricing(studio="fusion",participant_count_value=3,natural_units=2)
assert s and s.billable_units==2
print("LIVE_FUSION_NATURAL_UNITS=PASS")
PY

docker exec -i df-svc-fusion-extension python - <<'PY'
from pathlib import Path
route=Path("/app/app/api/routes/v3_scene_pricing.py").read_text()
premium=Path("/app/app/services/premium_actual_seconds_pricing.py").read_text()
assert '_VARIANT_CODE = "FUSION_MULTI_PERSON"' in route
assert '_PROVIDER = "provider-neutral"' in route
assert "PREMIUM_CREDITS_PER_SECOND = 15" in premium
print("LIVE_FUSION_PARENT_SELECTOR=PASS")
PY

echo
echo "===== FINAL NARROW COMMERCIAL CERTIFICATION ====="
set +e
bash "$AUDIT" 2>&1 | tee /tmp/df-commercial-v4-resume-certification.log
AUDIT_RC=${PIPESTATUS[0]}
set -e

echo
echo "============================================================"
echo " COMMERCIAL V4 RESUME FINAL"
echo "============================================================"
echo "db_migration=ALREADY_APPLIED"
echo "compose_interpolation=PASS"
echo "runtime_cutover=PASS"
echo "audit_rc=$AUDIT_RC"
echo "generation_workflows=FROZEN"
echo "production=UNTOUCHED"

if [[ "$AUDIT_RC" != "0" ]]; then
  echo "COMMERCIAL_ALIGNMENT_V4=FAIL_CERTIFICATION"
  exit "$AUDIT_RC"
fi

echo "COMMERCIAL_ALIGNMENT_V4=PASS"
echo "============================================================"
