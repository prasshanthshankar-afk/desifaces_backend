#!/usr/bin/env bash
set -Eeuo pipefail

PROD_HOST="${PROD_HOST:-desifaces-gpu}"
BACKEND_ROOT="/home/azureuser/workspace/desifaces"
LEGACY_ROOT="/home/azureuser/workspace/desifaces-v2"
WEB_ROOT="/home/azureuser/workspace/desifaces-web"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

fail(){ echo "FAIL: $*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"; }
for x in ssh; do need "$x"; done

printf '%s\n' "============================================================"
printf '%s\n' " desifaces.ai — CLEAN PRODUCTION BACKEND / WEB LAYOUT"
printf '%s\n' "============================================================"
printf '%s\n' "TARGET_BACKEND=$BACKEND_ROOT"
printf '%s\n' "TARGET_WEB=$WEB_ROOT"
printf '%s\n' "LEGACY=$LEGACY_ROOT"
printf '%s\n' "CUSTOMER_DATA_ACTION=NONE"
printf '%s\n' "DB_DATA_ACTION=NONE"
printf '%s\n' "REDIS_DATA_ACTION=NONE"
printf '%s\n' "WEB_RUNTIME_ACTION=NONE"
printf '%s\n' "LEGACY_DELETE=NONE"

ssh -o BatchMode=yes -o ConnectTimeout=12 "$PROD_HOST" 'hostname -s' >/dev/null || fail "cannot SSH to $PROD_HOST"

ssh "$PROD_HOST" "BACKEND_ROOT='$BACKEND_ROOT' LEGACY_ROOT='$LEGACY_ROOT' WEB_ROOT='$WEB_ROOT' STAMP='$STAMP' bash -s" <<'REMOTE'
set -Eeuo pipefail

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "wrong host"
[[ -f "$BACKEND_ROOT/docker-compose.yml" ]] || fail "canonical backend compose missing"
[[ -f "$BACKEND_ROOT/deploy/production/docker-compose.v3-app.production.yml" ]] || fail "canonical production overlay missing"
[[ -f "$BACKEND_ROOT/infra/.env" ]] || fail "canonical production env missing"
[[ -d "$LEGACY_ROOT" ]] || fail "legacy workspace missing"

BASE="$BACKEND_ROOT/docker-compose.yml"
OVERLAY="$BACKEND_ROOT/deploy/production/docker-compose.v3-app.production.yml"
ENV="$BACKEND_ROOT/infra/.env"
BACKUP="/home/azureuser/backups/desifaces-layout-migration-$STAMP"
mkdir -p "$BACKUP"

echo
echo "===== 1. PRE-MIGRATION SAFETY SNAPSHOT ====="
cp "$ENV" "$BACKUP/infra.env.before"
cp "$BASE" "$BACKUP/docker-compose.yml.before"
cp "$OVERLAY" "$BACKUP/docker-compose.v3-app.production.yml.before"
docker inspect desifaces-db > "$BACKUP/desifaces-db.inspect.json"
docker inspect desifaces-redis > "$BACKUP/desifaces-redis.inspect.json"
docker ps --format '{{.Names}}' | sort > "$BACKUP/containers.before"

DB_ID="$(docker inspect -f '{{.Id}}' desifaces-db)"
REDIS_ID="$(docker inspect -f '{{.Id}}' desifaces-redis)"
DB_MOUNTS="$(docker inspect desifaces-db --format '{{range .Mounts}}{{println .Type "|" .Source "|" .Destination}}{{end}}' | sort)"
REDIS_MOUNTS="$(docker inspect desifaces-redis --format '{{range .Mounts}}{{println .Type "|" .Source "|" .Destination}}{{end}}' | sort)"

DB_USER="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_DB"')"
[[ "$DB_NAME" == "desifaces" ]] || fail "unexpected production DB name: $DB_NAME"

docker exec desifaces-db pg_dump -Fc -U "$DB_USER" -d "$DB_NAME" > "$BACKUP/desifaces-$STAMP.dump"
sha256sum "$BACKUP/desifaces-$STAMP.dump" > "$BACKUP/desifaces-$STAMP.dump.sha256"

