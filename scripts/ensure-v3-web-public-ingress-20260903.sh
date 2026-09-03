#!/usr/bin/env bash
set -Eeuo pipefail

WEB_HOST="${WEB_HOST:-web.desifaces.ai}"
TARGET_PORT="${TARGET_PORT:-13000}"
OLD_PORT="${OLD_PORT:-3000}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP=""
TARGET_FILE=""
CHANGED=0

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
for x in sudo nginx python3 curl grep cp; do need "$x"; done

rollback(){
  local rc=$?
  set +e
  if (( rc != 0 && CHANGED == 1 )) && [[ -n "$BACKUP" && -n "$TARGET_FILE" && -f "$BACKUP" ]]; then
    sudo cp "$BACKUP" "$TARGET_FILE" || true
    sudo nginx -t >/dev/null 2>&1 && sudo systemctl reload nginx >/dev/null 2>&1 || true
    log "NGINX_INGRESS_ROLLBACK=ATTEMPTED backup=$BACKUP"
  fi
  exit "$rc"
}
trap rollback EXIT

log "===== PUBLIC WEB INGRESS CERTIFICATION ====="
log "host=$WEB_HOST target_port=$TARGET_PORT"

# The production web host and certificate must already exist; this script only
# corrects the V3 upstream port. It never creates DNS or a new TLS virtual host.
ACTIVE="$(sudo nginx -T 2>/dev/null)"
printf '%s\n' "$ACTIVE" | grep -Fq "server_name $WEB_HOST" || fail "active nginx does not contain server_name $WEB_HOST"
printf '%s\n' "$ACTIVE" | grep -Fq "/etc/letsencrypt/live/$WEB_HOST/" || fail "active nginx does not reference the existing $WEB_HOST TLS certificate"

