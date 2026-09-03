#!/usr/bin/env bash
set -Eeuo pipefail

HOST="${HOST:-dev-app.desifaces.ai}"
TARGET="${TARGET:-http://127.0.0.1:13000}"
CERT="${CERT:-}"
KEY="${KEY:-}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CONFIG=""
BACKUP=""
CHANGED=0

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
for x in sudo nginx curl openssl python3 grep cp awk cmp; do need "$x"; done

rollback(){
  local rc=$?
  set +e
  if (( rc != 0 && CHANGED == 1 )) && [[ -n "$BACKUP" && -n "$CONFIG" ]] && sudo test -f "$BACKUP"; then
    sudo cp "$BACKUP" "$CONFIG" || true
    sudo nginx -t >/dev/null 2>&1 && sudo systemctl reload nginx >/dev/null 2>&1 || true
    log "DEV_APP_BRIDGE_ROLLBACK=ATTEMPTED backup=$BACKUP"
  fi
  exit "$rc"
}
trap rollback EXIT

log "============================================================"
log " desifaces V3 — PREPARE DEV-APP HTTPS WEB BRIDGE"
log "============================================================"
log "host=$HOST target=$TARGET"

code="$(curl -sS --max-time 20 -o /tmp/df-v3-bridge-local.html -w '%{http_code}' "$TARGET/auth/login")"
[[ "$code" == "200" ]] || fail "V3 web is not healthy at $TARGET/auth/login (HTTP $code)"
grep -qi 'desifaces' /tmp/df-v3-bridge-local.html || fail "V3 web response missing desifaces branding"
log "LOCAL_V3_WEB=PASS"

PREFERRED="/etc/nginx/sites-enabled/desifaces-dev"
if sudo test -f "$PREFERRED" && sudo grep -Eq "server_name[^;]*${HOST//./\\.}([[:space:];]|$)" "$PREFERRED"; then
  CONFIG="$PREFERRED"
else
  mapfile -t FILES < <(sudo grep -RIl -E "server_name[^;]*${HOST//./\\.}([[:space:];]|$)" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null \
    | grep -Ev '(\.bak$|\.backup$|\.before-|\.pre-|~$)' \
    | sort -u)
  ((${#FILES[@]} == 1)) || fail "expected exactly one active nginx file containing $HOST; found ${#FILES[@]}"
  CONFIG="${FILES[0]}"
fi
log "nginx_file=$CONFIG"

if [[ -z "$CERT" ]]; then
  CERT="$(sudo awk '/^[[:space:]]*ssl_certificate[[:space:]]+/ && $1 !~ /_key$/ {gsub(/;/,"",$2); print $2; exit}' "$CONFIG")"
fi
if [[ -z "$KEY" ]]; then
  KEY="$(sudo awk '/^[[:space:]]*ssl_certificate_key[[:space:]]+/ {gsub(/;/,"",$2); print $2; exit}' "$CONFIG")"
fi
[[ -n "$CERT" && -n "$KEY" ]] || fail "could not discover TLS certificate/key from $CONFIG"
sudo test -e "$CERT" || fail "active TLS certificate path is missing: $CERT"
sudo test -e "$KEY" || fail "active TLS key path is missing: $KEY"
sudo openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null | grep -Fq "DNS:$HOST" || fail "$CERT does not cover $HOST"
log "DEV_APP_TLS_SAN=PASS cert=$CERT"

BACKUP_DIR="/var/backups/desifaces-nginx"
sudo mkdir -p "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/$(basename "$CONFIG").pre-v3-web-bridge-${STAMP}"
sudo cp "$CONFIG" "$BACKUP"
log "backup=$BACKUP"

TMP="$(mktemp)"
sudo cat "$CONFIG" > "$TMP"
python3 - "$TMP" "$HOST" "$CERT" "$KEY" "$TARGET" <<'PY'
from pathlib import Path
import re, sys
p=Path(sys.argv[1]); host=sys.argv[2]; cert=sys.argv[3]; key=sys.argv[4]; target=sys.argv[5]
s=p.read_text()
marker='DESIFACES_V3_WEB_BRIDGE_20260903'
if marker in s:
    print('DEV_APP_BRIDGE_ALREADY_PRESENT=YES')
    raise SystemExit(0)

changed=0
out=[]
for line in s.splitlines():
    if re.search(r'^\s*server_name\b', line) and host in line:
        before=line
        prefix=line[:len(line)-len(line.lstrip())]
        body=line.strip()
        if not body.endswith(';'):
            raise SystemExit('unexpected server_name syntax')
        names=body[len('server_name'):].strip()[:-1].split()
        names=[n for n in names if n != host]
        if names:
            line=f"{prefix}server_name {' '.join(names)};"
        else:
            raise SystemExit('refusing to remove the only server_name from an existing block')
        if line != before:
            changed += 1
    out.append(line)
if changed < 1:
    raise SystemExit(f'no existing server_name reference to {host} was removed')
s='\n'.join(out).rstrip()+"\n"
bridge=f'''\n# {marker}\nserver {{\n    listen 443 ssl http2;\n    listen [::]:443 ssl http2;\n    server_name {host};\n\n    ssl_certificate {cert};\n    ssl_certificate_key {key};\n    include /etc/letsencrypt/options-ssl-nginx.conf;\n    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;\n\n    client_max_body_size 200M;\n    proxy_read_timeout 600s;\n    proxy_send_timeout 600s;\n\n    location / {{\n        proxy_pass {target};\n        proxy_http_version 1.1;\n        proxy_set_header Host $host;\n        proxy_set_header X-Real-IP $remote_addr;\n        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n        proxy_set_header X-Forwarded-Proto https;\n        proxy_set_header Upgrade $http_upgrade;\n        proxy_set_header Connection "upgrade";\n    }}\n}}\n\nserver {{\n    listen 80;\n    listen [::]:80;\n    server_name {host};\n    return 301 https://$host$request_uri;\n}}\n'''
p.write_text(s+bridge)
print(f'DEV_APP_SERVER_NAME_REFERENCES_REMOVED={changed}')
PY

if cmp -s "$TMP" <(sudo cat "$CONFIG"); then
  log "DEV_APP_BRIDGE_CHANGE=NOT_REQUIRED"
else
  sudo cp "$TMP" "$CONFIG"
  CHANGED=1
  sudo nginx -t
  sudo systemctl reload nginx
  log "DEV_APP_BRIDGE_CHANGE=APPLIED"
fi
rm -f "$TMP"

code="$(curl -sS --max-time 30 --resolve "$HOST:443:127.0.0.1" -o /tmp/df-v3-bridge-https.html -w '%{http_code}' "https://$HOST/auth/login")"
[[ "$code" == "200" ]] || fail "local HTTPS bridge returned HTTP $code"
grep -qi 'desifaces' /tmp/df-v3-bridge-https.html || fail "HTTPS bridge response missing desifaces branding"
log "DEV_APP_HTTPS_BRIDGE=PASS url=https://$HOST/auth/login"

CHANGED=0
trap - EXIT
exit 0