: > "$BACKUP/customer-counts.before"
for t in core.users media_assets pricing_credit_accounts pricing_credit_lots pricing_credit_reservations studio_jobs longform_jobs; do
  if docker exec desifaces-db psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -c "select to_regclass('$t') is not null" | grep -qx t; then
    n="$(docker exec desifaces-db psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -c "select count(*) from $t" | tr -d '[:space:]')"
    echo "$t|$n" >> "$BACKUP/customer-counts.before"
  fi
done

echo "BACKUP_DIR=$BACKUP"
echo "DB_CONTAINER_ID=$DB_ID"
echo "REDIS_CONTAINER_ID=$REDIS_ID"
echo "PRODUCTION_SAFETY_SNAPSHOT=PASS"


echo
echo "===== 2. CANONICAL COMPOSE PREFLIGHT ====="
cd "$BACKEND_ROOT"
PROJECT="$(docker inspect desifaces-db --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)"
[[ -n "$PROJECT" ]] || PROJECT="desifaces"
echo "COMPOSE_PROJECT=$PROJECT"

docker compose -p "$PROJECT" --env-file "$ENV" -f "$BASE" -f "$OVERLAY" config > "$BACKUP/canonical.compose.rendered.yml"
if grep -Fq "$LEGACY_ROOT" "$BACKUP/canonical.compose.rendered.yml"; then
  grep -nF "$LEGACY_ROOT" "$BACKUP/canonical.compose.rendered.yml" | head -50
  fail "canonical compose still references legacy workspace"
