#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_REPO="prasshanthshankar-afk/desifaces_backend"
WEB_REPO="prasshanthshankar-afk/desifaces_web"
BACKEND_SHA="18dfd6a3a4941307a466960108e4573f3b9ff555"
WEB_SHA="21d1c8d4083c7f9705b957807e12d3d2bdf518d8"
MOBILE_SHA="b92e58a92eca115508804dd68dbc99cab086441f"
CANONICAL_ROOT="/home/azureuser/workspace/desifaces"
LEGACY_ROOT="/home/azureuser/workspace/desifaces-v2"
BACKUP_ROOT="/home/azureuser/backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="/tmp/desifaces-v3-direct-cutover-$STAMP"
STAGE="/home/azureuser/workspace/.desifaces-v3-stage-$STAMP"
PREVIOUS=""

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }

[[ "${DESIFACES_PRODUCTION_CUTOVER_APPROVED:-}" == "YES" ]] || \
  fail "set DESIFACES_PRODUCTION_CUTOVER_APPROVED=YES for this one production cutover"
[[ -n "${DESIFACES_PRODUCTION_HOSTNAME:-}" ]] || \
  fail "set DESIFACES_PRODUCTION_HOSTNAME to the exact certified production hostname"

HOST="$(hostname -s 2>/dev/null || hostname)"
[[ "$HOST" == "$DESIFACES_PRODUCTION_HOSTNAME" ]] || \
  fail "production host mismatch actual=$HOST expected=$DESIFACES_PRODUCTION_HOSTNAME"
[[ "$HOST" != "desifaces-dev" ]] || fail "production cutover forbidden on DEV"
[[ "$HOST" != *non-prod* && "$HOST" != *nonprod* ]] || \
  fail "production cutover forbidden on non-production hostname: $HOST"

for x in gh tar docker curl python3 sudo nginx sha256sum; do need "$x"; done
docker compose version >/dev/null 2>&1 || fail "docker compose v2 is required"

cleanup(){ rm -rf "$RUN" >/dev/null 2>&1 || true; }
trap cleanup EXIT
rm -rf "$RUN" "$STAGE"
mkdir -p "$RUN" "$STAGE" "$BACKUP_ROOT"

log "============================================================"
log " desifaces.ai V3 — DIRECT PRODUCTION VM CUTOVER"
log "============================================================"
log "host=$HOST"
log "backend_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "mobile_parity_sha=$MOBILE_SHA"
log "stripe_live_action=INSPECTION_ONLY"
log "mobile_store_action=NONE"

log ""
log "===== 1. PRE-MUTATION ACCESS + PROVENANCE GATE ====="
gh auth status -h github.com >/dev/null 2>&1 || fail "GitHub CLI is not authenticated on production VM"
gh api "repos/$BACKEND_REPO/commits/$BACKEND_SHA" --jq .sha | grep -qx "$BACKEND_SHA" || fail "backend release commit inaccessible"
gh api "repos/$WEB_REPO/commits/$WEB_SHA" --jq .sha | grep -qx "$WEB_SHA" || fail "private Web release commit inaccessible from production VM"

docker inspect desifaces-db >/dev/null 2>&1 || fail "production database container desifaces-db is not running"
docker inspect desifaces-redis >/dev/null 2>&1 || fail "production Redis container desifaces-redis is not running"
docker network inspect df-net >/dev/null 2>&1 || fail "production Docker network df-net is missing"

ENV_SOURCE=""
[[ -f "$CANONICAL_ROOT/infra/.env" ]] && ENV_SOURCE="$CANONICAL_ROOT/infra/.env"
[[ -n "$ENV_SOURCE" ]] || { [[ -f "$LEGACY_ROOT/infra/.env" ]] && ENV_SOURCE="$LEGACY_ROOT/infra/.env"; }
[[ -n "$ENV_SOURCE" ]] || fail "production infra/.env source not found"
log "PRODUCTION_PREFLIGHT=PASS"
log "GITHUB_PRIVATE_WEB_ACCESS=PASS"

log ""
log "===== 2. EXPORT IMMUTABLE CERTIFIED SOURCES ON PRODUCTION VM ====="
gh api -H 'Accept: application/vnd.github+json' "repos/$BACKEND_REPO/tarball/$BACKEND_SHA" > "$RUN/backend.tar.gz"
[[ -s "$RUN/backend.tar.gz" ]] || fail "backend archive download failed"
tar -xzf "$RUN/backend.tar.gz" --strip-components=1 -C "$STAGE"

mkdir -p "$STAGE/web-app"
gh api -H 'Accept: application/vnd.github+json' "repos/$WEB_REPO/tarball/$WEB_SHA" > "$RUN/web.tar.gz"
[[ -s "$RUN/web.tar.gz" ]] || fail "Web archive download failed"
tar -xzf "$RUN/web.tar.gz" --strip-components=1 -C "$STAGE/web-app"

