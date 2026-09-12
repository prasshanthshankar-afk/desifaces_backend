#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_REPO="prasshanthshankar-afk/desifaces_backend"
WEB_REPO="prasshanthshankar-afk/desifaces_web"
BACKEND_SHA="18dfd6a3a4941307a466960108e4573f3b9ff555"
WEB_SHA="21d1c8d4083c7f9705b957807e12d3d2bdf518d8"
MOBILE_SHA="b92e58a92eca115508804dd68dbc99cab086441f"
SSH_HOST="${SSH_HOST:-desifaces-gpu}"
REMOTE_WORKSPACE="/home/azureuser/workspace"
CANONICAL_ROOT="$REMOTE_WORKSPACE/desifaces"
LEGACY_ROOT="$REMOTE_WORKSPACE/desifaces-v2"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="/tmp/desifaces-v3-production-cutover-$STAMP"
PACKAGE="$RUN/desifaces-v3-production-$STAMP.tar.gz"
REMOTE_PACKAGE="/tmp/desifaces-v3-production-$STAMP.tar.gz"

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }

[[ "${DESIFACES_PRODUCTION_CUTOVER_APPROVED:-}" == "YES" ]] || \
  fail "set DESIFACES_PRODUCTION_CUTOVER_APPROVED=YES for this one production cutover"
[[ "$(uname -s)" == "Darwin" ]] || fail "run this launcher from the Mac"
for x in gh git tar ssh scp python3 shasum; do need "$x"; done

cleanup(){ rm -rf "$RUN" >/dev/null 2>&1 || true; }
trap cleanup EXIT
mkdir -p "$RUN/backend" "$RUN/web" "$RUN/package"

log "============================================================"
log " desifaces.ai V3 — SEP 12 PRODUCTION CUTOVER"
log "============================================================"
log "backend_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "mobile_parity_sha=$MOBILE_SHA"
log "ssh_host=$SSH_HOST"
log "stripe_live_action=INSPECTION_ONLY"
log "mobile_store_action=NONE"

log ""
log "===== 1. VERIFY EXACT PRODUCTION TARGET ====="
REMOTE_HOST="$(ssh -o BatchMode=yes -o ConnectTimeout=12 "$SSH_HOST" 'hostname -s')" || \
  fail "cannot SSH to $SSH_HOST"
[[ "$REMOTE_HOST" == desifaces-gpu* ]] || fail "SSH target is not desifaces-gpu: $REMOTE_HOST"
[[ "$REMOTE_HOST" != "desifaces-dev" ]] || fail "production cutover forbidden on DEV"
ssh "$SSH_HOST" "test -f '$CANONICAL_ROOT/infra/.env' -o -f '$LEGACY_ROOT/infra/.env'" || \
  fail "production infra/.env not found"
log "PRODUCTION_SSH=PASS host=$REMOTE_HOST"

log ""
log "===== 2. EXPORT IMMUTABLE CERTIFIED SOURCES ====="
gh repo clone "$BACKEND_REPO" "$RUN/backend" -- --filter=blob:none --no-checkout >/dev/null
(
  cd "$RUN/backend"
  git fetch origin "$BACKEND_SHA" --quiet
  git checkout --detach "$BACKEND_SHA" --quiet
  [[ "$(git rev-parse HEAD)" == "$BACKEND_SHA" ]] || exit 1
  git archive HEAD | tar -x -C "$RUN/package"
)

gh repo clone "$WEB_REPO" "$RUN/web" -- --filter=blob:none --no-checkout >/dev/null
(
  cd "$RUN/web"
  git fetch origin "$WEB_SHA" --quiet
  git checkout --detach "$WEB_SHA" --quiet
  [[ "$(git rev-parse HEAD)" == "$WEB_SHA" ]] || exit 1
  mkdir -p "$RUN/package/web-app"
  git archive HEAD | tar -x -C "$RUN/package/web-app"
)

