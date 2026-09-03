#!/usr/bin/env bash
set -Eeuo pipefail

BRIDGE_HOST="${BRIDGE_HOST:-dev-api.desifaces.ai}"
BRIDGE_PREFIX="${BRIDGE_PREFIX:-/__v3web__/}"
TARGET="${TARGET:-http://127.0.0.1:13000}"
PUBLIC_HOST="${PUBLIC_HOST:-web.desifaces.ai}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CONFIG=""
BACKUP=""
CHANGED=0
TMP_SOURCE=""
TMP_EDIT=""

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
for x in sudo nginx curl openssl python3 grep cp cmp mktemp; do need "$x"; done

cleanup_files(){
  [[ -z "$TMP_SOURCE" ]] || rm -f "$TMP_SOURCE" || true
  [[ -z "$TMP_EDIT" ]] || rm -f "$TMP_EDIT" || true
}

rollback(){
  local rc=$?
  set +e
  if (( rc != 0 && CHANGED == 1 )) && [[ -n "$BACKUP" && -n "$CONFIG" ]] && sudo test -f "$BACKUP"; then
    sudo cp "$BACKUP" "$CONFIG" || true
    sudo nginx -t >/dev/null 2>&1 && sudo systemctl reload nginx >/dev/null 2>&1 || true
    log "DEV_API_V3_BRIDGE_ROLLBACK=ATTEMPTED backup=$BACKUP"
  fi
  cleanup_files
  exit "$rc"
}
trap rollback EXIT

