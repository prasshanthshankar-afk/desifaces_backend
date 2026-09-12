#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_REPO="prasshanthshankar-afk/desifaces_backend"
BACKEND_SHA="b81c3e6a2e74528c059d14c0adffab4a67af1816"
WEB_REPO="prasshanthshankar-afk/desifaces_web"
WEB_SHA="e51de0181bc9dd74c4ace4ec5ab8891f26be83d2"
PROD_ROOT="${PROD_ROOT:-/home/azureuser/workspace/desifaces-v2}"
PROD_ENV="${PROD_ENV:-$PROD_ROOT/infra/.env}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${RUN_DIR:-/tmp/desifaces-v3-production-$STAMP}"
BACKEND_DIR="$RUN_DIR/backend"
WEB_DIR="$RUN_DIR/web"
BACKUP_DIR="${BACKUP_DIR:-/home/azureuser/backups/desifaces-v3-$STAMP}"
DB_BACKUP="$BACKUP_DIR/desifaces-$STAMP.dump"
MIGRATION_MANIFEST="deploy/production/migrations-v3-production-20260903.txt"
WEB_IMAGE="desifaces-v3-web-production:$WEB_SHA"
WEB_CONTAINER="df-v3-web-prod"
WEB_PORT="13000"
WEB_HOST="web.desifaces.ai"
API_HOST="api.desifaces.ai"
VALIDATE_DB="desifaces_v3_validate_${STAMP//[^0-9]/}"
NGINX_CHANGED=0
VALIDATE_DB_CREATED=0
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
    if [[ -n "$WEB_BACKUP" && -n "$WEB_CONFIG" && -f "$WEB_BACKUP" ]]; then sudo cp "$WEB_BACKUP" "$WEB_CONFIG"; fi
    if [[ -n "$API_BACKUP" && -n "$API_CONFIG" && -f "$API_BACKUP" && "$API_CONFIG" != "$WEB_CONFIG" ]]; then sudo cp "$API_BACKUP" "$API_CONFIG"; fi
    sudo nginx -t >/dev/null 2>&1 && sudo systemctl reload nginx >/dev/null 2>&1 || true
    log "NGINX_ROLLBACK=ATTEMPTED"
  fi
}
cleanup(){
  rc=$?
  set +e
  if (( VALIDATE_DB_CREATED == 1 )); then
    DB_USER="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_USER"' 2>/dev/null)"
    docker exec desifaces-db dropdb -U "$DB_USER" --if-exists "$VALIDATE_DB" >/dev/null 2>&1 || true
  fi
  if (( rc != 0 )); then rollback_nginx; fi
  exit "$rc"
}
trap cleanup EXIT

for x in gh git docker curl python3 sudo nginx grep awk sed cmp mktemp openssl; do need "$x"; done
docker compose version >/dev/null 2>&1 || fail "docker compose v2 is required"

HOST="$(hostname -s 2>/dev/null || hostname)"
[[ "$HOST" == desifaces-gpu* ]] || fail "run this launcher on desifaces-gpu, current host=$HOST"
[[ -f "$PROD_ENV" ]] || fail "production env file not found: $PROD_ENV"
docker inspect desifaces-db >/dev/null 2>&1 || fail "production database container desifaces-db is not running"
docker inspect desifaces-redis >/dev/null 2>&1 || fail "production redis container desifaces-redis is not running"
docker network inspect df-net >/dev/null 2>&1 || fail "production network df-net is missing"

mkdir -p "$RUN_DIR" "$BACKUP_DIR"
chmod 700 "$RUN_DIR" "$BACKUP_DIR"

log "============================================================"
log " desifaces.ai V3 — PRODUCTION GPU DEPLOY + TEST"
log "============================================================"
log "host=$HOST"
log "backend_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "run_dir=$RUN_DIR"
log "backup_dir=$BACKUP_DIR"

