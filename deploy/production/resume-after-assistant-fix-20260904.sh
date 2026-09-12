#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/home/azureuser/workspace/desifaces}"
PROD_ENV="${PROD_ENV:-$ROOT/infra/.env}"
WEB_HOST="web.desifaces.ai"
API_HOST="api.desifaces.ai"
WEB_CONTAINER="df-v3-web-prod"
WEB_PORT="13000"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NGINX_CHANGED=0
WEB_CONFIG=""; API_CONFIG=""; WEB_BACKUP=""; API_BACKUP=""

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
rollback_nginx(){
  set +e
  if (( NGINX_CHANGED == 1 )); then
    [[ -n "$WEB_BACKUP" && -f "$WEB_BACKUP" ]] && sudo cp "$WEB_BACKUP" "$WEB_CONFIG" || true
    if [[ -n "$API_BACKUP" && -f "$API_BACKUP" && "$API_CONFIG" != "$WEB_CONFIG" ]]; then sudo cp "$API_BACKUP" "$API_CONFIG" || true; fi
    sudo nginx -t >/dev/null 2>&1 && sudo systemctl reload nginx >/dev/null 2>&1 || true
    log "NGINX_ROLLBACK=ATTEMPTED"
  fi
}
trap 'rc=$?; (( rc==0 )) || rollback_nginx; exit $rc' EXIT

for x in docker curl python3 sudo nginx grep awk cmp mktemp; do need "$x"; done
docker compose version >/dev/null 2>&1 || fail "docker compose v2 required"
HOST="$(hostname -s 2>/dev/null || hostname)"
[[ "$HOST" == desifaces-gpu* ]] || fail "run on desifaces-gpu; current host=$HOST"
[[ -f "$PROD_ENV" && -f "$ROOT/RELEASE" ]] || fail "canonical production package/env missing"
docker inspect desifaces-db >/dev/null 2>&1 || fail "desifaces-db missing"
docker inspect desifaces-redis >/dev/null 2>&1 || fail "desifaces-redis missing"

BACKEND_SHA="$(awk -F= '$1=="backend_application_sha"{print $2}' "$ROOT/RELEASE")"
WEB_SHA="$(awk -F= '$1=="web_sha"{print $2}' "$ROOT/RELEASE")"
WEB_IMAGE="desifaces-web-production:$WEB_SHA"
COMPOSE=(docker compose --env-file "$PROD_ENV" -f "$ROOT/docker-compose.yml" -f "$ROOT/deploy/production/docker-compose.v3-app.production.yml")

log "============================================================"
log " desifaces.ai — POST-MIGRATION ASSISTANT RECOVERY + CUTOVER"
log "============================================================"
log "root=$ROOT"
log "backend_application_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "DATABASE_MIGRATION_ACTION=SKIPPED_ALREADY_CERTIFIED_AND_APPLIED"

log ""
log "===== 1. REBUILD + RECREATE ASSISTANT ONLY ====="
"${COMPOSE[@]}" build svc-assistant
"${COMPOSE[@]}" up -d --no-deps --force-recreate svc-assistant
log "ASSISTANT_RECREATE=APPLIED"

wait_http(){
  local name="$1" url="$2" max="${3:-60}" i code
  for ((i=1;i<=max;i++)); do
    code="$(curl -sS --max-time 4 -o /tmp/df-health.$$ -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [[ "$code" == 200 ]]; then log "PASS $name $url"; return 0; fi
    sleep 2
  done
  return 1
}

if ! wait_http assistant http://127.0.0.1:18012/api/health 45; then
  log "ASSISTANT_CONTAINER_STATE=$(docker inspect -f '{{.State.Status}}/{{.State.Restarting}}/{{.State.ExitCode}}' df-v3-svc-assistant 2>/dev/null || true)"
  docker logs --tail 120 df-v3-svc-assistant 2>&1 || true
  fail "assistant HTTP runtime did not start"
fi

ASSISTANT_HEALTH="$(curl -fsS http://127.0.0.1:18012/api/health)"
DIRECTOR_HEALTH="$(curl -fsS http://127.0.0.1:18011/api/health)"
printf '%s\n' "$ASSISTANT_HEALTH" > /tmp/desifaces-assistant-health.json
printf '%s\n' "$DIRECTOR_HEALTH" > /tmp/desifaces-director-health.json
python3 - <<'PY'
import json
for name,path in [('ASSISTANT','/tmp/desifaces-assistant-health.json'),('DIRECTOR','/tmp/desifaces-director-health.json')]:
    data=json.load(open(path))
    print(f"{name}_HEALTH={json.dumps(data, sort_keys=True)}")
    if data.get('runtime_ready') is not True:
        err=data.get('llm_configuration_error') or data.get('configuration_error') or 'runtime_ready_false'
        raise SystemExit(f"FAIL: {name.lower()} runtime is not ready: {err}")
