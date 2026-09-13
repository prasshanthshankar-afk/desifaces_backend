#!/usr/bin/env bash
set -Eeuo pipefail

# desifaces.ai Sep 12 production cutover.
# DATA POLICY: preserve the existing production database and apply exactly one
# allowlisted, effective-dated internal COGS migration. DEV data is never read,
# copied, restored or synchronized into production.

EXPECTED_GUEST_HOST="desifaces-gpu"
EXPECTED_AZURE_RESOURCE="desifaces-gpu-non-prod"
EXPECTED_AZURE_RG="desifaces_rg"
EXPECTED_PUBLIC_IP="52.252.188.211"
CURRENT_PROD_RUNTIME_SHA="793db700365e6d0fcbf9345b97737afac497afc0"
BACKEND_SHA="18dfd6a3a4941307a466960108e4573f3b9ff555"
WEB_SHA="21d1c8d4083c7f9705b957807e12d3d2bdf518d8"
MOBILE_SHA="b92e58a92eca115508804dd68dbc99cab086441f"
MIGRATION_REL="migrations/2026_09_12_audio_tts_cost_basis.sql"
MIGRATION_BLOB="f0c9bc640b183571e8f8ee5eccb6709b0b81fa59"
BACKEND_REPO="prasshanthshankar-afk/desifaces_backend"
WEB_REPO="prasshanthshankar-afk/desifaces_web"

CANONICAL_ROOT="/home/azureuser/workspace/desifaces"
BACKUP_ROOT="/home/azureuser/backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="/tmp/desifaces-v3-preserve-data-$STAMP"
STAGE="/home/azureuser/workspace/.desifaces-v3-preserve-data-stage-$STAMP"
BACKUP_DIR="$BACKUP_ROOT/desifaces-v3-preserve-data-$STAMP"
DB_BACKUP="$BACKUP_DIR/desifaces-production-$STAMP.dump"
VALIDATE_DB="desifaces_v3_targeted_validate_${STAMP//[^0-9]/}"
VALIDATE_DB_CREATED=0
PREVIOUS_ROOT=""
WEB_HOST="web.desifaces.ai"
API_HOST="api.desifaces.ai"
WEB_PORT="13000"
WEB_CONTAINER="df-v3-web-prod"
WEB_IMAGE="desifaces-web-production:$WEB_SHA"
WEB_CONFIG=""
WEB_CONFIG_BACKUP=""
NGINX_CHANGED=0

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }

rollback_nginx(){
  set +e
  if (( NGINX_CHANGED == 1 )) && [[ -n "$WEB_CONFIG" && -n "$WEB_CONFIG_BACKUP" && -f "$WEB_CONFIG_BACKUP" ]]; then
    sudo cp "$WEB_CONFIG_BACKUP" "$WEB_CONFIG" || true
    sudo nginx -t >/dev/null 2>&1 && sudo systemctl reload nginx >/dev/null 2>&1 || true
    log "NGINX_ROLLBACK=ATTEMPTED"
  fi
}

cleanup(){
  rc=$?
  set +e
  if (( VALIDATE_DB_CREATED == 1 )); then
    DB_USER="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_USER"' 2>/dev/null || true)"
    [[ -n "$DB_USER" ]] && docker exec desifaces-db dropdb -U "$DB_USER" --if-exists "$VALIDATE_DB" >/dev/null 2>&1 || true
  fi
  rm -rf "$RUN" >/dev/null 2>&1 || true
  (( rc == 0 )) || rollback_nginx
  exit "$rc"
}
trap cleanup EXIT

[[ "${DESIFACES_PRODUCTION_CUTOVER_APPROVED:-}" == "YES" ]] || \
  fail "set DESIFACES_PRODUCTION_CUTOVER_APPROVED=YES for this one production cutover"

for x in gh git tar docker curl python3 sudo nginx sha256sum getent grep awk sed cmp mktemp; do need "$x"; done
docker compose version >/dev/null 2>&1 || fail "docker compose v2 is required"

mkdir -p "$RUN" "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
rm -rf "$STAGE"
mkdir -p "$STAGE"

log "============================================================"
log " desifaces.ai V3 — PRODUCTION PRESERVE-DATA CUTOVER"
log "============================================================"
log "prod_data_source=EXISTING_PRODUCTION_ONLY"
log "dev_data_import=FORBIDDEN"
log "live_db_restore=FORBIDDEN"
log "allowlisted_live_db_migrations=1"
log "migration=$MIGRATION_REL"
log "backend_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"

