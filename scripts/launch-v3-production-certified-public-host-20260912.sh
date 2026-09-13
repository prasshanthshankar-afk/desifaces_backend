#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-gpu-non-prod"
EXPECTED_PUBLIC_IP="52.252.188.211"
BASE_LAUNCHER_COMMIT="d9948354b69273b26bdbdfda93bc9a03b9a3eba3"
BASE_LAUNCHER_GIT_BLOB="98a355094222c7bbf2780fb522e0ba611fba4869"
BASE_LAUNCHER_URL="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${BASE_LAUNCHER_COMMIT}/scripts/launch-v3-production-direct-on-vm-20260912.sh"
RUN="/tmp/desifaces-certified-public-host-$(date -u +%Y%m%dT%H%M%SZ)"
BASE="$RUN/base-launcher.sh"
PATCHED="$RUN/certified-public-host-launcher.sh"

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }

[[ "${DESIFACES_PRODUCTION_CUTOVER_APPROVED:-}" == "YES" ]] || \
  fail "set DESIFACES_PRODUCTION_CUTOVER_APPROVED=YES for this one production cutover"

for x in curl docker python3 nginx git getent; do need "$x"; done

HOST="$(hostname -s 2>/dev/null || hostname)"
[[ "$HOST" == "$EXPECTED_HOST" ]] || fail "certified public host mismatch actual=$HOST expected=$EXPECTED_HOST"
[[ "$HOST" != "desifaces-dev" ]] || fail "production cutover forbidden on DEV"

log "============================================================"
log " desifaces.ai V3 — CERTIFIED PUBLIC-HOST CUTOVER"
log "============================================================"
log "host=$HOST"
log "expected_public_ip=$EXPECTED_PUBLIC_IP"

log ""
log "===== 1. PUBLIC-ENDPOINT IDENTITY — PRE-MUTATION ====="
IMDS_PUBLIC_IP="$(curl -fsS --max-time 5 -H Metadata:true \
  'http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text' 2>/dev/null || true)"
[[ "$IMDS_PUBLIC_IP" == "$EXPECTED_PUBLIC_IP" ]] || \
  fail "Azure IMDS public IP mismatch actual=${IMDS_PUBLIC_IP:-NONE} expected=$EXPECTED_PUBLIC_IP"
log "IMDS_PUBLIC_IP_GATE=PASS ip=$IMDS_PUBLIC_IP"

for name in web.desifaces.ai api.desifaces.ai; do
  IPS="$(getent ahostsv4 "$name" | awk '{print $1}' | sort -u || true)"
  printf '%s\n' "$IPS" | grep -Fxq "$EXPECTED_PUBLIC_IP" || \
    fail "$name does not resolve to certified public IP $EXPECTED_PUBLIC_IP"
  log "PUBLIC_DNS_GATE=PASS host=$name ip=$EXPECTED_PUBLIC_IP"
done

RELEASE=/home/azureuser/workspace/desifaces/RELEASE
[[ -f "$RELEASE" ]] || fail "canonical production RELEASE metadata missing"
grep -qx 'product=desifaces.ai' "$RELEASE" || fail "canonical RELEASE product mismatch"
grep -Eq '^release=v3-production-' "$RELEASE" || fail "canonical RELEASE is not a production release"
log "EXISTING_PRODUCTION_RELEASE_GATE=PASS"

docker inspect desifaces-db >/dev/null 2>&1 || fail "production database container missing"
docker inspect desifaces-redis >/dev/null 2>&1 || fail "production Redis container missing"
docker network inspect df-net >/dev/null 2>&1 || fail "production Docker network missing"
log "PRODUCTION_DATA_RUNTIME_GATE=PASS"

NGINX_TEXT="$(grep -RhsE 'server_name.*(web|api)\.desifaces\.ai' /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null || true)"
printf '%s\n' "$NGINX_TEXT" | grep -q 'web.desifaces.ai' || fail "nginx Web production host binding missing"
printf '%s\n' "$NGINX_TEXT" | grep -q 'api.desifaces.ai' || fail "nginx API production host binding missing"
log "PRODUCTION_NGINX_BINDING_GATE=PASS"

http_gate(){
  local name="$1" url="$2" code
  code="$(curl -k -sS --max-time 10 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
  [[ "$code" == 200 ]] || fail "$name failed url=$url http=$code"
  log "$name=PASS http=200"
}

http_gate PUBLIC_WEB_PRECUTOVER "https://web.desifaces.ai/auth/login"
http_gate PUBLIC_DIRECTOR_PRECUTOVER "https://api.desifaces.ai/director/api/health"
http_gate PUBLIC_ASSISTANT_PRECUTOVER "https://api.desifaces.ai/assistant/api/health"

log "PUBLIC_PRODUCTION_HOST_CLASSIFICATION=PASS"
log "PRODUCTION_HOSTNAME=$EXPECTED_HOST"

log ""
log "===== 2. FETCH + VERIFY CERTIFIED BASE LAUNCHER ====="
mkdir -p "$RUN"
curl -fsSL "$BASE_LAUNCHER_URL" -o "$BASE"
[[ "$(git hash-object "$BASE")" == "$BASE_LAUNCHER_GIT_BLOB" ]] || \
  fail "base launcher provenance mismatch"
log "BASE_LAUNCHER_PROVENANCE=PASS"

log ""
log "===== 3. APPLY EXPLICIT PUBLIC-HOST CLASSIFICATION PATCH ====="
python3 - "$BASE" "$PATCHED" <<'PY'
from pathlib import Path
import sys
src=Path(sys.argv[1]).read_text()
old='''[[ "$HOST" != *non-prod* && "$HOST" != *nonprod* ]] || \\\n  fail "production cutover forbidden on non-production hostname: $HOST"\n'''
if src.count(old) != 1:
    raise SystemExit(f'FAIL: expected exactly one legacy name-only rejection, found {src.count(old)}')
src=src.replace(old, '')
old_url='https://api.desifaces.ai/api/health'
new_url='https://api.desifaces.ai/director/api/health'
if src.count(old_url) != 1:
    raise SystemExit(f'FAIL: expected exactly one legacy public API smoke URL, found {src.count(old_url)}')
src=src.replace(old_url,new_url)
Path(sys.argv[2]).write_text(src)
PY
bash -n "$PATCHED"
grep -q 'production host mismatch actual=' "$PATCHED"
! grep -q 'production cutover forbidden on non-production hostname' "$PATCHED"
grep -q 'https://api.desifaces.ai/director/api/health' "$PATCHED"
log "PUBLIC_HOST_CLASSIFICATION_PATCH=PASS"

log ""
log "===== 4. EXECUTE CERTIFIED CUTOVER ====="
DESIFACES_PRODUCTION_CUTOVER_APPROVED=YES \
DESIFACES_PRODUCTION_HOSTNAME="$EXPECTED_HOST" \
bash "$PATCHED"

log "============================================================"
log " CERTIFIED PUBLIC-HOST PRODUCTION CUTOVER=PASS"
log "============================================================"
log "host=$EXPECTED_HOST"
log "public_ip=$EXPECTED_PUBLIC_IP"
