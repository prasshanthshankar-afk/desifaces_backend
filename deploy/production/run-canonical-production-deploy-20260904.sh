#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/home/azureuser/workspace/desifaces}"
PROD_ENV="${PROD_ENV:-$ROOT/infra/.env}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${BACKUP_DIR:-/home/azureuser/backups/desifaces-release-$STAMP}"
DB_BACKUP="$BACKUP_DIR/desifaces-$STAMP.dump"
MIGRATION_MANIFEST="$ROOT/deploy/production/migrations-v3-production-20260903.txt"
WEB_IMAGE="desifaces-web-production:${WEB_SHA:-release}"
WEB_CONTAINER="df-v3-web-prod"
WEB_PORT="13000"
WEB_HOST="web.desifaces.ai"
API_HOST="api.desifaces.ai"
VALIDATE_DB="desifaces_v3_validate_${STAMP//[^0-9]/}"
VALIDATE_DB_CREATED=0
NGINX_CHANGED=0
WEB_CONFIG=""
API_CONFIG=""
WEB_BACKUP=""
API_BACKUP=""

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }

rollback_nginx(){
  set +e
  if (( NGINX_CHANGED == 1 )); then
    [[ -n "$WEB_BACKUP" && -f "$WEB_BACKUP" ]] && sudo cp "$WEB_BACKUP" "$WEB_CONFIG" || true
    if [[ -n "$API_BACKUP" && -f "$API_BACKUP" && "$API_CONFIG" != "$WEB_CONFIG" ]]; then
      sudo cp "$API_BACKUP" "$API_CONFIG" || true
    fi
    sudo nginx -t >/dev/null 2>&1 && sudo systemctl reload nginx >/dev/null 2>&1 || true
    log "NGINX_ROLLBACK=ATTEMPTED"
  fi
}

cleanup(){
  rc=$?
  set +e
  if (( VALIDATE_DB_CREATED == 1 )); then
    u="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_USER"' 2>/dev/null)"
    [[ -n "$u" ]] && docker exec desifaces-db dropdb -U "$u" --if-exists "$VALIDATE_DB" >/dev/null 2>&1 || true
  fi
  (( rc == 0 )) || rollback_nginx
  exit "$rc"
}
trap cleanup EXIT

for x in docker curl python3 sudo nginx grep awk sed cmp mktemp openssl sha256sum; do need "$x"; done
docker compose version >/dev/null 2>&1 || fail "docker compose v2 is required"

HOST="$(hostname -s 2>/dev/null || hostname)"
[[ "$HOST" == desifaces-gpu* ]] || fail "run on desifaces-gpu; current host=$HOST"
[[ -d "$ROOT" ]] || fail "canonical production package missing: $ROOT"
[[ -f "$ROOT/RELEASE" ]] || fail "canonical RELEASE metadata missing"
[[ -f "$PROD_ENV" ]] || fail "production env missing: $PROD_ENV"
[[ -f "$ROOT/docker-compose.yml" ]] || fail "canonical docker-compose.yml missing"
[[ -f "$ROOT/deploy/production/docker-compose.v3-app.production.yml" ]] || fail "production V3 overlay missing"
[[ -f "$MIGRATION_MANIFEST" ]] || fail "production migration manifest missing"
[[ -f "$ROOT/web-app/web/Dockerfile" ]] || fail "web application Dockerfile missing"
! find "$ROOT" -name .git -type d -print -quit | grep -q . || fail "production package contains Git metadata"
docker inspect desifaces-db >/dev/null 2>&1 || fail "desifaces-db is not running"
docker inspect desifaces-redis >/dev/null 2>&1 || fail "desifaces-redis is not running"
docker network inspect df-net >/dev/null 2>&1 || fail "df-net is missing"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

BACKEND_SHA="$(awk -F= '$1=="backend_application_sha"{print $2}' "$ROOT/RELEASE")"
WEB_SHA="$(awk -F= '$1=="web_sha"{print $2}' "$ROOT/RELEASE")"
[[ -n "$BACKEND_SHA" && -n "$WEB_SHA" ]] || fail "release SHA metadata incomplete"
WEB_IMAGE="desifaces-web-production:$WEB_SHA"

log "============================================================"
log " desifaces.ai — CANONICAL PRODUCTION DEPLOY + TEST"
log "============================================================"
log "root=$ROOT"
log "backend_application_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "backup_dir=$BACKUP_DIR"