log ""
log "===== 1. CERTIFY EXACT LIVE PRODUCTION HOST — READ ONLY ====="
HOST="$(hostname -s 2>/dev/null || hostname)"
[[ "$HOST" == "$EXPECTED_GUEST_HOST" ]] || fail "guest hostname mismatch actual=$HOST expected=$EXPECTED_GUEST_HOST"

curl -fsS -H Metadata:true \
  'http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01' \
  > "$RUN/imds.json"
python3 - "$RUN/imds.json" "$EXPECTED_AZURE_RESOURCE" "$EXPECTED_AZURE_RG" <<'PY'
import json,sys
p,expected_name,expected_rg=sys.argv[1:]
d=json.load(open(p,encoding='utf-8'))
name=str(d.get('name') or '')
rg=str(d.get('resourceGroupName') or d.get('resourceGroup') or '')
if name != expected_name:
    raise SystemExit(f'FAIL: Azure VM resource mismatch actual={name} expected={expected_name}')
if rg.lower() != expected_rg.lower():
    raise SystemExit(f'FAIL: Azure resource group mismatch actual={rg} expected={expected_rg}')
print(f'AZURE_RESOURCE_IDENTITY=PASS name={name} resource_group={rg}')
PY

for public_host in "$WEB_HOST" "$API_HOST"; do
  getent ahostsv4 "$public_host" | awk '{print $1}' | sort -u | grep -Fx "$EXPECTED_PUBLIC_IP" >/dev/null || \
    fail "$public_host does not resolve to certified public IP $EXPECTED_PUBLIC_IP"
done

test -f "$CANONICAL_ROOT/RELEASE" || fail "current production RELEASE metadata missing"
grep -q '^release=v3-production-' "$CANONICAL_ROOT/RELEASE" || fail "current canonical source is not marked production"
grep -q "^backend_runtime_release_sha=$CURRENT_PROD_RUNTIME_SHA$" "$CANONICAL_ROOT/RELEASE" || \
  fail "current production runtime baseline mismatch"

docker inspect desifaces-db >/dev/null 2>&1 || fail "desifaces-db is not running"
docker inspect desifaces-redis >/dev/null 2>&1 || fail "desifaces-redis is not running"
docker network inspect df-net >/dev/null 2>&1 || fail "df-net is missing"

curl -fsS --max-time 10 "https://$WEB_HOST/auth/login" >/dev/null || fail "public Web is not healthy before cutover"
curl -fsS --max-time 10 "https://$API_HOST/director/api/health" >/dev/null || fail "public Director is not healthy before cutover"
curl -fsS --max-time 10 "https://$API_HOST/assistant/api/health" >/dev/null || fail "public Assistant is not healthy before cutover"

log "PUBLIC_PRODUCTION_IDENTITY=PASS"
log "PROD_DATA_SOURCE=EXISTING_PRODUCTION_ONLY"

log ""
log "===== 2. CERTIFY IMMUTABLE RELEASE ACCESS — READ ONLY ====="
gh auth status -h github.com >/dev/null 2>&1 || fail "GitHub CLI is not authenticated on production VM"
gh api "repos/$BACKEND_REPO/commits/$BACKEND_SHA" --jq .sha | grep -qx "$BACKEND_SHA" || fail "backend release commit inaccessible"
gh api "repos/$WEB_REPO/commits/$WEB_SHA" --jq .sha | grep -qx "$WEB_SHA" || fail "Web release commit inaccessible"
log "RELEASE_ACCESS=PASS"

log ""
log "===== 3. EXPORT TARGET APPLICATION SOURCE — NO DATABASE DATA ====="
gh api -H 'Accept: application/vnd.github+json' "repos/$BACKEND_REPO/tarball/$BACKEND_SHA" > "$RUN/backend.tar.gz"
[[ -s "$RUN/backend.tar.gz" ]] || fail "backend archive download failed"
tar -xzf "$RUN/backend.tar.gz" --strip-components=1 -C "$STAGE"

mkdir -p "$STAGE/web-app"
gh api -H 'Accept: application/vnd.github+json' "repos/$WEB_REPO/tarball/$WEB_SHA" > "$RUN/web.tar.gz"
[[ -s "$RUN/web.tar.gz" ]] || fail "Web archive download failed"
tar -xzf "$RUN/web.tar.gz" --strip-components=1 -C "$STAGE/web-app"