print('DIRECTOR_ASSISTANT_RUNTIME_READY=PASS')
PY

log ""
log "===== 2. VERIFY ALREADY-DEPLOYED BACKEND ====="
wait_http core http://127.0.0.1:8000/api/health 20 || fail "core unhealthy"
wait_http fusion http://127.0.0.1:8002/api/health 20 || fail "fusion unhealthy"
wait_http face http://127.0.0.1:8003/api/health 20 || fail "face unhealthy"
wait_http audio http://127.0.0.1:8004/api/health 20 || fail "audio unhealthy"
wait_http dashboard http://127.0.0.1:8005/api/health 20 || fail "dashboard unhealthy"
wait_http fusion-extension http://127.0.0.1:8006/api/health 20 || fail "fusion-extension unhealthy"
wait_http pricing http://127.0.0.1:8009/api/health 20 || fail "pricing unhealthy"
log "LOCAL_BACKEND_CERTIFICATION=PASS"

log ""
log "===== 3. START + CERTIFY LATEST WEB ====="
if ! docker image inspect "$WEB_IMAGE" >/dev/null 2>&1; then docker build -t "$WEB_IMAGE" "$ROOT/web-app/web"; fi
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
wait_http web-local "http://127.0.0.1:${WEB_PORT}/auth/login" 45 || fail "web candidate unhealthy"
for asset in launch-home-20260903.webp launch-voice-20260903.webp; do curl -fsS -o /dev/null "http://127.0.0.1:${WEB_PORT}/assets/$asset" || fail "missing web asset $asset"; done
curl -fsS "http://127.0.0.1:${WEB_PORT}/auth/login" | grep -qi desifaces || fail "sign-in branding missing"
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
log "===== 4. PUBLIC NGINX CUTOVER ====="
WEB_CONFIG="$(find_https_config "$WEB_HOST" || true)"; API_CONFIG="$(find_https_config "$API_HOST" || true)"
[[ -n "$WEB_CONFIG" && -n "$API_CONFIG" ]] || fail "public HTTPS nginx configuration not found"
NGDIR="/var/backups/desifaces-nginx"; sudo mkdir -p "$NGDIR"
WEB_BACKUP="$NGDIR/$(basename "$WEB_CONFIG").pre-v3-resume-$STAMP"; sudo cp "$WEB_CONFIG" "$WEB_BACKUP"
if [[ "$API_CONFIG" == "$WEB_CONFIG" ]]; then API_BACKUP="$WEB_BACKUP"; else API_BACKUP="$NGDIR/$(basename "$API_CONFIG").pre-v3-resume-$STAMP"; sudo cp "$API_CONFIG" "$API_BACKUP"; fi
TMP_WEB="$(mktemp)"; TMP_API="$(mktemp)"; sudo cat "$WEB_CONFIG" > "$TMP_WEB"; sudo cat "$API_CONFIG" > "$TMP_API"

python3 - "$TMP_WEB" "$WEB_HOST" "$WEB_PORT" <<'PY'
from pathlib import Path
import re,sys
p=Path(sys.argv[1]); host=sys.argv[2]; port=sys.argv[3]; lines=p.read_text().splitlines(); marker='DESIFACES_CANONICAL_WEB_20260904'
def blocks(lines):
    out=[]; start=None; depth=0
    for i,l in enumerate(lines):
        if start is None and re.match(r'^\s*server\s*\{',l): start=i; depth=l.count('{')-l.count('}'); continue
        if start is not None:
            depth += l.count('{')-l.count('}')
            if depth==0: out.append((start,i)); start=None
    return out
hits=[(a,b) for a,b in blocks(lines) if re.search(rf'(?m)^\s*server_name\s+[^;]*\b{re.escape(host)}\b[^;]*;', '\n'.join(lines[a:b+1])) and re.search(r'(?m)^\s*listen\s+[^;]*443','\n'.join(lines[a:b+1]))]
if len(hits)!=1: raise SystemExit(f'expected one HTTPS {host} block, found {len(hits)}')
a,b=hits[0]; server=lines[a:b+1]
locs=[]; s=None; depth=0
for i,l in enumerate(server):
    if s is None and re.match(r'^\s*location\s+(?:=\s*)?/\s*\{',l): s=i; depth=l.count('{')-l.count('}'); continue
    if s is not None:
        depth += l.count('{')-l.count('}')
        if depth==0: locs.append((s,i)); s=None