! find "$RUN/package" -name .git -type d -print -quit | grep -q . || fail "package unexpectedly contains Git metadata"
[[ -f "$RUN/package/deploy/production/run-canonical-production-deploy-20260904.sh" ]] || fail "canonical production runner missing"
[[ -f "$RUN/package/scripts/apply-v3-audio-cogs-production.sh" ]] || fail "Audio COGS production runner missing"
[[ -f "$RUN/package/scripts/install-v3-production-systemd-bundle.sh" ]] || fail "scheduler installer missing"
[[ -f "$RUN/package/scripts/certify-v3-stripe-live-readiness.sh" ]] || fail "Stripe readiness inspector missing"
[[ -f "$RUN/package/web-app/web/Dockerfile" ]] || fail "Web Dockerfile missing"

cat > "$RUN/package/RELEASE" <<EOF
product=desifaces.ai
release=v3-production-20260912
backend_release_sha=$BACKEND_SHA
backend_application_sha=$BACKEND_SHA
web_sha=$WEB_SHA
mobile_parity_sha=$MOBILE_SHA
created_utc=$STAMP
source_policy=immutable_export_no_git
production_approval=explicit_one_shot
EOF

tar -C "$RUN/package" -czf "$PACKAGE" .
[[ -s "$PACKAGE" ]] || fail "release package is empty"
PACKAGE_SHA="$(shasum -a 256 "$PACKAGE" | awk '{print $1}')"
[[ ${#PACKAGE_SHA} -eq 64 ]] || fail "package SHA256 unavailable"
log "IMMUTABLE_PACKAGE=PASS sha256=$PACKAGE_SHA"

log ""
log "===== 3. TRANSFER + VERIFY PACKAGE ====="
scp -q "$PACKAGE" "$SSH_HOST:$REMOTE_PACKAGE"
REMOTE_SHA="$(ssh "$SSH_HOST" "sha256sum '$REMOTE_PACKAGE' | awk '{print \$1}'")"
[[ "$REMOTE_SHA" == "$PACKAGE_SHA" ]] || fail "production package checksum mismatch"
log "PACKAGE_TRANSFER=PASS"
log "PACKAGE_SHA256_GATE=PASS"

log ""
log "===== 4. ACTIVATE + DEPLOY + CERTIFY PRODUCTION ====="
ssh -tt "$SSH_HOST" \
  "REMOTE_PACKAGE='$REMOTE_PACKAGE' STAMP='$STAMP' EXPECTED_BACKEND_SHA='$BACKEND_SHA' EXPECTED_WEB_SHA='$WEB_SHA' bash -s" <<'REMOTE'
set -Eeuo pipefail

CANONICAL_ROOT="/home/azureuser/workspace/desifaces"
LEGACY_ROOT="/home/azureuser/workspace/desifaces-v2"
BACKUP_ROOT="/home/azureuser/backups"
STAGE="/home/azureuser/workspace/.desifaces-v3-stage-$STAMP"
PREVIOUS=""

fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
HOST="$(hostname -s)"
[[ "$HOST" == desifaces-gpu* ]] || fail "production host guard failed: $HOST"
[[ "$HOST" != "desifaces-dev" ]] || fail "production cutover forbidden on DEV"

mkdir -p "$BACKUP_ROOT"
rm -rf "$STAGE"
mkdir -p "$STAGE"
tar -xzf "$REMOTE_PACKAGE" -C "$STAGE"
[[ -f "$STAGE/RELEASE" ]] || fail "staged RELEASE metadata missing"
[[ "$(awk -F= '$1=="backend_application_sha"{print $2}' "$STAGE/RELEASE")" == "$EXPECTED_BACKEND_SHA" ]] || fail "backend release SHA mismatch"
[[ "$(awk -F= '$1=="web_sha"{print $2}' "$STAGE/RELEASE")" == "$EXPECTED_WEB_SHA" ]] || fail "web release SHA mismatch"
! find "$STAGE" -name .git -type d -print -quit | grep -q . || fail "staged package contains Git metadata"
echo "RELEASE_PROVENANCE=PASS"

ENV_SOURCE=""
[[ -f "$CANONICAL_ROOT/infra/.env" ]] && ENV_SOURCE="$CANONICAL_ROOT/infra/.env"
[[ -n "$ENV_SOURCE" ]] || { [[ -f "$LEGACY_ROOT/infra/.env" ]] && ENV_SOURCE="$LEGACY_ROOT/infra/.env"; }
[[ -n "$ENV_SOURCE" ]] || fail "production infra/.env source not found"
mkdir -p "$STAGE/infra"
cp "$ENV_SOURCE" "$STAGE/infra/.env"
chmod 600 "$STAGE/infra/.env"

echo "===== PRESERVE CURRENT CANONICAL SOURCE ====="
if [[ -e "$CANONICAL_ROOT" ]]; then
  PREVIOUS="$BACKUP_ROOT/desifaces-pre-v3-20260912-$STAMP"
  mv "$CANONICAL_ROOT" "$PREVIOUS"
  echo "PREVIOUS_CANONICAL=$PREVIOUS"
else
  echo "PREVIOUS_CANONICAL=NONE"
fi
mv "$STAGE" "$CANONICAL_ROOT"
cd "$CANONICAL_ROOT"
echo "CANONICAL_PACKAGE_ACTIVE=$CANONICAL_ROOT"

# This existing runner performs: prebuild, production DB backup, clone migration
# certification, live migrations, application recreation, local service health,
# Web build, nginx cutover and public smoke. Postgres/Redis containers are not
# recreated by the application cutover step.
ROOT="$CANONICAL_ROOT" bash deploy/production/run-canonical-production-deploy-20260904.sh

echo "===== APPLY SEP 12 AUDIO COGS BASIS ====="
DF_PRODUCTION_CONFIRM=YES \
DF_PRODUCTION_HOSTNAME="$HOST" \
bash scripts/apply-v3-audio-cogs-production.sh

echo "===== INSTALL CERTIFIED PRODUCTION TIMERS ====="
DF_PRODUCTION_CONFIRM=YES \
DF_PRODUCTION_HOSTNAME="$HOST" \
bash scripts/install-v3-production-systemd-bundle.sh

echo "===== STRIPE LIVE READINESS — INSPECTION ONLY ====="
bash scripts/certify-v3-stripe-live-readiness.sh prepare

echo "===== FINAL PUBLIC SMOKE ====="
curl -fsS --max-time 15 https://web.desifaces.ai/auth/login >/tmp/desifaces-web-smoke.html
grep -qi desifaces /tmp/desifaces-web-smoke.html || fail "public Web branding smoke failed"
curl -fsS --max-time 15 https://api.desifaces.ai/api/health >/tmp/desifaces-api-health.json
python3 - <<'PY'
import json
from pathlib import Path
payload=json.loads(Path('/tmp/desifaces-api-health.json').read_text())
if not bool(payload.get('ok', True)):
    raise SystemExit('FAIL: public API health not OK')
print('PUBLIC_API_HEALTH=PASS')
PY

echo "PUBLIC_WEB_SMOKE=PASS"
echo "PRODUCTION_AUDIO_COGS=PASS"
echo "PRODUCTION_TIMERS=PASS"
echo "STRIPE_LIVE_ACTION=INSPECTION_ONLY"
echo "MOBILE_STORE_ACTION=NONE"
echo "============================================================"
echo " DESIFACES V3 PRODUCTION CUTOVER=PASS"
echo "============================================================"
echo "backend_sha=$EXPECTED_BACKEND_SHA"
echo "web_sha=$EXPECTED_WEB_SHA"
echo "host=$HOST"
echo "previous_canonical=${PREVIOUS:-NONE}"

rm -f "$REMOTE_PACKAGE"
REMOTE

log ""
log "============================================================"
log " desifaces.ai V3 — PRODUCTION CUTOVER COMPLETE"
log "============================================================"
log "backend_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "mobile_parity_sha=$MOBILE_SHA"
log "production_host=$REMOTE_HOST"
log "PRODUCTION_CUTOVER=PASS"
log "STRIPE_LIVE_ACTION=INSPECTION_ONLY"
log "MOBILE_STORE_ACTION=NONE"