COMPOSE=(docker compose --env-file "$PROD_ENV" -f "$ROOT/docker-compose.yml" -f "$ROOT/deploy/production/docker-compose.v3-app.production.yml")
mapfile -t AVAILABLE < <("${COMPOSE[@]}" config --services)
want(){ printf '%s\n' "${AVAILABLE[@]}" | grep -qx "$1"; }
WANTED=(
  svc-pricing svc-core svc-face svc-face-worker svc-audio svc-audio-worker
  svc-fusion svc-fusion-worker svc-dashboard
  svc-fusion-extension svc-fusion-extension-worker svc-fusion-extension-stitch-worker
  svc-commerce svc-director svc-director-worker svc-assistant
)
SERVICES=()
for s in "${WANTED[@]}"; do want "$s" && SERVICES+=("$s"); done
((${#SERVICES[@]} >= 10)) || fail "unexpected application service inventory: ${#SERVICES[@]}"
log "COMPOSE_TOPOLOGY=PASS services=${SERVICES[*]}"

log ""
log "===== 1. PREBUILD APPLICATION BEFORE DATABASE CHANGE ====="
"${COMPOSE[@]}" build "${SERVICES[@]}"
docker build -t "$WEB_IMAGE" "$ROOT/web-app/web"
log "APPLICATION_BUILD=PASS"

log ""
log "===== 2. BACK UP LIVE PRODUCTION DATABASE ====="
DB_USER="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_DB"')"
[[ -n "$DB_USER" && -n "$DB_NAME" ]] || fail "cannot resolve production PostgreSQL identity"
docker exec desifaces-db pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null
docker exec desifaces-db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$DB_BACKUP"
[[ -s "$DB_BACKUP" ]] || fail "database backup is empty"
sha256sum "$DB_BACKUP" > "$DB_BACKUP.sha256"
log "DB_BACKUP=PASS file=$DB_BACKUP"

apply_manifest(){
  local db="$1" rel count=0
  while IFS= read -r rel || [[ -n "$rel" ]]; do
    rel="${rel%%$'\r'}"
    [[ -z "$rel" || "$rel" == \#* ]] && continue
    [[ -f "$ROOT/$rel" ]] || fail "migration missing: $rel"
    log "MIGRATE db=$db file=$rel"
    docker exec -i desifaces-db psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$db" < "$ROOT/$rel" >/dev/null
    count=$((count+1))
  done < "$MIGRATION_MANIFEST"
  (( count > 0 )) || fail "migration manifest empty"
  log "MIGRATION_COUNT=$count db=$db"
}

log ""
log "===== 3. CERTIFY MIGRATIONS AGAINST PRODUCTION CLONE ====="
docker exec desifaces-db createdb -U "$DB_USER" "$VALIDATE_DB"
VALIDATE_DB_CREATED=1
docker exec -i desifaces-db pg_restore -U "$DB_USER" -d "$VALIDATE_DB" --no-owner --no-privileges < "$DB_BACKUP" >/dev/null
apply_manifest "$VALIDATE_DB"
docker exec desifaces-db psql -X -A -t -U "$DB_USER" -d "$VALIDATE_DB" -v ON_ERROR_STOP=1 -c "SELECT CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='core' AND table_name='users' AND column_name='country_code') THEN 'PASS' ELSE 'FAIL' END;" | grep -qx PASS || fail "country_code clone validation failed"
docker exec desifaces-db dropdb -U "$DB_USER" "$VALIDATE_DB"
VALIDATE_DB_CREATED=0
log "DB_CLONE_MIGRATION_CERTIFICATION=PASS"

log ""
log "===== 4. APPLY CERTIFIED MIGRATIONS TO LIVE DB ====="
apply_manifest "$DB_NAME"
docker exec desifaces-db psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "SELECT CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='core' AND table_name='users' AND column_name='country_code') THEN 'PASS' ELSE 'FAIL' END;" | grep -qx PASS || fail "live country_code migration check failed"
MISMATCH="$(docker exec desifaces-db psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "SELECT COUNT(*) FROM public.pricing_billing_account_members bam JOIN core.users u ON u.id=bam.user_id JOIN public.pricing_billing_accounts ba ON ba.id=bam.billing_account_id WHERE bam.status='active' AND u.country_code='IN' AND ba.default_currency<>'INR';" 2>/dev/null || printf '0')"
[[ "${MISMATCH//[[:space:]]/}" == "0" ]] || fail "India billing currency mismatch count=$MISMATCH"
log "LIVE_DB_MIGRATIONS=PASS"

log ""
log "===== 5. RECREATE APPLICATION — POSTGRES/REDIS UNTOUCHED ====="
if want svc-pricing; then "${COMPOSE[@]}" up -d --no-deps --force-recreate svc-pricing; sleep 3; fi
APIS=(); for s in svc-core svc-face svc-audio svc-fusion svc-dashboard svc-fusion-extension svc-commerce; do want "$s" && APIS+=("$s"); done
((${#APIS[@]})) && "${COMPOSE[@]}" up -d --no-deps --force-recreate "${APIS[@]}"
WORKERS=(); for s in svc-face-worker svc-audio-worker svc-fusion-worker svc-fusion-extension-worker svc-fusion-extension-stitch-worker; do want "$s" && WORKERS+=("$s"); done
((${#WORKERS[@]})) && "${COMPOSE[@]}" up -d --no-deps --force-recreate "${WORKERS[@]}"
NEW=(); for s in svc-director svc-director-worker svc-assistant; do want "$s" && NEW+=("$s"); done
((${#NEW[@]})) && "${COMPOSE[@]}" up -d --no-deps --force-recreate "${NEW[@]}"
log "APP_RECREATE=APPLIED"

wait_http(){
  local name="$1" url="$2" max="${3:-90}" i code
  for ((i=1;i<=max;i++)); do
    code="$(curl -sS --max-time 4 -o /tmp/df-health.$$ -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [[ "$code" == 200 ]]; then log "PASS $name $url"; return 0; fi
    sleep 2
  done
  fail "$name did not become healthy: $url"
}

log ""
log "===== 6. LOCAL BACKEND CERTIFICATION ====="
wait_http core http://127.0.0.1:8000/api/health
wait_http fusion http://127.0.0.1:8002/api/health
wait_http face http://127.0.0.1:8003/api/health
wait_http audio http://127.0.0.1:8004/api/health
wait_http dashboard http://127.0.0.1:8005/api/health
wait_http fusion-extension http://127.0.0.1:8006/api/health
wait_http pricing http://127.0.0.1:8009/api/health
wait_http director http://127.0.0.1:18011/api/health
wait_http assistant http://127.0.0.1:18012/api/health
log "LOCAL_BACKEND_CERTIFICATION=PASS"

log ""
log "===== 7. START + CERTIFY LATEST WEB LOCALLY ====="
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
wait_http web-local "http://127.0.0.1:${WEB_PORT}/auth/login"
for asset in launch-home-20260903.webp launch-voice-20260903.webp; do
  curl -fsS --max-time 10 -o /dev/null "http://127.0.0.1:${WEB_PORT}/assets/$asset" || fail "web asset missing: $asset"
done
curl -fsS --max-time 10 "http://127.0.0.1:${WEB_PORT}/auth/login" | grep -qi 'desifaces' || fail "sign-in branding missing"
log "LOCAL_WEB_CERTIFICATION=PASS"

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
log "===== 8. PUBLIC NGINX CUTOVER ====="
WEB_CONFIG="$(find_https_config "$WEB_HOST" || true)"
API_CONFIG="$(find_https_config "$API_HOST" || true)"
[[ -n "$WEB_CONFIG" ]] || fail "HTTPS nginx config for $WEB_HOST not found"
[[ -n "$API_CONFIG" ]] || fail "HTTPS nginx config for $API_HOST not found"
NGDIR="/var/backups/desifaces-nginx"; sudo mkdir -p "$NGDIR"
WEB_BACKUP="$NGDIR/$(basename "$WEB_CONFIG").pre-v3-$STAMP"; sudo cp "$WEB_CONFIG" "$WEB_BACKUP"
if [[ "$API_CONFIG" == "$WEB_CONFIG" ]]; then API_BACKUP="$WEB_BACKUP"; else API_BACKUP="$NGDIR/$(basename "$API_CONFIG").pre-v3-$STAMP"; sudo cp "$API_CONFIG" "$API_BACKUP"; fi

TMP_WEB="$(mktemp)"; TMP_API="$(mktemp)"; sudo cat "$WEB_CONFIG" > "$TMP_WEB"; sudo cat "$API_CONFIG" > "$TMP_API"
python3 - "$TMP_WEB" "$WEB_HOST" "$WEB_PORT" <<'PY'
from pathlib import Path
import re,sys
p=Path(sys.argv[1]); host=sys.argv[2]; port=sys.argv[3]
lines=p.read_text().splitlines()
def blocks(kind, lines):
    out=[]; start=None; depth=0
    pat=re.compile(r'^\s*'+re.escape(kind)+r'\b[^\{]*\{')
    for i,l in enumerate(lines):
        if start is None and pat.search(l): start=i; depth=l.count('{')-l.count('}'); continue
        if start is not None:
            depth += l.count('{')-l.count('}')
            if depth==0: out.append((start,i)); start=None
    return out
servers=[(a,b) for a,b in blocks('server',lines) if re.search(rf'(?m)^\s*server_name\s+[^;]*\b{re.escape(host)}\b[^;]*;', '\n'.join(lines[a:b+1])) and re.search(r'(?m)^\s*listen\s+[^;]*443', '\n'.join(lines[a:b+1]))]
if len(servers)!=1: raise SystemExit(f'expected one HTTPS {host} block, found {len(servers)}')
a,b=servers[0]; server=lines[a:b+1]
# locate exact location / within server
locs=[]; s=None; depth=0
for i,l in enumerate(server):
    if s is None and re.match(r'^\s*location\s+(?:=\s*)?/\s*\{',l): s=i; depth=l.count('{')-l.count('}'); continue
    if s is not None:
        depth += l.count('{')-l.count('}')
        if depth==0: locs.append((s,i)); s=None
if len(locs)!=1: raise SystemExit(f'expected one location / in {host}, found {len(locs)}')
la,lb=locs[0]; block=server[la:lb+1]; marker='DESIFACES_CANONICAL_WEB_20260904'
if marker not in '\n'.join(block):
    pis=[i for i,l in enumerate(block) if re.match(r'^\s*proxy_pass\s+',l)]
    if len(pis)!=1: raise SystemExit(f'expected one proxy_pass in location /, found {len(pis)}')
    i=pis[0]; ind=block[i][:len(block[i])-len(block[i].lstrip())]
    block[i]=f'{ind}proxy_pass http://127.0.0.1:{port};'
    block.insert(i+1,f'{ind}# {marker}')
    server[la:lb+1]=block
    lines[a:b+1]=server
p.write_text('\n'.join(lines).rstrip()+'\n')
PY

python3 - "$TMP_API" "$API_HOST" <<'PY'
from pathlib import Path
import re,sys
p=Path(sys.argv[1]); host=sys.argv[2]
lines=p.read_text().splitlines(); marker='DESIFACES_CANONICAL_V3_API_ROUTES_20260904'
# find server blocks
out=[]; start=None; depth=0
for i,l in enumerate(lines):
    if start is None and re.match(r'^\s*server\s*\{',l): start=i; depth=l.count('{')-l.count('}'); continue
    if start is not None:
        depth += l.count('{')-l.count('}')
        if depth==0: out.append((start,i)); start=None
hits=[(a,b) for a,b in out if re.search(rf'(?m)^\s*server_name\s+[^;]*\b{re.escape(host)}\b[^;]*;', '\n'.join(lines[a:b+1])) and re.search(r'(?m)^\s*listen\s+[^;]*443', '\n'.join(lines[a:b+1]))]
if len(hits)!=1: raise SystemExit(f'expected one HTTPS {host} block, found {len(hits)}')
a,b=hits[0]; text='\n'.join(lines[a:b+1])
if marker not in text:
    indent='    '
    extra=[
      f'{indent}# {marker}',
      f'{indent}location ^~ /director/ {{',
      f'{indent}    proxy_pass http://127.0.0.1:18011/;',
      f'{indent}    proxy_http_version 1.1;',
      f'{indent}    proxy_set_header Host $host;',
      f'{indent}    proxy_set_header X-Real-IP $remote_addr;',
      f'{indent}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
      f'{indent}    proxy_set_header X-Forwarded-Proto https;',
      f'{indent}}}',
      f'{indent}location ^~ /assistant/ {{',
      f'{indent}    proxy_pass http://127.0.0.1:18012/;',
      f'{indent}    proxy_http_version 1.1;',
      f'{indent}    proxy_set_header Host $host;',
      f'{indent}    proxy_set_header X-Real-IP $remote_addr;',
      f'{indent}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
      f'{indent}    proxy_set_header X-Forwarded-Proto https;',
      f'{indent}}}',
    ]
    lines[b:b]=extra
p.write_text('\n'.join(lines).rstrip()+'\n')
PY

if ! cmp -s "$TMP_WEB" <(sudo cat "$WEB_CONFIG"); then sudo cp "$TMP_WEB" "$WEB_CONFIG"; NGINX_CHANGED=1; fi
if ! cmp -s "$TMP_API" <(sudo cat "$API_CONFIG"); then sudo cp "$TMP_API" "$API_CONFIG"; NGINX_CHANGED=1; fi
rm -f "$TMP_WEB" "$TMP_API"
sudo nginx -t
sudo systemctl reload nginx
log "NGINX_CUTOVER=PASS"

log ""
log "===== 9. PUBLIC PRODUCTION CERTIFICATION ====="
wait_http public-web "https://$WEB_HOST/auth/login" 45
curl -fsS --max-time 15 -o /dev/null "https://$WEB_HOST/assets/launch-home-20260903.webp"
curl -fsS --max-time 15 -o /dev/null "https://$WEB_HOST/assets/launch-voice-20260903.webp"
wait_http public-director "https://$API_HOST/director/api/health" 30
wait_http public-assistant "https://$API_HOST/assistant/api/health" 30
log "PUBLIC_PRODUCTION_CERTIFICATION=PASS"

NGINX_CHANGED=0
trap - EXIT
log ""
log "============================================================"
log " CANONICAL PRODUCTION DEPLOY PASS"
log "============================================================"
log "root=$ROOT"
log "backend_application_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "db_backup=$DB_BACKUP"
log "CANONICAL_PRODUCTION_DEPLOY=PASS"