fi
mapfile -t CANONICAL_SERVICES < <(docker compose -p "$PROJECT" --env-file "$ENV" -f "$BASE" -f "$OVERLAY" config --services | sort -u)
[[ ${#CANONICAL_SERVICES[@]} -gt 0 ]] || fail "canonical compose has no services"
printf 'CANONICAL_SERVICE=%s\n' "${CANONICAL_SERVICES[@]}"
echo "CANONICAL_COMPOSE_PREFLIGHT=PASS"


echo
echo "===== 3. DISCOVER LEGACY-ORIGIN APPLICATION SERVICES ====="
mapfile -t APP_SERVICES < <(
  for c in $(docker ps --format '{{.Names}}' | sort); do
    [[ "$c" == "desifaces-db" || "$c" == "desifaces-redis" || "$c" == "df-v3-web-prod" ]] && continue
    wd="$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
    svc="$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.service"}}' 2>/dev/null || true)"
    if [[ "$wd" == "$LEGACY_ROOT" && -n "$svc" ]]; then
      echo "$svc"
    fi
  done | sort -u
)

[[ ${#APP_SERVICES[@]} -gt 0 ]] || echo "LEGACY_APPLICATION_SERVICES=NONE"
for s in "${APP_SERVICES[@]}"; do
  printf '%s\n' "LEGACY_APP_SERVICE=$s"
  printf '%s\n' "${CANONICAL_SERVICES[@]}" | grep -Fxq "$s" || fail "legacy service not present in canonical compose: $s"
done

echo "LEGACY_APPLICATION_DISCOVERY=PASS"


echo
echo "===== 4. MIGRATE APPLICATION RUNTIME ORIGIN TO CANONICAL BACKEND ====="
if [[ ${#APP_SERVICES[@]} -gt 0 ]]; then
  docker compose -p "$PROJECT" --env-file "$ENV" -f "$BASE" -f "$OVERLAY" \
    up -d --no-deps --force-recreate "${APP_SERVICES[@]}"
fi

# DB/Redis must remain exact same containers in this phase.
[[ "$(docker inspect -f '{{.Id}}' desifaces-db)" == "$DB_ID" ]] || fail "DB container changed"
[[ "$(docker inspect -f '{{.Id}}' desifaces-redis)" == "$REDIS_ID" ]] || fail "Redis container changed"
[[ "$(docker inspect desifaces-db --format '{{range .Mounts}}{{println .Type "|" .Source "|" .Destination}}{{end}}' | sort)" == "$DB_MOUNTS" ]] || fail "DB mounts changed"
[[ "$(docker inspect desifaces-redis --format '{{range .Mounts}}{{println .Type "|" .Source "|" .Destination}}{{end}}' | sort)" == "$REDIS_MOUNTS" ]] || fail "Redis mounts changed"

bad=0
for c in $(docker ps --format '{{.Names}}' | sort); do
  [[ "$c" == "desifaces-db" || "$c" == "desifaces-redis" || "$c" == "df-v3-web-prod" ]] && continue
  wd="$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
  mounts="$(docker inspect "$c" --format '{{range .Mounts}}{{println .Source}}{{end}}' 2>/dev/null || true)"
  if [[ "$wd" == "$LEGACY_ROOT" ]] || printf '%s\n' "$mounts" | grep -Fq "$LEGACY_ROOT"; then
    echo "FAIL_RUNTIME_REFERENCE|$c|working_dir=$wd"
    bad=1
  fi
done
[[ $bad -eq 0 ]] || fail "application runtime still references legacy workspace"
echo "APPLICATION_RUNTIME_ORIGIN=CANONICAL_BACKEND"
echo "DB_CONTAINER_UNCHANGED=PASS"
echo "REDIS_CONTAINER_UNCHANGED=PASS"


echo
echo "===== 5. HEALTH + CUSTOMER DATA GATE ====="
for spec in \
  'core|http://127.0.0.1:8000/api/health' \
  'audio|http://127.0.0.1:8004/api/health' \
  'pricing|http://127.0.0.1:8009/api/health'; do
  n="${spec%%|*}"; u="${spec#*|}"
  code=""
  for i in {1..20}; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$u" 2>/dev/null || true)"
    [[ "$code" == 200 ]] && break
    sleep 3
  done
  echo "HEALTH|$n|$code"
  [[ "$code" == 200 ]] || fail "$n health failed"
done

: > "$BACKUP/customer-counts.after"
while IFS='|' read -r t before; do
  after="$(docker exec desifaces-db psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -c "select count(*) from $t" | tr -d '[:space:]')"
  echo "$t|$after" >> "$BACKUP/customer-counts.after"
  [[ "$before" == "$after" ]] || fail "customer count changed for $t: $before -> $after"
done < "$BACKUP/customer-counts.before"
echo "CUSTOMER_DATA_PRESERVED=PASS"


echo
echo "===== 6. SEPARATE WEB DEPLOYMENT MATERIAL ====="
# Web runtime is image-baked; no host mounts are allowed.
WEB_MOUNTS="$(docker inspect df-v3-web-prod --format '{{range .Mounts}}{{println .Source}}{{end}}' 2>/dev/null || true)"
[[ -z "$WEB_MOUNTS" ]] || fail "web container unexpectedly has host mounts"
WEB_IMAGE="$(docker inspect df-v3-web-prod --format '{{.Config.Image}}')"
echo "WEB_IMAGE=$WEB_IMAGE"

if [[ -e "$WEB_ROOT" ]]; then
  fail "$WEB_ROOT already exists; refusing to merge directories"
fi
if [[ -d "$BACKEND_ROOT/web-app" ]]; then
  mv "$BACKEND_ROOT/web-app" "$WEB_ROOT"
  echo "WEB_DEPLOYMENT_MATERIAL_MOVED=PASS"
else
  mkdir -p "$WEB_ROOT"
  cat > "$WEB_ROOT/README.production.txt" <<EOF
Production web runtime is image-baked.
Container: df-v3-web-prod
Image: $WEB_IMAGE
Public URL: https://web.desifaces.ai
No host bind mounts are used by the web runtime.
EOF
  echo "WEB_DEPLOYMENT_MATERIAL_CREATED=PASS"
fi

cat > "$WEB_ROOT/PRODUCTION_RUNTIME" <<EOF
container=df-v3-web-prod
image=$WEB_IMAGE
public_url=https://web.desifaces.ai
nginx_target=http://127.0.0.1:13001
runtime_mounts=none
EOF

[[ ! -e "$BACKEND_ROOT/web-app" ]] || fail "web-app still present under backend root"
echo "BACKEND_WEB_SEPARATION=PASS"


echo
echo "===== 7. ARCHIVE NON-RUNTIME MAC METADATA FROM BACKEND ROOT ====="
mkdir -p "$BACKUP/backend-metadata"
find "$BACKEND_ROOT" -xdev -type f \( -name '._*' -o -name '.DS_Store' \) -print0 2>/dev/null |
while IFS= read -r -d '' f; do
  rel="${f#$BACKEND_ROOT/}"
  dst="$BACKUP/backend-metadata/$rel"
  mkdir -p "$(dirname "$dst")"
  mv "$f" "$dst"
done

echo "BACKEND_METADATA_ARCHIVE=PASS"


echo
echo "===== 8. FINAL WORKSPACE / LEGACY REFERENCE CERTIFICATION ====="
echo "BACKEND_ROOT=$BACKEND_ROOT"
echo "WEB_ROOT=$WEB_ROOT"
[[ -d "$BACKEND_ROOT/services" && -f "$BACKEND_ROOT/docker-compose.yml" ]] || fail "backend root incomplete"
[[ -d "$WEB_ROOT" ]] || fail "web root missing"

DOCKER_MOUNT_REFS=0
APP_ORIGIN_REFS=0
for c in $(docker ps --format '{{.Names}}' | sort); do
  refs="$(docker inspect "$c" --format '{{range .Mounts}}{{println .Source}}{{end}}' 2>/dev/null | grep -F "$LEGACY_ROOT" || true)"
  [[ -z "$refs" ]] || { echo "LEGACY_MOUNT_REF|$c|$refs"; DOCKER_MOUNT_REFS=$((DOCKER_MOUNT_REFS+1)); }
  [[ "$c" == "desifaces-db" || "$c" == "desifaces-redis" ]] && continue
  wd="$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
  [[ "$wd" != "$LEGACY_ROOT" ]] || { echo "LEGACY_ORIGIN_REF|$c"; APP_ORIGIN_REFS=$((APP_ORIGIN_REFS+1)); }
done

echo "LEGACY_RUNTIME_MOUNT_REFERENCE_COUNT=$DOCKER_MOUNT_REFS"
echo "LEGACY_APPLICATION_ORIGIN_REFERENCE_COUNT=$APP_ORIGIN_REFS"
[[ $DOCKER_MOUNT_REFS -eq 0 && $APP_ORIGIN_REFS -eq 0 ]] || fail "legacy application runtime references remain"

# DB/Redis may retain historical compose labels; those are reported, not treated as code dependency.
for c in desifaces-db desifaces-redis; do
  wd="$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
  echo "INFRA_ORIGIN_LABEL|$c|$wd"
done

WEB_CODE_IN_BACKEND=NO
[[ ! -e "$BACKEND_ROOT/web-app" ]] || WEB_CODE_IN_BACKEND=YES
[[ "$WEB_CODE_IN_BACKEND" == NO ]] || fail "web code still mixed into backend"

curl -fsS -o /dev/null https://web.desifaces.ai/auth/login || fail "public web unavailable"
echo "PUBLIC_WEB=PASS"

echo "============================================================"
echo " CLEAN PRODUCTION LAYOUT — PHASE 1 PASS"
echo "============================================================"
echo "BACKEND_ONLY_ROOT=$BACKEND_ROOT"
echo "WEB_ONLY_ROOT=$WEB_ROOT"
echo "APPLICATION_RUNTIME_LEGACY_DEPENDENCY=NONE"
echo "CUSTOMER_DATA_PRESERVED=PASS"
echo "DB_REDIS_PRESERVED=PASS"
echo "PUBLIC_WEB=PASS"
echo "LEGACY_DELETE=NOT_PERFORMED"
echo "NEXT_GATE=DB_REDIS_COMPOSE_LABEL_RETIREMENT_THEN_LEGACY_ARCHIVE"
REMOTE
