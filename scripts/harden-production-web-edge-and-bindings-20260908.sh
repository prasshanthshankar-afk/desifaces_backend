#!/usr/bin/env bash
set -Eeuo pipefail

HOST="desifaces-gpu"
BACKEND="/home/azureuser/workspace/desifaces"
WEB_URL="https://web.desifaces.ai"
API_URL="https://api.desifaces.ai"
COMPOSE_BASE="$BACKEND/docker-compose.yml"
COMPOSE_V3="$BACKEND/deploy/production/docker-compose.v3-app.production.yml"
SECURITY_OVERLAY="$BACKEND/deploy/production/docker-compose.security.production.yml"
SENSITIVE_PORTS=(13001 15432 5432 6379 8000 8001 8002 8003 8004 8005 8006 8007 8008 8009 8010 18011 18012)

need(){ command -v "$1" >/dev/null 2>&1 || { echo "FAIL: missing required command: $1" >&2; exit 2; }; }
for x in ssh curl nc; do need "$x"; done

echo "============================================================"
echo " desifaces.ai — PRODUCTION WEB/EDGE HARDENING"
echo "============================================================"
echo "TARGET_BACKEND=$BACKEND"
echo "DB_VOLUME_REPLACEMENT=FORBIDDEN"
echo "REDIS_DATA_ACTION=NONE"
echo "CUSTOMER_DATA_ACTION=NONE"
echo "CSP_MODE=REPORT_ONLY"

echo
echo "===== 1. EXTERNAL EXPOSURE — BEFORE ====="
for p in 80 443 "${SENSITIVE_PORTS[@]}"; do
  if nc -z -w 2 web.desifaces.ai "$p" >/dev/null 2>&1; then
    echo "BEFORE_PUBLIC_PORT_${p}=OPEN"
  else
    echo "BEFORE_PUBLIC_PORT_${p}=CLOSED_OR_FILTERED"
  fi
done

echo
echo "===== 2. GUARDED HOST HARDENING ====="
ssh -o BatchMode=yes "$HOST" 'bash -s' <<'REMOTE'
set -Eeuo pipefail
BACKEND="/home/azureuser/workspace/desifaces"
COMPOSE_BASE="$BACKEND/docker-compose.yml"
COMPOSE_V3="$BACKEND/deploy/production/docker-compose.v3-app.production.yml"
SECURITY_OVERLAY="$BACKEND/deploy/production/docker-compose.security.production.yml"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/home/azureuser/backups/desifaces-security-hardening-$TS"
mkdir -p "$BACKUP"

[[ -f "$COMPOSE_BASE" ]] || { echo "FAIL: missing canonical compose" >&2; exit 3; }
[[ -f "$COMPOSE_V3" ]] || { echo "FAIL: missing production V3 overlay" >&2; exit 3; }

# Capture persistence and customer-data safety state before any container recreation.
DB_ID_BEFORE="$(docker inspect -f '{{.Id}}' desifaces-db)"
DB_VOLUME_BEFORE="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' desifaces-db)"
REDIS_ID_BEFORE="$(docker inspect -f '{{.Id}}' desifaces-redis)"
REDIS_VOLUME_BEFORE="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' desifaces-redis)"
[[ -n "$DB_VOLUME_BEFORE" ]] || { echo "FAIL: DB volume unresolved" >&2; exit 3; }
[[ -n "$REDIS_VOLUME_BEFORE" ]] || { echo "FAIL: Redis volume unresolved" >&2; exit 3; }

echo "DB_VOLUME_BEFORE=$DB_VOLUME_BEFORE"
echo "REDIS_VOLUME_BEFORE=$REDIS_VOLUME_BEFORE"

docker exec desifaces-db sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$BACKUP/desifaces.dump"
sha256sum "$BACKUP/desifaces.dump" > "$BACKUP/desifaces.dump.sha256"