! find "$STAGE" -name .git -type d -print -quit | grep -q . || fail "staged application unexpectedly contains Git metadata"
[[ -f "$STAGE/docker-compose.yml" ]] || fail "target docker-compose.yml missing"
[[ -f "$STAGE/deploy/production/docker-compose.v3-app.production.yml" ]] || fail "target production overlay missing"
[[ -f "$STAGE/$MIGRATION_REL" ]] || fail "allowlisted migration missing"
[[ "$(git hash-object "$STAGE/$MIGRATION_REL")" == "$MIGRATION_BLOB" ]] || fail "allowlisted migration provenance mismatch"
[[ -f "$STAGE/scripts/apply-v3-audio-cogs-production.sh" ]] || fail "target Audio COGS production guard missing"
[[ -f "$STAGE/web-app/web/Dockerfile" ]] || fail "target Web Dockerfile missing"

# Production secrets/config come only from the existing production environment.
[[ -f "$CANONICAL_ROOT/infra/.env" ]] || fail "existing production infra/.env missing"
mkdir -p "$STAGE/infra"
cp "$CANONICAL_ROOT/infra/.env" "$STAGE/infra/.env"
chmod 600 "$STAGE/infra/.env"

cat > "$STAGE/RELEASE" <<EOF
product=desifaces.ai
release=v3-production-20260912-targeted
backend_release_sha=$BACKEND_SHA
backend_application_sha=$BACKEND_SHA
backend_runtime_release_sha=$BACKEND_SHA
web_sha=$WEB_SHA
mobile_parity_sha=$MOBILE_SHA
created_utc=$STAMP
source_policy=immutable_github_export_no_git
production_guest_hostname=$HOST
production_azure_resource=$EXPECTED_AZURE_RESOURCE
production_public_ip=$EXPECTED_PUBLIC_IP
db_policy=preserve_existing_production_targeted_migration_only
prod_data_source=existing_production_only
dev_data_import=forbidden
live_db_restore=forbidden
allowlisted_live_db_migrations=1
allowlisted_migration=$MIGRATION_REL
EOF

log "TARGET_SOURCE_PROVENANCE=PASS"
log "DEV_DATA_IMPORT=NONE"

log ""
log "===== 4. PREBUILD ONLY CHANGED RUNTIMES BEFORE DB MUTATION ====="
STAGE_COMPOSE=(docker compose --env-file "$STAGE/infra/.env" -f "$STAGE/docker-compose.yml" -f "$STAGE/deploy/production/docker-compose.v3-app.production.yml")
mapfile -t STAGE_SERVICES < <("${STAGE_COMPOSE[@]}" config --services)
for svc in svc-audio svc-audio-worker svc-director svc-director-worker; do
  printf '%s\n' "${STAGE_SERVICES[@]}" | grep -qx "$svc" || fail "required changed service missing: $svc"
done
"${STAGE_COMPOSE[@]}" build svc-audio svc-audio-worker svc-director svc-director-worker
docker build -t "$WEB_IMAGE" "$STAGE/web-app/web"
log "CHANGED_RUNTIME_PREBUILD=PASS"

log ""
log "===== 5. BACK UP EXISTING LIVE PRODUCTION DATABASE ====="
DB_USER="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_DB"')"
[[ -n "$DB_USER" && -n "$DB_NAME" ]] || fail "cannot resolve live production PostgreSQL identity"
docker exec desifaces-db pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null
docker exec desifaces-db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$DB_BACKUP"
[[ -s "$DB_BACKUP" ]] || fail "production DB backup is empty"
sha256sum "$DB_BACKUP" > "$DB_BACKUP.sha256"
log "PRODUCTION_DB_BACKUP=PASS file=$DB_BACKUP"
log "BACKUP_SOURCE=LIVE_PRODUCTION_DB_ONLY"

pricing_hash(){
  local db="$1"
  docker exec desifaces-db psql -X -A -t -U "$DB_USER" -d "$db" -v ON_ERROR_STOP=1 -c "
    select md5(coalesce(string_agg(j,'|' order by j),'')) from (
      select to_jsonb(s)::text j from public.pricing_skus s
       where code in ('AUDIO_TTS_1K_CHARS','IMG_STD_RUN','FUSION_TALK_MIN')
      union all
      select to_jsonb(v)::text j from public.pricing_variant_lines v
       where sku_code in ('AUDIO_TTS_1K_CHARS','IMG_STD_RUN','FUSION_TALK_MIN')
    ) q;"
}