[[ "$BRIDGE_PREFIX" == /*/ ]] || fail "BRIDGE_PREFIX must start and end with /"

log "============================================================"
log " desifaces V3 — PREPARE DEV-API HTTPS PATH BRIDGE"
log "============================================================"
log "bridge=https://${BRIDGE_HOST}${BRIDGE_PREFIX} -> ${TARGET}/"

code="$(curl -sS --max-time 20 -o /tmp/df-v3-bridge-local.html -w '%{http_code}' "$TARGET/auth/login")"
[[ "$code" == "200" ]] || fail "V3 web is not healthy at $TARGET/auth/login (HTTP $code)"
grep -qi 'desifaces' /tmp/df-v3-bridge-local.html || fail "V3 web response missing desifaces branding"
log "LOCAL_V3_WEB=PASS"

PREFERRED="/etc/nginx/sites-enabled/desifaces-dev"
if sudo test -f "$PREFERRED" && sudo grep -Eq "server_name[^;]*${BRIDGE_HOST//./\\.}([[:space:];]|$)" "$PREFERRED"; then
  CONFIG="$PREFERRED"
else
  mapfile -t FILES < <(sudo grep -RIl -E "server_name[^;]*${BRIDGE_HOST//./\\.}([[:space:];]|$)" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null \
    | grep -Ev '(\.bak$|\.backup$|\.before-|\.pre-|~$)' \
    | sort -u)
  ((${#FILES[@]} == 1)) || fail "expected exactly one active nginx file containing $BRIDGE_HOST; found ${#FILES[@]}"
  CONFIG="${FILES[0]}"
fi
log "nginx_file=$CONFIG"

TMP_SOURCE="$(mktemp)"
sudo cat "$CONFIG" > "$TMP_SOURCE"

DISCOVERY="$(python3 - "$TMP_SOURCE" "$BRIDGE_HOST" <<'PY'
from pathlib import Path
import re, sys
p=Path(sys.argv[1]); host=sys.argv[2]
lines=p.read_text().splitlines()
blocks=[]; start=None; depth=0
for i,line in enumerate(lines):
    if start is None and re.match(r'^\s*server\s*\{', line):
        start=i; depth=line.count('{')-line.count('}')
        continue
    if start is not None:
        depth += line.count('{')-line.count('}')
        if depth == 0:
            blocks.append((start,i)); start=None
hits=[]
for a,b in blocks:
    text='\n'.join(lines[a:b+1])
    if re.search(rf'(?m)^\s*server_name\b[^;]*\b{re.escape(host)}\b[^;]*;', text) and re.search(r'(?m)^\s*listen\s+(?:\[::\]:)?443\b', text):
        hits.append((a,b,text))
if len(hits) != 1:
    raise SystemExit(f'expected exactly one HTTPS server block for {host}, found {len(hits)}')
_,_,text=hits[0]
cm=re.search(r'(?m)^\s*ssl_certificate\s+([^;]+);', text)
km=re.search(r'(?m)^\s*ssl_certificate_key\s+([^;]+);', text)
if not cm or not km:
    raise SystemExit('HTTPS server block does not declare certificate and key')
print(cm.group(1).strip()+'|'+km.group(1).strip())
PY
)" || fail "could not discover active HTTPS server/certificate for $BRIDGE_HOST"
IFS='|' read -r CERT KEY <<< "$DISCOVERY"
[[ -n "$CERT" && -n "$KEY" ]] || fail "empty TLS discovery result for $BRIDGE_HOST"
sudo test -e "$CERT" || fail "active TLS certificate path is missing: $CERT"
sudo test -e "$KEY" || fail "active TLS key path is missing: $KEY"
sudo openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null | grep -Fq "DNS:$BRIDGE_HOST" || fail "$CERT does not cover $BRIDGE_HOST"
log "DEV_API_TLS=PASS cert=$CERT"

BACKUP_DIR="/var/backups/desifaces-nginx"
sudo mkdir -p "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/$(basename "$CONFIG").pre-v3-path-bridge-${STAMP}"
sudo cp "$CONFIG" "$BACKUP"
log "backup=$BACKUP"

TMP_EDIT="$(mktemp)"
cp "$TMP_SOURCE" "$TMP_EDIT"
python3 - "$TMP_EDIT" "$BRIDGE_HOST" "$BRIDGE_PREFIX" "$TARGET" "$PUBLIC_HOST" <<'PY'
from pathlib import Path
import re, sys
p=Path(sys.argv[1]); host=sys.argv[2]; prefix=sys.argv[3]; target=sys.argv[4].rstrip('/'); public=sys.argv[5]
s=p.read_text(); lines=s.splitlines()
marker='DESIFACES_V3_WEB_PATH_BRIDGE_20260903'
blocks=[]; start=None; depth=0
for i,line in enumerate(lines):
    if start is None and re.match(r'^\s*server\s*\{', line):
        start=i; depth=line.count('{')-line.count('}')
        continue
    if start is not None:
        depth += line.count('{')-line.count('}')
        if depth == 0:
            blocks.append((start,i)); start=None
hits=[]
for a,b in blocks:
    text='\n'.join(lines[a:b+1])
    if re.search(rf'(?m)^\s*server_name\b[^;]*\b{re.escape(host)}\b[^;]*;', text) and re.search(r'(?m)^\s*listen\s+(?:\[::\]:)?443\b', text):
        hits.append((a,b))
if len(hits) != 1:
    raise SystemExit(f'expected exactly one HTTPS server block for {host}, found {len(hits)}')
a,b=hits[0]
block='\n'.join(lines[a:b+1])
if marker in block:
    print('DEV_API_V3_PATH_BRIDGE_ALREADY_PRESENT=YES')
    raise SystemExit(0)
if prefix in block:
    raise SystemExit(f'bridge prefix already exists without expected marker: {prefix}')
indent='    '
bridge=[
    f'{indent}# {marker}',
    f'{indent}location ^~ {prefix} {{',
    f'{indent}    proxy_pass {target}/;',
    f'{indent}    proxy_http_version 1.1;',
    f'{indent}    proxy_read_timeout 600s;',
    f'{indent}    proxy_send_timeout 600s;',
    f'{indent}    proxy_set_header Host {public};',
    f'{indent}    proxy_set_header X-Real-IP $remote_addr;',
    f'{indent}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
    f'{indent}    proxy_set_header X-Forwarded-Proto https;',
    f'{indent}    proxy_set_header X-Forwarded-Host {public};',
    f'{indent}    proxy_set_header Upgrade $http_upgrade;',
    f'{indent}    proxy_set_header Connection "upgrade";',
    f'{indent}}}',
    '',
]
lines[b:b]=bridge
p.write_text('\n'.join(lines).rstrip()+'\n')
print(f'DEV_API_V3_PATH_BRIDGE_ADDED={prefix}')
PY

if cmp -s "$TMP_EDIT" "$TMP_SOURCE"; then
  log "DEV_API_V3_PATH_BRIDGE_CHANGE=NOT_REQUIRED"
else
  sudo cp "$TMP_EDIT" "$CONFIG"
  CHANGED=1
  sudo nginx -t
  sudo systemctl reload nginx
  log "DEV_API_V3_PATH_BRIDGE_CHANGE=APPLIED"
fi

BRIDGE_URL="https://${BRIDGE_HOST}${BRIDGE_PREFIX}auth/login"
code="$(curl -sS --max-time 30 --resolve "$BRIDGE_HOST:443:127.0.0.1" -o /tmp/df-v3-bridge-https.html -w '%{http_code}' "$BRIDGE_URL")"
[[ "$code" == "200" ]] || fail "local HTTPS path bridge returned HTTP $code"
grep -qi 'desifaces' /tmp/df-v3-bridge-https.html || fail "HTTPS path bridge response missing desifaces branding"
log "DEV_API_V3_PATH_BRIDGE=PASS url=$BRIDGE_URL"

CHANGED=0
cleanup_files
trap - EXIT
exit 0