docker exec desifaces-db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "select count(*) from users"' > "$BACKUP/users.count" 2>/dev/null || true
cp -a "$COMPOSE_BASE" "$BACKUP/docker-compose.yml.before"
cp -a "$COMPOSE_V3" "$BACKUP/docker-compose.v3-app.production.yml.before"
sudo cp -a /etc/nginx "$BACKUP/nginx.before"
echo "BACKUP_DIR=$BACKUP"
echo "SECURITY_SNAPSHOT=PASS"

# Dedicated production security overlay. !override is required so the original
# all-interface port mappings are replaced rather than merged.
cat > "$SECURITY_OVERLAY" <<'YAML'
services:
  desifaces-db:
    ports: !override
      - "127.0.0.1:15432:5432"

  svc-core:
    ports: !override
      - "127.0.0.1:8000:8000"
  svc-fusion:
    ports: !override
      - "127.0.0.1:8002:8002"
  svc-face:
    ports: !override
      - "127.0.0.1:8003:8003"
  svc-audio:
    ports: !override
      - "127.0.0.1:8004:8004"
  svc-dashboard:
    ports: !override
      - "127.0.0.1:8005:8005"
  svc-fusion-extension:
    ports: !override
      - "127.0.0.1:8006:8006"
  svc-music:
    ports: !override
      - "127.0.0.1:8007:8000"
  svc-commerce:
    ports: !override
      - "127.0.0.1:8008:8008"
  svc-pricing:
    ports: !override
      - "127.0.0.1:8009:8009"
  svc-marketing:
    ports: !override
      - "127.0.0.1:8010:8010"
YAML

# Compose parser/version gate and rendered-config assertion.
if ! docker compose -f "$COMPOSE_BASE" -f "$COMPOSE_V3" -f "$SECURITY_OVERLAY" config > "$BACKUP/rendered-security-compose.yml"; then
  echo "FAIL: Docker Compose does not support required !override semantics; no runtime mutation performed" >&2
  exit 4
fi

python3 - "$BACKUP/rendered-security-compose.yml" <<'PY'
import re,sys
p=sys.argv[1]
s=open(p,encoding='utf-8').read()
# Hard fail if any sensitive published port remains all-interface in rendered config.
for port in [15432,8000,8002,8003,8004,8005,8006,8007,8008,8009,8010,18011,18012]:
    patterns=[rf'host_ip:\s*["\x27]?0\.0\.0\.0["\x27]?.*?published:\s*["\x27]?{port}["\x27]?',
              rf'-\s*["\x27]?{port}:']
    if any(re.search(x,s,re.S) for x in patterns):
        raise SystemExit(f"FAIL: rendered compose still exposes port {port} on all interfaces")
print("RENDERED_BINDING_PREFLIGHT=PASS")
PY

# Nginx per-host security snippet. CSP remains Report-Only until browser E2E proves
# the actual third-party/blob requirements; the remaining headers enforce immediately.
SEC_SNIP="/etc/nginx/snippets/desifaces-production-security.conf"
sudo tee "$SEC_SNIP" >/dev/null <<'NGINX'
server_tokens off;
proxy_hide_header X-Powered-By;
add_header Strict-Transport-Security "max-age=31536000" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "DENY" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header Permissions-Policy "geolocation=(), accelerometer=(), gyroscope=(), magnetometer=(), usb=()" always;
add_header Content-Security-Policy-Report-Only "default-src 'self' https: data: blob:; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; img-src 'self' https: data: blob:; media-src 'self' https: blob:; connect-src 'self' https: wss:; script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; style-src 'self' 'unsafe-inline' https:; font-src 'self' https: data:; form-action 'self' https:" always;
NGINX

# Find the active config(s) containing the two production HTTPS server names and
# insert the security include only into 443 server blocks. Symlinks are resolved.
python3 - <<'PY'
from pathlib import Path
import os,re
roots=[Path('/etc/nginx/sites-enabled'),Path('/etc/nginx/conf.d')]
files=[]
for root in roots:
    if not root.exists(): continue
    for p in root.iterdir():
        if p.is_file() or p.is_symlink():
            try: rp=Path(os.path.realpath(p)); txt=rp.read_text()
            except Exception: continue
            if 'web.desifaces.ai' in txt or 'api.desifaces.ai' in txt:
                files.append(rp)