if len(locs)!=1: raise SystemExit(f'expected one location /, found {len(locs)}')
la,lb=locs[0]; block=server[la:lb+1]
pis=[i for i,l in enumerate(block) if re.match(r'^\s*proxy_pass\s+',l)]
if marker not in '\n'.join(block):
    if len(pis)!=1: raise SystemExit(f'expected one proxy_pass, found {len(pis)}')
    i=pis[0]; ind=block[i][:len(block[i])-len(block[i].lstrip())]; block[i]=f'{ind}proxy_pass http://127.0.0.1:{port};'; block.insert(i+1,f'{ind}# {marker}')
    server[la:lb+1]=block; lines[a:b+1]=server
p.write_text('\n'.join(lines).rstrip()+'\n')
PY

python3 - "$TMP_API" "$API_HOST" <<'PY'
from pathlib import Path
import re,sys
p=Path(sys.argv[1]); host=sys.argv[2]; lines=p.read_text().splitlines(); marker='DESIFACES_CANONICAL_V3_API_ROUTES_20260904'
out=[]; start=None; depth=0
for i,l in enumerate(lines):
    if start is None and re.match(r'^\s*server\s*\{',l): start=i; depth=l.count('{')-l.count('}'); continue
    if start is not None:
        depth += l.count('{')-l.count('}')
        if depth==0: out.append((start,i)); start=None
hits=[(a,b) for a,b in out if re.search(rf'(?m)^\s*server_name\s+[^;]*\b{re.escape(host)}\b[^;]*;', '\n'.join(lines[a:b+1])) and re.search(r'(?m)^\s*listen\s+[^;]*443','\n'.join(lines[a:b+1]))]
if len(hits)!=1: raise SystemExit(f'expected one HTTPS {host} block, found {len(hits)}')
a,b=hits[0]; text='\n'.join(lines[a:b+1])
if marker not in text:
    ind='    '; extra=[
      f'{ind}# {marker}',
      f'{ind}location ^~ /director/ {{',f'{ind}    proxy_pass http://127.0.0.1:18011/;',f'{ind}    proxy_http_version 1.1;',f'{ind}    proxy_set_header Host $host;',f'{ind}    proxy_set_header X-Real-IP $remote_addr;',f'{ind}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',f'{ind}    proxy_set_header X-Forwarded-Proto https;',f'{ind}}}',
      f'{ind}location ^~ /assistant/ {{',f'{ind}    proxy_pass http://127.0.0.1:18012/;',f'{ind}    proxy_http_version 1.1;',f'{ind}    proxy_set_header Host $host;',f'{ind}    proxy_set_header X-Real-IP $remote_addr;',f'{ind}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',f'{ind}    proxy_set_header X-Forwarded-Proto https;',f'{ind}}}',
    ]; lines[b:b]=extra
p.write_text('\n'.join(lines).rstrip()+'\n')
PY

if ! cmp -s "$TMP_WEB" <(sudo cat "$WEB_CONFIG"); then sudo cp "$TMP_WEB" "$WEB_CONFIG"; NGINX_CHANGED=1; fi
if ! cmp -s "$TMP_API" <(sudo cat "$API_CONFIG"); then sudo cp "$TMP_API" "$API_CONFIG"; NGINX_CHANGED=1; fi
rm -f "$TMP_WEB" "$TMP_API"
sudo nginx -t; sudo systemctl reload nginx; log "NGINX_CUTOVER=PASS"

log ""
log "===== 5. PUBLIC CERTIFICATION ====="
wait_http public-web "https://$WEB_HOST/auth/login" 30 || fail "public web failed"
wait_http public-director "https://$API_HOST/director/api/health" 20 || fail "public Director failed"
wait_http public-assistant "https://$API_HOST/assistant/api/health" 20 || fail "public Assistant failed"
curl -fsS "https://$API_HOST/director/api/health" > /tmp/df-public-director.json
curl -fsS "https://$API_HOST/assistant/api/health" > /tmp/df-public-assistant.json
python3 - <<'PY'
import json
for path in ('/tmp/df-public-director.json','/tmp/df-public-assistant.json'):
    data=json.load(open(path))
    if data.get('runtime_ready') is not True: raise SystemExit(f"FAIL: public runtime_ready=false: {data}")
print('PUBLIC_DIRECTOR_ASSISTANT_RUNTIME=PASS')
PY
log "PUBLIC_PRODUCTION_CERTIFICATION=PASS"

NGINX_CHANGED=0
trap - EXIT
log "============================================================"
log " POST-MIGRATION PRODUCTION RECOVERY PASS"
log "============================================================"
log "POST_MIGRATION_RECOVERY=PASS"