mapfile -t CANDIDATES < <(
  sudo grep -RIl --include='*.conf' --include='*' -E 'upstream[[:space:]]+df_web|server_name[[:space:]].*web\.desifaces\.ai' /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null | sort -u
)
((${#CANDIDATES[@]} > 0)) || fail "could not locate active nginx file for $WEB_HOST"

# Locate exactly one enabled/configured file that contains either the df_web
# upstream or the web.desifaces.ai server block. Prefer the upstream owner.
UPSTREAM_FILES=()
for f in "${CANDIDATES[@]}"; do
  if sudo grep -Eq 'upstream[[:space:]]+df_web' "$f"; then UPSTREAM_FILES+=("$f"); fi
done
if ((${#UPSTREAM_FILES[@]} == 1)); then
  TARGET_FILE="${UPSTREAM_FILES[0]}"
elif ((${#UPSTREAM_FILES[@]} > 1)); then
  printf 'candidate=%s\n' "${UPSTREAM_FILES[@]}" >&2
  fail "multiple active df_web upstream definitions found"
else
  # Some installations proxy directly inside the web host instead of declaring
  # df_web. Permit exactly one direct-host file and patch only its web server block.
  HOST_FILES=()
  for f in "${CANDIDATES[@]}"; do
    if sudo grep -Eq 'server_name[[:space:]]+web\.desifaces\.ai([[:space:];]|$)' "$f"; then HOST_FILES+=("$f"); fi
  done
  ((${#HOST_FILES[@]} == 1)) || fail "expected one active $WEB_HOST config file, found ${#HOST_FILES[@]}"
  TARGET_FILE="${HOST_FILES[0]}"
fi

BACKUP="${TARGET_FILE}.pre-v3-web-launch-${STAMP}"
sudo cp "$TARGET_FILE" "$BACKUP"
log "nginx_file=$TARGET_FILE"
log "backup=$BACKUP"

TMP="$(mktemp)"
sudo cat "$TARGET_FILE" > "$TMP"
python3 - "$TMP" "$TARGET_PORT" "$OLD_PORT" <<'PY'
from pathlib import Path
import re, sys
p=Path(sys.argv[1]); target=sys.argv[2]; old=sys.argv[3]
s=p.read_text()

# First choice: narrow mutation inside `upstream df_web { ... }` only.
m=re.search(r'(?ms)^\s*upstream\s+df_web\s*\{(?P<body>.*?)^\s*\}', s)
if m:
    body=m.group('body')
    if re.search(rf'127\.0\.0\.1:{re.escape(target)}\b', body):
        print('INGRESS_ALREADY_TARGET=YES')
        raise SystemExit(0)
    if not re.search(rf'127\.0\.0\.1:{re.escape(old)}\b', body):
        raise SystemExit('df_web upstream is neither expected old nor target port; refusing ambiguous mutation')
    newbody=re.sub(rf'127\.0\.0\.1:{re.escape(old)}\b', f'127.0.0.1:{target}', body, count=1)
    s=s[:m.start('body')]+newbody+s[m.end('body'):]
    p.write_text(s)
    print('INGRESS_MUTATION_MODE=df_web_upstream')
    raise SystemExit(0)

# Fallback: isolate the HTTPS server block for web.desifaces.ai and change one
# direct proxy_pass from 127.0.0.1:3000 to :13000.
blocks=list(re.finditer(r'(?ms)^\s*server\s*\{.*?^\s*\}', s))
hits=[]
for b in blocks:
    text=b.group(0)
    if re.search(r'(?m)^\s*server_name\s+web\.desifaces\.ai\s*;', text) and re.search(r'(?m)^\s*listen\s+443\b', text):
        hits.append(b)
if len(hits)!=1:
    raise SystemExit(f'expected exactly one HTTPS web.desifaces.ai server block, found {len(hits)}')
b=hits[0]; text=b.group(0)
if re.search(rf'127\.0\.0\.1:{re.escape(target)}\b', text):
    print('INGRESS_ALREADY_TARGET=YES')
    raise SystemExit(0)
if not re.search(rf'127\.0\.0\.1:{re.escape(old)}\b', text):
    raise SystemExit('web host proxy is neither expected old nor target port; refusing ambiguous mutation')
newtext=re.sub(rf'127\.0\.0\.1:{re.escape(old)}\b', f'127.0.0.1:{target}', text, count=1)
s=s[:b.start()]+newtext+s[b.end():]
p.write_text(s)
print('INGRESS_MUTATION_MODE=direct_web_server')
PY

if cmp -s "$TMP" <(sudo cat "$TARGET_FILE"); then
  log "NGINX_INGRESS_CHANGE=NOT_REQUIRED"
else
  sudo cp "$TMP" "$TARGET_FILE"
  CHANGED=1
  sudo nginx -t
  sudo systemctl reload nginx
  log "NGINX_INGRESS_CHANGE=APPLIED"
fi
rm -f "$TMP"

# Local target must be healthy before we trust the public proxy.
local_code="$(curl -sS -o /tmp/desifaces-v3-web-local.html -w '%{http_code}' "http://127.0.0.1:${TARGET_PORT}/auth/login")"
[[ "$local_code" == "200" ]] || fail "local V3 web returned HTTP $local_code on port $TARGET_PORT"
grep -qi 'desifaces' /tmp/desifaces-v3-web-local.html || fail "local V3 web response missing desifaces branding"

public_code="$(curl -sS --max-time 20 -o /tmp/desifaces-v3-web-public.html -w '%{http_code}' "https://${WEB_HOST}/auth/login")"
[[ "$public_code" == "200" ]] || fail "public $WEB_HOST returned HTTP $public_code"
grep -qi 'desifaces' /tmp/desifaces-v3-web-public.html || fail "public $WEB_HOST response missing desifaces branding"

log "PUBLIC_WEB_INGRESS=PASS url=https://${WEB_HOST}/auth/login"
CHANGED=0
trap - EXIT
exit 0