! find "$STAGE" -name .git -type d -print -quit | grep -q . || fail "staged source unexpectedly contains Git metadata"
[[ -f "$STAGE/deploy/production/run-canonical-production-deploy-20260904.sh" ]] || fail "canonical production runner missing"
[[ -f "$STAGE/scripts/apply-v3-audio-cogs-production.sh" ]] || fail "Audio COGS production runner missing"
[[ -f "$STAGE/scripts/install-v3-production-systemd-bundle.sh" ]] || fail "scheduler installer missing"
[[ -f "$STAGE/scripts/certify-v3-stripe-live-readiness.sh" ]] || fail "Stripe readiness inspector missing"
[[ -f "$STAGE/web-app/web/Dockerfile" ]] || fail "Web Dockerfile missing"

cat > "$STAGE/RELEASE" <<EOF
product=desifaces.ai
release=v3-production-20260912
backend_release_sha=$BACKEND_SHA
backend_application_sha=$BACKEND_SHA
web_sha=$WEB_SHA
mobile_parity_sha=$MOBILE_SHA
created_utc=$STAMP
source_policy=immutable_github_export_no_git
production_approval=explicit_one_shot
production_hostname=$HOST
EOF

mkdir -p "$STAGE/infra"
cp "$ENV_SOURCE" "$STAGE/infra/.env"
chmod 600 "$STAGE/infra/.env"

STAGED_HASH="$(tar -C "$STAGE" -cf - RELEASE deploy/production/run-canonical-production-deploy-20260904.sh scripts/apply-v3-audio-cogs-production.sh scripts/install-v3-production-systemd-bundle.sh scripts/certify-v3-stripe-live-readiness.sh 2>/dev/null | sha256sum | awk '{print $1}')"
[[ ${#STAGED_HASH} -eq 64 ]] || fail "staged release hash unavailable"
log "IMMUTABLE_SOURCE_EXPORT=PASS"
log "STAGED_RELEASE_HASH=$STAGED_HASH"

log ""
log "===== 3. ACTIVATE CANONICAL SOURCE ====="
if [[ -e "$CANONICAL_ROOT" ]]; then
  PREVIOUS="$BACKUP_ROOT/desifaces-pre-v3-20260912-$STAMP"
  mv "$CANONICAL_ROOT" "$PREVIOUS"
  log "PREVIOUS_CANONICAL=$PREVIOUS"
else
  log "PREVIOUS_CANONICAL=NONE"
fi
mv "$STAGE" "$CANONICAL_ROOT"
cd "$CANONICAL_ROOT"
log "CANONICAL_PACKAGE_ACTIVE=$CANONICAL_ROOT"

log ""
log "===== 4. DEPLOY + BACKUP + CLONE-CERTIFY + PUBLIC CUTOVER ====="
ROOT="$CANONICAL_ROOT" bash deploy/production/run-canonical-production-deploy-20260904.sh

log ""
log "===== 5. APPLY CERTIFIED AUDIO COGS BASIS ====="
DF_PRODUCTION_CONFIRM=YES \
DF_PRODUCTION_HOSTNAME="$HOST" \
bash scripts/apply-v3-audio-cogs-production.sh

log ""
log "===== 6. INSTALL CERTIFIED PRODUCTION TIMERS ====="
DF_PRODUCTION_CONFIRM=YES \
DF_PRODUCTION_HOSTNAME="$HOST" \
bash scripts/install-v3-production-systemd-bundle.sh

log ""
log "===== 7. STRIPE LIVE READINESS — INSPECTION ONLY ====="
bash scripts/certify-v3-stripe-live-readiness.sh prepare

log ""
log "===== 8. FINAL PUBLIC SMOKE ====="
curl -fsS --max-time 15 https://web.desifaces.ai/auth/login >/tmp/desifaces-web-smoke.html
grep -qi desifaces /tmp/desifaces-web-smoke.html || fail "public Web branding smoke failed"
curl -fsS --max-time 15 https://api.desifaces.ai/api/health >/tmp/desifaces-api-health.json
python3 - <<'PY'
import json
from pathlib import Path
payload=json.loads(Path('/tmp/desifaces-api-health.json').read_text())
if payload.get('ok') is False:
    raise SystemExit('FAIL: public API health not OK')
print('PUBLIC_API_HEALTH=PASS')
PY

log "PUBLIC_WEB_SMOKE=PASS"
log "PRODUCTION_AUDIO_COGS=PASS"
log "PRODUCTION_TIMERS=PASS"
log "STRIPE_LIVE_ACTION=INSPECTION_ONLY"
log "MOBILE_STORE_ACTION=NONE"
log "============================================================"
log " DESIFACES V3 DIRECT PRODUCTION CUTOVER=PASS"
log "============================================================"
log "backend_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "mobile_parity_sha=$MOBILE_SHA"
log "host=$HOST"
log "previous_canonical=${PREVIOUS:-NONE}"