log ""
log "===== 6. CERTIFY ONLY ALLOWLISTED MIGRATION ON PROD-DATA CLONE ====="
docker exec desifaces-db createdb -U "$DB_USER" "$VALIDATE_DB"
VALIDATE_DB_CREATED=1
# This restores the PROD backup into a disposable validation DB only. It never
# restores any data into the live production DB.
docker exec -i desifaces-db pg_restore -U "$DB_USER" -d "$VALIDATE_DB" --no-owner --no-privileges < "$DB_BACKUP" >/dev/null
CLONE_PRICE_BEFORE="$(pricing_hash "$VALIDATE_DB")"
docker exec -i desifaces-db psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$VALIDATE_DB" < "$STAGE/$MIGRATION_REL" >/dev/null
CLONE_PRICE_AFTER="$(pricing_hash "$VALIDATE_DB")"
[[ "$CLONE_PRICE_BEFORE" == "$CLONE_PRICE_AFTER" ]] || fail "allowlisted migration changed customer pricing on validation clone"
CLONE_COGS="$(docker exec desifaces-db psql -X -A -t -U "$DB_USER" -d "$VALIDATE_DB" -v ON_ERROR_STOP=1 -c "select variable_cost_money from public.pricing_sku_costs where sku_code='AUDIO_TTS_1K_CHARS' and component_code='azure_neural_tts_payg' and is_active=true and effective_to is null order by effective_from desc limit 1;")"
[[ "${CLONE_COGS//[[:space:]]/}" == "0.01600000" || "${CLONE_COGS//[[:space:]]/}" == "0.016" ]] || fail "validation clone Audio COGS mismatch: $CLONE_COGS"
docker exec desifaces-db dropdb -U "$DB_USER" "$VALIDATE_DB"
VALIDATE_DB_CREATED=0
log "TARGETED_MIGRATION_CLONE_CERTIFICATION=PASS"
log "VALIDATION_DB_SOURCE=PRODUCTION_BACKUP"
log "VALIDATION_DB_RESTORE_TO_LIVE=NONE"

log ""
log "===== 7. ACTIVATE TARGET APPLICATION SOURCE — DB UNCHANGED ====="
PREVIOUS_ROOT="$BACKUP_ROOT/desifaces-source-pre-targeted-$STAMP"
mv "$CANONICAL_ROOT" "$PREVIOUS_ROOT"
mv "$STAGE" "$CANONICAL_ROOT"
cd "$CANONICAL_ROOT"
log "SOURCE_ACTIVATION=PASS previous=$PREVIOUS_ROOT"
log "LIVE_DB_ACTION=NONE"

log ""
log "===== 8. APPLY EXACTLY ONE TARGETED LIVE DB MIGRATION ====="
# The production guard makes a table-level COGS backup and verifies customer
# pricing hashes are unchanged before/after the migration.
DF_PRODUCTION_CONFIRM=YES \
DF_PRODUCTION_HOSTNAME="$HOST" \
DF_DB_CONTAINER=desifaces-db \
bash scripts/apply-v3-audio-cogs-production.sh
log "LIVE_DB_MIGRATION_COUNT=1"
log "LIVE_DB_MIGRATION=$MIGRATION_REL"
log "DEV_DB_IMPORT=NONE"
log "LIVE_DB_RESTORE=NONE"

log ""
log "===== 9. RECREATE ONLY CHANGED BACKEND RUNTIMES ====="
COMPOSE=(docker compose --env-file "$CANONICAL_ROOT/infra/.env" -f "$CANONICAL_ROOT/docker-compose.yml" -f "$CANONICAL_ROOT/deploy/production/docker-compose.v3-app.production.yml")
for c in df-svc-audio df-svc-audio-worker df-v3-svc-director df-v3-svc-director-worker; do
  if docker inspect "$c" >/dev/null 2>&1; then
    printf '%s %s\n' "$c" "$(docker inspect -f '{{.Image}}' "$c")" >> "$BACKUP_DIR/pre-cutover-container-images.txt"
  fi