log ""
log "===== 1. FETCH IMMUTABLE RELEASE SOURCES ====="
gh repo clone "$BACKEND_REPO" "$BACKEND_DIR" -- --no-checkout >/dev/null
(
  cd "$BACKEND_DIR"
  git checkout --detach "$BACKEND_SHA" >/dev/null
  [[ "$(git rev-parse HEAD)" == "$BACKEND_SHA" ]] || exit 10
)
gh repo clone "$WEB_REPO" "$WEB_DIR" -- --no-checkout >/dev/null
(
  cd "$WEB_DIR"
  git checkout --detach "$WEB_SHA" >/dev/null
  [[ "$(git rev-parse HEAD)" == "$WEB_SHA" ]] || exit 11
)
mkdir -p "$BACKEND_DIR/infra"
cp "$PROD_ENV" "$BACKEND_DIR/infra/.env"
chmod 600 "$BACKEND_DIR/infra/.env"
[[ -f "$BACKEND_DIR/$MIGRATION_MANIFEST" ]] || fail "migration manifest missing from frozen backend source"
log "IMMUTABLE_SOURCE=PASS"

COMPOSE=(docker compose --env-file "$BACKEND_DIR/infra/.env" -f "$BACKEND_DIR/docker-compose.yml" -f "$BACKEND_DIR/deploy/production/docker-compose.v3-app.production.yml")
mapfile -t COMPOSE_SERVICES < <("${COMPOSE[@]}" config --services)
want_service(){ local x="$1"; printf '%s\n' "${COMPOSE_SERVICES[@]}" | grep -qx "$x"; }
WANTED=(
  svc-pricing svc-core
  svc-face svc-face-worker
  svc-audio svc-audio-worker
  svc-fusion svc-fusion-worker
  svc-dashboard
  svc-fusion-extension svc-fusion-extension-worker svc-fusion-extension-stitch-worker
  svc-director svc-director-worker svc-assistant
)
SERVICES=()
for s in "${WANTED[@]}"; do want_service "$s" && SERVICES+=("$s"); done
((${#SERVICES[@]} >= 10)) || fail "unexpected production compose service inventory (${#SERVICES[@]} selected)"
log "COMPOSE_TOPOLOGY=PASS services=${SERVICES[*]}"

log ""
log "===== 2. PREBUILD BACKEND + WEB BEFORE ANY DB CHANGE ====="
"${COMPOSE[@]}" build "${SERVICES[@]}"
docker build -t "$WEB_IMAGE" "$WEB_DIR/web"
log "APPLICATION_BUILD=PASS"

log ""
log "===== 3. BACK UP LIVE PRODUCTION DATABASE ====="
DB_USER="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_DB"')"
[[ -n "$DB_USER" && -n "$DB_NAME" ]] || fail "could not resolve production PostgreSQL identity"
docker exec desifaces-db pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null
docker exec desifaces-db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$DB_BACKUP"
[[ -s "$DB_BACKUP" ]] || fail "production database backup is empty"
sha256sum "$DB_BACKUP" > "$DB_BACKUP.sha256"
log "DB_BACKUP=PASS file=$DB_BACKUP"

apply_manifest(){
  local db="$1"
  local count=0
  while IFS= read -r rel || [[ -n "$rel" ]]; do
    rel="${rel%%$'\r'}"
    [[ -z "$rel" || "$rel" == \#* ]] && continue
    [[ -f "$BACKEND_DIR/$rel" ]] || fail "migration missing: $rel"
    log "MIGRATE db=$db file=$rel"
    docker exec -i desifaces-db psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$db" < "$BACKEND_DIR/$rel" >/dev/null
    count=$((count+1))
  done < "$BACKEND_DIR/$MIGRATION_MANIFEST"
  (( count > 0 )) || fail "migration manifest is empty"
  log "MIGRATION_COUNT=$count db=$db"
}

log ""
log "===== 4. CERTIFY ALL MIGRATIONS AGAINST A CLONE OF PRODUCTION ====="
docker exec desifaces-db createdb -U "$DB_USER" "$VALIDATE_DB"
VALIDATE_DB_CREATED=1
docker exec -i desifaces-db pg_restore -U "$DB_USER" -d "$VALIDATE_DB" --no-owner --no-privileges < "$DB_BACKUP" >/dev/null
apply_manifest "$VALIDATE_DB"
docker exec desifaces-db psql -X -A -t -U "$DB_USER" -d "$VALIDATE_DB" -v ON_ERROR_STOP=1 -c "SELECT CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='core' AND table_name='users' AND column_name='country_code') THEN 'PASS' ELSE 'FAIL' END;" | grep -qx PASS || fail "country_code migration validation failed"
docker exec desifaces-db dropdb -U "$DB_USER" "$VALIDATE_DB"
VALIDATE_DB_CREATED=0
log "DB_CLONE_MIGRATION_CERTIFICATION=PASS"

log ""
log "===== 5. APPLY CERTIFIED MIGRATIONS TO LIVE PRODUCTION DB ====="
apply_manifest "$DB_NAME"
docker exec desifaces-db psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "SELECT CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='core' AND table_name='users' AND column_name='country_code') THEN 'PASS' ELSE 'FAIL' END;" | grep -qx PASS || fail "live country_code migration check failed"
# Any already-known India billing account must be INR; unknown country remains global USD until next auth country sync.
MISMATCH="$(docker exec desifaces-db psql -X -A -t -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "SELECT COUNT(*) FROM public.pricing_billing_account_members bam JOIN core.users u ON u.id=bam.user_id JOIN public.pricing_billing_accounts ba ON ba.id=bam.billing_account_id WHERE bam.status='active' AND u.country_code='IN' AND ba.default_currency<>'INR';" 2>/dev/null || printf '0')"
[[ "${MISMATCH//[[:space:]]/}" == "0" ]] || fail "India billing currency mismatch count=$MISMATCH"
log "LIVE_DB_MIGRATIONS=PASS"

log ""
log "===== 6. RECREATE V3 APPLICATION SERVICES — DB/REDIS UNTOUCHED ====="
# Pricing first, then APIs, then workers/Director/Piku. --no-deps is deliberate.
if want_service svc-pricing; then "${COMPOSE[@]}" up -d --no-deps --force-recreate svc-pricing; fi
sleep 3
CORE_APIS=()
for s in svc-core svc-face svc-audio svc-fusion svc-dashboard svc-fusion-extension; do want_service "$s" && CORE_APIS+=("$s"); done
((${#CORE_APIS[@]})) && "${COMPOSE[@]}" up -d --no-deps --force-recreate "${CORE_APIS[@]}"
WORKERS=()
for s in svc-face-worker svc-audio-worker svc-fusion-worker svc-fusion-extension-worker svc-fusion-extension-stitch-worker; do want_service "$s" && WORKERS+=("$s"); done
((${#WORKERS[@]})) && "${COMPOSE[@]}" up -d --no-deps --force-recreate "${WORKERS[@]}"
DIRECTOR=()
for s in svc-director svc-director-worker svc-assistant; do want_service "$s" && DIRECTOR+=("$s"); done
((${#DIRECTOR[@]})) && "${COMPOSE[@]}" up -d --no-deps --force-recreate "${DIRECTOR[@]}"
log "APP_RECREATE=APPLIED"

wait_http(){
  local name="$1" url="$2" max="${3:-90}" i code
  for ((i=1;i<=max;i++)); do
    code="$(curl -sS --max-time 4 -o /tmp/df-health.$$ -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [[ "$code" == "200" ]]; then log "PASS $name $url"; return 0; fi
    sleep 2
  done
  fail "$name did not become healthy: $url"
}

log ""
log "===== 7. LOCAL BACKEND CERTIFICATION ====="
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
log "===== 8. START LATEST WEB CANDIDATE LOCALLY ====="
docker rm -f "$WEB_CONTAINER" >/dev/null 2>&1 || true
docker run -d \
  --name "$WEB_CONTAINER" \
  --restart unless-stopped \
  --network df-net \
  -p "127.0.0.1:${WEB_PORT}:3000" \
  -e CORE_BASE_URL=http://svc-core:8000 \
  -e DASHBOARD_BASE_URL=http://svc-dashboard:8005 \
  -e FACE_BASE_URL=http://svc-face:8003 \
  -e AUDIO_BASE_URL=http://svc-audio:8004 \
  -e FUSION_BASE_URL=http://svc-fusion:8002 \
  -e DIRECTOR_BASE_URL=http://svc-director:8011 \
  -e FUSION_EXTENSION_BASE_URL=http://svc-fusion-extension:8006 \
  -e PRICING_BASE_URL=http://svc-pricing:8009 \
  -e NOTIFICATION_BASE_URL=http://svc-core:8000 \
  -e ASSISTANT_BASE_URL=http://svc-assistant:8012 \
  -e COOKIE_SECURE=true \
  "$WEB_IMAGE" >/dev/null
wait_http web-local "http://127.0.0.1:${WEB_PORT}/auth/login"
for asset in launch-home-20260903.webp launch-voice-20260903.webp; do
  curl -fsS --max-time 10 -o /dev/null "http://127.0.0.1:${WEB_PORT}/assets/$asset" || fail "web asset missing: $asset"
done
curl -fsS --max-time 10 "http://127.0.0.1:${WEB_PORT}/auth/login" | grep -qi 'Piku' || fail "sign-in HTML does not contain Piku"
log "LOCAL_WEB_CERTIFICATION=PASS"

log ""
log "===== 9. PREPARE PUBLIC NGINX CUTOVER ====="
find_https_config(){
  local host="$1" f
  while IFS= read -r f; do
    sudo grep -Eq "server_name[^;]*${host//./\\.}[^;]*;" "$f" || continue
    sudo grep -Eq 'listen[[:space:]]+([^;]*:)?443|listen[[:space:]]+443' "$f" || continue
    printf '%s\n' "$f"; return 0
  done < <(sudo grep -RIl -E "server_name[^;]*${host//./\\.}" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null | grep -Ev '(\.bak$|\.backup$|\.before-|\.pre-|~$)' | sort -u)
  return 1
}
WEB_CONFIG="$(find_https_config "$WEB_HOST" || true)"
API_CONFIG="$(find_https_config "$API_HOST" || true)"
[[ -n "$WEB_CONFIG" ]] || fail "HTTPS nginx config for $WEB_HOST not found"
[[ -n "$API_CONFIG" ]] || fail "HTTPS nginx config for $API_HOST not found"
NGINX_BACKUP_DIR="/var/backups/desifaces-nginx"
sudo mkdir -p "$NGINX_BACKUP_DIR"
WEB_BACKUP="$NGINX_BACKUP_DIR/$(basename "$WEB_CONFIG").pre-v3-$STAMP"
sudo cp "$WEB_CONFIG" "$WEB_BACKUP"
if [[ "$API_CONFIG" == "$WEB_CONFIG" ]]; then
  API_BACKUP="$WEB_BACKUP"
else
  API_BACKUP="$NGINX_BACKUP_DIR/$(basename "$API_CONFIG").pre-v3-$STAMP"
  sudo cp "$API_CONFIG" "$API_BACKUP"
fi

TMP_WEB="$(mktemp)"; TMP_API="$(mktemp)"
sudo cat "$WEB_CONFIG" > "$TMP_WEB"
sudo cat "$API_CONFIG" > "$TMP_API"
python3 - "$TMP_WEB" "$TMP_API" "$WEB_HOST" "$API_HOST" "$WEB_PORT" <<'PY'
from pathlib import Path
import re, sys
webp, apip, whost, ahost, wport = map(str, sys.argv[1:])

def blocks(lines):
    out=[]; start=None; depth=0
    for i,line in enumerate(lines):
        if start is None and re.match(r'^\s*server\s*\{', line):
            start=i; depth=line.count('{')-line.count('}'); continue
        if start is not None:
            depth += line.count('{')-line.count('}')
            if depth==0: out.append((start,i)); start=None
    return out

def find_https(lines,host):
    hits=[]
    for a,b in blocks(lines):
        text='\n'.join(lines[a:b+1])
        if re.search(rf'(?m)^\s*server_name\s+[^;]*\b{re.escape(host)}\b[^;]*;',text) and re.search(r'(?m)^\s*listen\s+[^;]*443\b',text): hits.append((a,b))
    if len(hits)!=1: raise SystemExit(f'expected exactly one HTTPS server block for {host}, found {len(hits)}')
    return hits[0]

# Web: switch only the established web upstream to local certified V3 web.
p=Path(webp); lines=p.read_text().splitlines(); a,b=find_https(lines,whost); block=lines[a:b+1]
changed=0
for i,line in enumerate(block):
    if re.search(r'^\s*proxy_pass\s+http://df_web/?\s*;',line) or re.search(r'^\s*proxy_pass\s+http://127\.0\.0\.1:3000/?\s*;',line) or re.search(r'^\s*proxy_pass\s+http://127\.0\.0\.1:'+re.escape(wport)+r'/?\s*;',line):
        indent=line[:len(line)-len(line.lstrip())]
        block[i]=f'{indent}proxy_pass http://127.0.0.1:{wport};'
        changed+=1
if changed==0: raise SystemExit('no recognized public web proxy_pass found')
lines[a:b+1]=block; p.write_text('\n'.join(lines).rstrip()+'\n')

# API: add only the two new V3 prefixes. Existing core/face/audio/video/etc stay untouched.
p=Path(apip); lines=p.read_text().splitlines(); a,b=find_https(lines,ahost); text='\n'.join(lines[a:b+1])
if 'DESIFACES_V3_DIRECTOR_ASSISTANT_20260904' not in text:
    if re.search(r'location\s+[^\n{]*?/director/?\b',text) or re.search(r'location\s+[^\n{]*?/assistant/?\b',text):
        raise SystemExit('director/assistant location already exists without launch marker; refusing ambiguous mutation')
    indent='    '
    addition=[
      f'{indent}# DESIFACES_V3_DIRECTOR_ASSISTANT_20260904',
      f'{indent}location /director/ {{',
      f'{indent}    proxy_pass http://127.0.0.1:18011/;',
      f'{indent}    proxy_http_version 1.1;',
      f'{indent}    proxy_set_header Host $host;',
      f'{indent}    proxy_set_header X-Forwarded-Proto $scheme;',
      f'{indent}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
      f'{indent}}}',
      f'{indent}location /assistant/ {{',
      f'{indent}    proxy_pass http://127.0.0.1:18012/;',
      f'{indent}    proxy_http_version 1.1;',
      f'{indent}    proxy_set_header Host $host;',
      f'{indent}    proxy_set_header X-Forwarded-Proto $scheme;',
      f'{indent}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
      f'{indent}}}',
    ]
    lines[b:b]=addition
p.write_text('\n'.join(lines).rstrip()+'\n')
PY
sudo cp "$TMP_WEB" "$WEB_CONFIG"
if [[ "$API_CONFIG" == "$WEB_CONFIG" ]]; then
  # Re-run API-only patch against the already web-patched same file.
  sudo cat "$WEB_CONFIG" > "$TMP_API"
  python3 - "$TMP_API" "$API_HOST" <<'PY'
from pathlib import Path
import re,sys
p=Path(sys.argv[1]); host=sys.argv[2]; lines=p.read_text().splitlines()
def blocks(lines):
    out=[]; start=None; depth=0
    for i,l in enumerate(lines):
        if start is None and re.match(r'^\s*server\s*\{',l): start=i; depth=l.count('{')-l.count('}'); continue
        if start is not None:
            depth += l.count('{')-l.count('}')
            if depth==0: out.append((start,i)); start=None
    return out
hits=[]
for a,b in blocks(lines):
    t='\n'.join(lines[a:b+1])
    if re.search(rf'(?m)^\s*server_name\s+[^;]*\b{re.escape(host)}\b[^;]*;',t) and re.search(r'(?m)^\s*listen\s+[^;]*443\b',t): hits.append((a,b))
if len(hits)!=1: raise SystemExit(f'expected one HTTPS API block, found {len(hits)}')
a,b=hits[0]; t='\n'.join(lines[a:b+1])
if 'DESIFACES_V3_DIRECTOR_ASSISTANT_20260904' not in t:
    if re.search(r'location\s+[^\n{]*?/director/?\b',t) or re.search(r'location\s+[^\n{]*?/assistant/?\b',t): raise SystemExit('ambiguous existing director/assistant location')
    ind='    '; add=[f'{ind}# DESIFACES_V3_DIRECTOR_ASSISTANT_20260904',f'{ind}location /director/ {{',f'{ind}    proxy_pass http://127.0.0.1:18011/;',f'{ind}    proxy_http_version 1.1;',f'{ind}    proxy_set_header Host $host;',f'{ind}    proxy_set_header X-Forwarded-Proto $scheme;',f'{ind}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',f'{ind}}}',f'{ind}location /assistant/ {{',f'{ind}    proxy_pass http://127.0.0.1:18012/;',f'{ind}    proxy_http_version 1.1;',f'{ind}    proxy_set_header Host $host;',f'{ind}    proxy_set_header X-Forwarded-Proto $scheme;',f'{ind}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',f'{ind}}}']
    lines[b:b]=add
p.write_text('\n'.join(lines).rstrip()+'\n')
PY
  sudo cp "$TMP_API" "$WEB_CONFIG"
else
  sudo cp "$TMP_API" "$API_CONFIG"
fi
rm -f "$TMP_WEB" "$TMP_API"
NGINX_CHANGED=1
sudo nginx -t
sudo systemctl reload nginx
log "PUBLIC_NGINX_CUTOVER=APPLIED"

log ""
log "===== 10. PUBLIC HTTPS CERTIFICATION ====="
code="$(curl -sS --max-time 20 -o /tmp/df-web-public.$$ -w '%{http_code}' "https://$WEB_HOST/auth/login")"
[[ "$code" == "200" ]] || fail "$WEB_HOST/auth/login returned HTTP $code"
grep -qi 'desifaces' /tmp/df-web-public.$$ || fail "public sign-in missing desifaces branding"
grep -qi 'Piku' /tmp/df-web-public.$$ || fail "public sign-in missing Piku"
for asset in launch-home-20260903.webp launch-voice-20260903.webp; do curl -fsS --max-time 15 -o /dev/null "https://$WEB_HOST/assets/$asset" || fail "public asset failed: $asset"; done
curl -fsS --max-time 15 "https://$API_HOST/director/api/health" | grep -q 'svc-director' || fail "public Director route failed"
curl -fsS --max-time 15 "https://$API_HOST/assistant/api/health" | grep -q 'svc-assistant' || fail "public Assistant route failed"
spend_code="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "https://$API_HOST/pricing/api/pricing/me/spending/summary" || true)"
[[ "$spend_code" == "401" || "$spend_code" == "403" ]] || fail "spending endpoint did not fail closed without auth (HTTP $spend_code)"
log "PUBLIC_HTTPS_CERTIFICATION=PASS"

NGINX_CHANGED=0
trap - EXIT
log ""
log "============================================================"
log " V3 PRODUCTION LAUNCH PASS"
log "============================================================"
log "backend_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "db_backup=$DB_BACKUP"
log "web=https://$WEB_HOST"
log "api=https://$API_HOST"
log "director=https://$API_HOST/director/api/health"
log "assistant=https://$API_HOST/assistant/api/health"
log "run_dir=$RUN_DIR"
exit 0