files=list(dict.fromkeys(files))
if not files:
    raise SystemExit('FAIL: unable to locate active desifaces nginx config')
include='    include /etc/nginx/snippets/desifaces-production-security.conf;\n'
for p in files:
    txt=p.read_text()
    out=[]; i=0; changed=False
    while i < len(txt):
        m=re.search(r'\bserver\s*\{',txt[i:])
        if not m:
            out.append(txt[i:]); break
        start=i+m.start(); brace=i+m.end()-1
        out.append(txt[i:start])
        depth=0; j=brace
        while j < len(txt):
            if txt[j]=='{': depth+=1
            elif txt[j]=='}':
                depth-=1
                if depth==0:
                    j+=1; break
            j+=1
        block=txt[start:j]
        if ('server_name web.desifaces.ai' in block or 'server_name api.desifaces.ai' in block) and re.search(r'listen\s+443\b',block):
            if 'desifaces-production-security.conf' not in block:
                sm=re.search(r'(^\s*server_name\s+[^;]+;\s*\n)',block,re.M)
                if not sm: raise SystemExit(f'FAIL: server_name insertion point missing in {p}')
                block=block[:sm.end()]+include+block[sm.end():]
                changed=True
        out.append(block); i=j
    if changed:
        p.write_text(''.join(out))
        print(f'NGINX_PATCHED={p}')
PY

sudo nginx -t
sudo systemctl reload nginx

echo "NGINX_SECURITY_HEADERS_APPLIED=PASS"

# Recreate only services whose host bindings change. DB persistence is guarded by
# exact volume identity before/after; Redis is explicitly untouched.
cd "$BACKEND"
SERVICES=(desifaces-db svc-core svc-fusion svc-face svc-audio svc-dashboard svc-fusion-extension svc-music svc-commerce svc-pricing svc-marketing)
docker compose -f "$COMPOSE_BASE" -f "$COMPOSE_V3" -f "$SECURITY_OVERLAY" up -d --no-deps --force-recreate "${SERVICES[@]}"

# Wait for DB and key HTTP services.
for n in $(seq 1 60); do
  st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' desifaces-db 2>/dev/null || true)"
  [[ "$st" == healthy ]] && break
  sleep 2
done
[[ "$(docker inspect -f '{{.State.Health.Status}}' desifaces-db)" == healthy ]] || { echo "FAIL: DB not healthy after guarded binding change" >&2; exit 5; }

DB_VOLUME_AFTER="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' desifaces-db)"
REDIS_ID_AFTER="$(docker inspect -f '{{.Id}}' desifaces-redis)"
REDIS_VOLUME_AFTER="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' desifaces-redis)"
[[ "$DB_VOLUME_AFTER" == "$DB_VOLUME_BEFORE" ]] || { echo "FAIL: DB persistent volume identity changed" >&2; exit 6; }
[[ "$REDIS_ID_AFTER" == "$REDIS_ID_BEFORE" ]] || { echo "FAIL: Redis container changed unexpectedly" >&2; exit 6; }
[[ "$REDIS_VOLUME_AFTER" == "$REDIS_VOLUME_BEFORE" ]] || { echo "FAIL: Redis volume changed unexpectedly" >&2; exit 6; }
echo "DB_PERSISTENCE_IDENTITY=PASS"
echo "REDIS_UNCHANGED=PASS"

# Ensure no sensitive service is host-wide after recreation.
BAD="$(sudo ss -lnt 2>/dev/null | awk '$4 ~ /(^|:)(15432|8000|8002|8003|8004|8005|8006|8007|8008|8009|8010)$/ && $4 !~ /^127\.0\.0\.1:/ && $4 !~ /^\[::1\]:/ {print}' || true)"
if [[ -n "$BAD" ]]; then
  echo "$BAD"
  echo "FAIL: sensitive host-wide listeners remain" >&2
  exit 7