done
"${COMPOSE[@]}" up -d --no-deps --force-recreate svc-audio svc-audio-worker svc-director svc-director-worker
log "BACKEND_RECREATE_SCOPE=svc-audio,svc-audio-worker,svc-director,svc-director-worker"
log "POSTGRES_CONTAINER_RECREATE=NONE"
log "REDIS_CONTAINER_RECREATE=NONE"

wait_http(){
  local name="$1" url="$2" max="${3:-90}" i code
  for ((i=1;i<=max;i++)); do
    code="$(curl -sS --max-time 4 -o /tmp/df-health.$$ -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [[ "$code" == 200 ]]; then log "PASS $name $url"; return 0; fi
    sleep 2
  done
  fail "$name did not become healthy: $url"
}

wait_http audio http://127.0.0.1:8004/api/health
wait_http director http://127.0.0.1:18011/api/health
# Assistant is deliberately preserved; verify it rather than recreating it.
wait_http assistant-preserved http://127.0.0.1:18012/api/health
wait_http core-preserved http://127.0.0.1:8000/api/health
wait_http face-preserved http://127.0.0.1:8003/api/health
wait_http fusion-preserved http://127.0.0.1:8002/api/health
log "TARGETED_BACKEND_RUNTIME_CERTIFICATION=PASS"

log ""
log "===== 10. START TARGET WEB LOCALLY ====="
docker rm -f "$WEB_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$WEB_CONTAINER" --restart unless-stopped --network df-net \
  -p "127.0.0.1:${WEB_PORT}:3000" \
  -e CORE_BASE_URL=http://svc-core:8000 \
  -e DASHBOARD_BASE_URL=http://svc-dashboard:8005 \
  -e FACE_BASE_URL=http://svc-face:8003 \
  -e AUDIO_BASE_URL=http://svc-audio:8004 \
  -e FUSION_BASE_URL=http://svc-fusion:8002 \
  -e DIRECTOR_BASE_URL=http://svc-director:8011 \
  -e FUSION_EXTENSION_BASE_URL=http://svc-fusion-extension:8006 \
  -e PRICING_BASE_URL=http://svc-pricing:8009 \
  -e COMMERCE_BASE_URL=http://svc-commerce:8008 \
  -e NOTIFICATION_BASE_URL=http://svc-core:8000 \
  -e ASSISTANT_BASE_URL=http://svc-assistant:8012 \
  -e COOKIE_SECURE=true "$WEB_IMAGE" >/dev/null
wait_http web-target-local "http://127.0.0.1:${WEB_PORT}/auth/login"
curl -fsS --max-time 10 "http://127.0.0.1:${WEB_PORT}/auth/login" | grep -qi desifaces || fail "target Web branding missing"
log "TARGET_WEB_LOCAL=PASS"

find_https_config(){
  local host="$1" f
  while IFS= read -r f; do
    sudo grep -Eq "server_name[^;]*${host//./\\.}[^;]*;" "$f" || continue
    sudo grep -Eq 'listen[[:space:]]+([^;]*:)?443|listen[[:space:]]+443' "$f" || continue
    printf '%s\n' "$f"; return 0
  done < <(sudo grep -RIl -E "server_name[^;]*${host//./\\.}" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null | grep -Ev '(\.bak$|\.backup$|\.before-|\.pre-|~$)' | sort -u)
  return 1
}

log ""
log "===== 11. CUT OVER WEB ONLY; PRESERVE EXISTING API INGRESS ====="
WEB_CONFIG="$(find_https_config "$WEB_HOST" || true)"
[[ -n "$WEB_CONFIG" ]] || fail "HTTPS nginx config for $WEB_HOST not found"
NGDIR="/var/backups/desifaces-nginx"; sudo mkdir -p "$NGDIR"
WEB_CONFIG_BACKUP="$NGDIR/$(basename "$WEB_CONFIG").pre-targeted-$STAMP"
sudo cp "$WEB_CONFIG" "$WEB_CONFIG_BACKUP"
TMP_WEB="$(mktemp)"; sudo cat "$WEB_CONFIG" > "$TMP_WEB"
python3 - "$TMP_WEB" "$WEB_HOST" "$WEB_PORT" <<'PY'
from pathlib import Path
import re,sys
p=Path(sys.argv[1]); host=sys.argv[2]; port=sys.argv[3]
lines=p.read_text().splitlines()
out=[]; start=None; depth=0
for i,l in enumerate(lines):
    if start is None and re.match(r'^\s*server\s*\{',l):
        start=i; depth=l.count('{')-l.count('}'); continue
    if start is not None:
        depth += l.count('{')-l.count('}')
        if depth==0: out.append((start,i)); start=None
