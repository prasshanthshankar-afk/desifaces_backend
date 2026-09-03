#!/usr/bin/env bash
set -Eeuo pipefail

PUBLIC_HOST="${PUBLIC_HOST:-web.desifaces.ai}"
UPSTREAM_HOST="${UPSTREAM_HOST:-dev-app.desifaces.ai}"
UPSTREAM_URL="https://${UPSTREAM_HOST}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CONFIG=""
BACKUP=""
CHANGED=0

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
for x in sudo nginx curl python3 grep cp cmp; do need "$x"; done

rollback(){
  local rc=$?
  set +e
  if (( rc != 0 && CHANGED == 1 )) && [[ -n "$BACKUP" && -n "$CONFIG" ]] && sudo test -f "$BACKUP"; then
    sudo cp "$BACKUP" "$CONFIG" || true
    sudo nginx -t >/dev/null 2>&1 && sudo systemctl reload nginx >/dev/null 2>&1 || true
    log "PUBLIC_WEB_ROLLBACK=ATTEMPTED backup=$BACKUP"
  fi
  exit "$rc"
}
trap rollback EXIT

log "============================================================"
log " desifaces.ai — PUBLIC WEB CUTOVER TO V3"
log "============================================================"
log "public_host=$PUBLIC_HOST upstream=$UPSTREAM_URL"

code="$(curl -sS --max-time 20 -o /tmp/df-v3-upstream-precheck.html -w '%{http_code}' "$UPSTREAM_URL/auth/login")"
[[ "$code" == "200" ]] || fail "$UPSTREAM_HOST bridge is not reachable (HTTP $code)"
grep -qi 'desifaces' /tmp/df-v3-upstream-precheck.html || fail "upstream bridge response missing desifaces branding"
log "V3_UPSTREAM_BRIDGE=PASS"

mapfile -t FILES < <(sudo grep -RIl -E "server_name[[:space:]]+${PUBLIC_HOST//./\\.}([[:space:];]|$)" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null \
  | grep -Ev '(\.bak$|\.backup$|\.before-|\.pre-|~$)' \
  | sort -u)
((${#FILES[@]} >= 1)) || fail "could not locate active nginx config for $PUBLIC_HOST"
for f in "${FILES[@]}"; do
  if sudo grep -q 'proxy_pass' "$f" && sudo grep -Eq "server_name[[:space:]]+${PUBLIC_HOST//./\\.}[[:space:]]*;" "$f"; then
    CONFIG="$f"; break
  fi
done
[[ -n "$CONFIG" ]] || fail "could not identify the HTTPS proxy file for $PUBLIC_HOST"

BACKUP_DIR="/var/backups/desifaces-nginx"
sudo mkdir -p "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/$(basename "$CONFIG").pre-v3-public-web-${STAMP}"
sudo cp "$CONFIG" "$BACKUP"
log "nginx_file=$CONFIG"
log "backup=$BACKUP"

TMP="$(mktemp)"
sudo cat "$CONFIG" > "$TMP"
python3 - "$TMP" "$PUBLIC_HOST" "$UPSTREAM_HOST" <<'PY'
from pathlib import Path
import re, sys
p=Path(sys.argv[1]); public=sys.argv[2]; upstream=sys.argv[3]
s=p.read_text()
marker='DESIFACES_V3_PUBLIC_WEB_CUTOVER_20260903'
if marker in s:
    print('PUBLIC_V3_CUTOVER_ALREADY_PRESENT=YES')
    raise SystemExit(0)

lines=s.splitlines()
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
    if re.search(rf'(?m)^\s*server_name\s+{re.escape(public)}\s*;', text) and re.search(r'(?m)^\s*listen\s+443\b', text):
        hits.append((a,b))
if len(hits)!=1:
    raise SystemExit(f'expected exactly one HTTPS {public} server block, found {len(hits)}')
a,b=hits[0]
block=lines[a:b+1]
proxy_idxs=[i for i,l in enumerate(block) if re.search(r'^\s*proxy_pass\s+', l)]
if len(proxy_idxs)!=1:
    raise SystemExit(f'expected exactly one proxy_pass in {public} HTTPS block, found {len(proxy_idxs)}')
pi=proxy_idxs[0]
indent=block[pi][:len(block[pi])-len(block[pi].lstrip())]
old=block[pi].strip()
if not (old.startswith('proxy_pass http://df_web') or '127.0.0.1:3000' in old):
    raise SystemExit(f'unexpected existing web upstream: {old}')
block[pi]=f'{indent}proxy_pass https://{upstream};'

host_idxs=[i for i,l in enumerate(block) if re.search(r'^\s*proxy_set_header\s+Host\s+', l)]
if host_idxs:
    hi=host_idxs[0]; hindent=block[hi][:len(block[hi])-len(block[hi].lstrip())]
    block[hi]=f'{hindent}proxy_set_header Host {upstream};'
    insert_at=hi+1
else:
    insert_at=pi+1
extra=[
    f'{indent}# {marker}',
    f'{indent}proxy_ssl_server_name on;',
    f'{indent}proxy_ssl_name {upstream};',
    f'{indent}proxy_ssl_verify on;',
    f'{indent}proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;',
    f'{indent}proxy_set_header X-Forwarded-Host $host;',
]
block[insert_at:insert_at]=extra
lines[a:b+1]=block
p.write_text('\n'.join(lines).rstrip()+'\n')
print(f'PUBLIC_WEB_UPSTREAM=https://{upstream}')
PY

if cmp -s "$TMP" <(sudo cat "$CONFIG"); then
  log "PUBLIC_WEB_CHANGE=NOT_REQUIRED"
else
  sudo cp "$TMP" "$CONFIG"
  CHANGED=1
  sudo nginx -t
  sudo systemctl reload nginx
  log "PUBLIC_WEB_CHANGE=APPLIED"
fi
rm -f "$TMP"

code="$(curl -sS --max-time 30 -o /tmp/df-v3-public-web.html -w '%{http_code}' "https://$PUBLIC_HOST/auth/login")"
[[ "$code" == "200" ]] || fail "public $PUBLIC_HOST returned HTTP $code after cutover"
grep -qi 'desifaces' /tmp/df-v3-public-web.html || fail "public response missing desifaces branding"
log "PUBLIC_WEB_V3=PASS url=https://$PUBLIC_HOST/auth/login"

CHANGED=0
trap - EXIT
exit 0