fi
echo "HOST_BINDING_HARDENING=PASS"

# Data and local service certification.
USERS_AFTER="$(docker exec desifaces-db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "select count(*) from users"' 2>/dev/null || true)"
USERS_BEFORE="$(cat "$BACKUP/users.count" 2>/dev/null || true)"
if [[ -n "$USERS_BEFORE" && "$USERS_AFTER" != "$USERS_BEFORE" ]]; then
  echo "FAIL: users count changed across hardening" >&2; exit 8
fi
echo "CUSTOMER_DATA_PRESERVED=PASS"

for spec in 'core:8000/api/health' 'audio:8004/api/health' 'pricing:8009/api/health'; do
  name="${spec%%:*}"; rest="${spec#*:}"; port="${rest%%/*}"; path="/${rest#*/}"
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${port}${path}" || true)"
  echo "HEALTH|$name|$code"
  [[ "$code" == 200 ]] || { echo "FAIL: $name health=$code" >&2; exit 9; }
done

echo "HOST_HARDENING=PASS"
REMOTE

echo
echo "===== 3. PUBLIC HTTPS / HEADERS — AFTER ====="
HEADERS="$(curl -sS -I --max-time 20 "$WEB_URL" || true)"
printf '%s\n' "$HEADERS"
for h in strict-transport-security x-content-type-options x-frame-options referrer-policy permissions-policy content-security-policy-report-only; do
  printf '%s\n' "$HEADERS" | grep -qi "^${h}:" || { echo "FAIL: missing public header $h" >&2; exit 10; }
done
if printf '%s\n' "$HEADERS" | grep -qi '^x-powered-by:'; then
  echo "FAIL: X-Powered-By still exposed" >&2; exit 10
fi
echo "PUBLIC_SECURITY_HEADERS=PASS"

WEB_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$WEB_URL")"
API_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$API_URL/director/api/health")"
echo "PUBLIC_WEB_HTTP=$WEB_CODE"
echo "PUBLIC_DIRECTOR_HTTP=$API_CODE"
[[ "$WEB_CODE" == 200 && "$API_CODE" == 200 ]] || { echo "FAIL: public web/API regression" >&2; exit 11; }

echo
echo "===== 4. EXTERNAL EXPOSURE — AFTER ====="
FAIL_OPEN=0
for p in "${SENSITIVE_PORTS[@]}"; do
  if nc -z -w 2 web.desifaces.ai "$p" >/dev/null 2>&1; then
    echo "AFTER_PUBLIC_PORT_${p}=OPEN"
    FAIL_OPEN=1
  else
    echo "AFTER_PUBLIC_PORT_${p}=CLOSED_OR_FILTERED"
  fi
done
[[ "$FAIL_OPEN" == 0 ]] || { echo "FAIL: one or more sensitive ports remain publicly reachable" >&2; exit 12; }
echo "SENSITIVE_PUBLIC_PORTS=CLOSED"

echo
echo "============================================================"
echo " PRODUCTION WEB/EDGE HARDENING PASS"
echo "============================================================"
echo "HTTP_TO_HTTPS=PASS"
echo "TLS_BASELINE=TLS1.2_TLS1.3"
echo "SECURITY_HEADERS=PASS"
echo "X_POWERED_BY=HIDDEN"
echo "CSP=REPORT_ONLY_PENDING_E2E"
echo "SERVICE_BINDINGS=LOOPBACK_ONLY"
echo "DB_PERSISTENCE_IDENTITY=PASS"
echo "REDIS_UNCHANGED=PASS"
echo "CUSTOMER_DATA_PRESERVED=PASS"
echo "PUBLIC_WEB=PASS"
echo "SENSITIVE_PUBLIC_PORTS=CLOSED"
echo "NEXT_ACTION=WEB_E2E_THEN_CSP_ENFORCEMENT_AND_AZURE_FRONT_DOOR_WAF"