hits=[(a,b) for a,b in out if re.search(rf'(?m)^\s*server_name\s+[^;]*\b{re.escape(host)}\b[^;]*;', '\n'.join(lines[a:b+1])) and re.search(r'(?m)^\s*listen\s+[^;]*443', '\n'.join(lines[a:b+1]))]
if len(hits)!=1:
    raise SystemExit(f'expected one HTTPS {host} server block, found {len(hits)}')
a,b=hits[0]; server=lines[a:b+1]
locs=[]; s=None; depth=0
for i,l in enumerate(server):
    if s is None and re.match(r'^\s*location\s+(?:=\s*)?/\s*\{',l):
        s=i; depth=l.count('{')-l.count('}'); continue
    if s is not None:
        depth += l.count('{')-l.count('}')
        if depth==0: locs.append((s,i)); s=None
if len(locs)!=1:
    raise SystemExit(f'expected one location / in {host}, found {len(locs)}')
la,lb=locs[0]; block=server[la:lb+1]
pis=[i for i,l in enumerate(block) if re.match(r'^\s*proxy_pass\s+',l)]
if len(pis)!=1:
    raise SystemExit(f'expected one proxy_pass in location /, found {len(pis)}')
i=pis[0]; ind=block[i][:len(block[i])-len(block[i].lstrip())]
block[i]=f'{ind}proxy_pass http://127.0.0.1:{port};'
marker=f'{ind}# DESIFACES_TARGETED_WEB_20260912'
if marker not in block: block.insert(i+1,marker)
server[la:lb+1]=block; lines[a:b+1]=server
p.write_text('\n'.join(lines).rstrip()+'\n')
PY
if ! cmp -s "$TMP_WEB" <(sudo cat "$WEB_CONFIG"); then
  sudo cp "$TMP_WEB" "$WEB_CONFIG"
  NGINX_CHANGED=1
fi
rm -f "$TMP_WEB"
sudo nginx -t
sudo systemctl reload nginx
log "TARGETED_WEB_NGINX_CUTOVER=PASS"
log "API_NGINX_CHANGE=NONE"

log ""
log "===== 12. FINAL PUBLIC CERTIFICATION ====="
wait_http public-web "https://$WEB_HOST/auth/login" 45
wait_http public-director "https://$API_HOST/director/api/health" 30
wait_http public-assistant "https://$API_HOST/assistant/api/health" 30
log "PUBLIC_PRODUCTION_CERTIFICATION=PASS"

log ""
log "===== 13. INSTALL CERTIFIED TIMERS + INSPECT STRIPE ONLY ====="
DF_PRODUCTION_CONFIRM=YES \
DF_PRODUCTION_HOSTNAME="$HOST" \
bash scripts/install-v3-production-systemd-bundle.sh
bash scripts/certify-v3-stripe-live-readiness.sh prepare

NGINX_CHANGED=0
trap - EXIT
rm -rf "$RUN" >/dev/null 2>&1 || true

log ""
log "============================================================"
log " DESIFACES V3 TARGETED PRODUCTION CUTOVER=PASS"
log "============================================================"
log "PROD_DATA_SOURCE=EXISTING_PRODUCTION_ONLY"
log "DEV_DATA_IMPORT=NONE"
log "LIVE_DB_RESTORE=NONE"
log "LIVE_DB_MIGRATION_COUNT=1"
log "LIVE_DB_MIGRATION=$MIGRATION_REL"
log "POSTGRES_CONTAINER_RECREATE=NONE"
log "REDIS_CONTAINER_RECREATE=NONE"
log "ASSISTANT_RUNTIME_RECREATE=NONE"
log "BACKEND_RECREATE_SCOPE=svc-audio,svc-audio-worker,svc-director,svc-director-worker"
log "WEB_SHA=$WEB_SHA"
log "BACKEND_SHA=$BACKEND_SHA"
log "DB_BACKUP=$DB_BACKUP"
log "PREVIOUS_SOURCE=$PREVIOUS_ROOT"
log "STRIPE_LIVE_ACTION=INSPECTION_ONLY"
log "MOBILE_STORE_ACTION=NONE"
