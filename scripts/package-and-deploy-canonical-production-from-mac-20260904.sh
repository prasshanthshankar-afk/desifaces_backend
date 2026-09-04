#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_REPO="prasshanthshankar-afk/desifaces_backend"
BACKEND_RELEASE_SHA="91bbb1f2ab9f84c585b6f293d18ad36083b0a5fa"
BACKEND_APPLICATION_SHA="b81c3e6a2e74528c059d14c0adffab4a67af1816"
WEB_REPO="prasshanthshankar-afk/desifaces_web"
WEB_SHA="e51de0181bc9dd74c4ace4ec5ab8891f26be83d2"
SSH_HOST="${SSH_HOST:-desifaces-gpu}"
REMOTE_WORKSPACE="/home/azureuser/workspace"
CANONICAL_ROOT="$REMOTE_WORKSPACE/desifaces"
LEGACY_ROOT="$REMOTE_WORKSPACE/desifaces-v2"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="/tmp/desifaces-production-package-$STAMP"
PACKAGE="$RUN/desifaces-production-$STAMP.tar.gz"
REMOTE_PACKAGE="/tmp/desifaces-production-$STAMP.tar.gz"

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
for x in gh git tar ssh scp python3; do need "$x"; done
[[ "$(uname -s)" == "Darwin" ]] || fail "run this packaging launcher from the Mac"

cleanup(){ rm -rf "$RUN" >/dev/null 2>&1 || true; }
trap cleanup EXIT
mkdir -p "$RUN/backend" "$RUN/web" "$RUN/package"

log "============================================================"
log " desifaces.ai — PACKAGE + DEPLOY CANONICAL PRODUCTION"
log "============================================================"
log "backend_release_sha=$BACKEND_RELEASE_SHA"
log "backend_application_sha=$BACKEND_APPLICATION_SHA"
log "web_sha=$WEB_SHA"
log "ssh_host=$SSH_HOST"

log ""
log "===== 1. VERIFY PRODUCTION SSH ====="
REMOTE_HOST="$(ssh -o BatchMode=yes -o ConnectTimeout=12 "$SSH_HOST" 'hostname -s')" || fail "cannot SSH to $SSH_HOST"
[[ "$REMOTE_HOST" == desifaces-gpu* ]] || fail "SSH target is not desifaces-gpu: $REMOTE_HOST"
ssh "$SSH_HOST" "test -f '$LEGACY_ROOT/infra/.env' -o -f '$CANONICAL_ROOT/infra/.env'" || fail "production infra/.env not found"
log "PRODUCTION_SSH=PASS host=$REMOTE_HOST"

log ""
log "===== 2. FETCH IMMUTABLE RELEASES ON MAC ====="
gh repo clone "$BACKEND_REPO" "$RUN/backend" -- --filter=blob:none --no-checkout >/dev/null
(
  cd "$RUN/backend"
  git checkout --detach "$BACKEND_RELEASE_SHA" >/dev/null
  [[ "$(git rev-parse HEAD)" == "$BACKEND_RELEASE_SHA" ]]
  git archive HEAD | tar -x -C "$RUN/package"
)
gh repo clone "$WEB_REPO" "$RUN/web" -- --filter=blob:none --no-checkout >/dev/null
(
  cd "$RUN/web"
  git checkout --detach "$WEB_SHA" >/dev/null
  [[ "$(git rev-parse HEAD)" == "$WEB_SHA" ]]
  mkdir -p "$RUN/package/web-app"
  git archive HEAD | tar -x -C "$RUN/package/web-app"
)
! find "$RUN/package" -name .git -type d -print -quit | grep -q . || fail "package unexpectedly contains Git metadata"
[[ -f "$RUN/package/deploy/production/run-canonical-production-deploy-20260904.sh" ]] || fail "canonical deploy runner missing"
[[ -f "$RUN/package/web-app/web/Dockerfile" ]] || fail "web release missing"

cat > "$RUN/package/RELEASE" <<EOF
product=desifaces.ai
release=v3-production-20260904
backend_release_sha=$BACKEND_RELEASE_SHA
backend_application_sha=$BACKEND_APPLICATION_SHA
web_sha=$WEB_SHA
created_utc=$STAMP
source_policy=immutable_export_no_git
EOF

tar -C "$RUN/package" -czf "$PACKAGE" .
[[ -s "$PACKAGE" ]] || fail "release package is empty"
log "IMMUTABLE_PACKAGE=PASS file=$PACKAGE"

log ""
log "===== 3. COPY PACKAGE TO PRODUCTION ====="
scp -q "$PACKAGE" "$SSH_HOST:$REMOTE_PACKAGE"
ssh "$SSH_HOST" "test -s '$REMOTE_PACKAGE'" || fail "production package transfer failed"
log "PACKAGE_TRANSFER=PASS"

log ""
log "===== 4. ACTIVATE CANONICAL PACKAGE + DEPLOY ====="
ssh -tt "$SSH_HOST" "REMOTE_PACKAGE='$REMOTE_PACKAGE' STAMP='$STAMP' bash -s" <<'REMOTE'
set -Eeuo pipefail
CANONICAL_ROOT="/home/azureuser/workspace/desifaces"
LEGACY_ROOT="/home/azureuser/workspace/desifaces-v2"
BACKUP_ROOT="/home/azureuser/backups"
STAGE="/home/azureuser/workspace/.desifaces-stage-$STAMP"
PREVIOUS=""

fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
mkdir -p "$BACKUP_ROOT"
rm -rf "$STAGE"
mkdir -p "$STAGE"
tar -xzf "$REMOTE_PACKAGE" -C "$STAGE"
[[ -f "$STAGE/RELEASE" ]] || fail "staged RELEASE metadata missing"
[[ -f "$STAGE/deploy/production/run-canonical-production-deploy-20260904.sh" ]] || fail "staged deploy runner missing"
! find "$STAGE" -name .git -type d -print -quit | grep -q . || fail "staged package contains Git metadata"

ENV_SOURCE=""
[[ -f "$CANONICAL_ROOT/infra/.env" ]] && ENV_SOURCE="$CANONICAL_ROOT/infra/.env"
[[ -n "$ENV_SOURCE" ]] || { [[ -f "$LEGACY_ROOT/infra/.env" ]] && ENV_SOURCE="$LEGACY_ROOT/infra/.env"; }
[[ -n "$ENV_SOURCE" ]] || fail "production infra/.env source not found"
mkdir -p "$STAGE/infra"
cp "$ENV_SOURCE" "$STAGE/infra/.env"
chmod 600 "$STAGE/infra/.env"

if [[ -e "$CANONICAL_ROOT" ]]; then
  PREVIOUS="$BACKUP_ROOT/desifaces-pre-release-$STAMP"
  mv "$CANONICAL_ROOT" "$PREVIOUS"
  echo "PREVIOUS_CANONICAL=$PREVIOUS"
fi
mv "$STAGE" "$CANONICAL_ROOT"

echo "CANONICAL_PACKAGE_ACTIVE=$CANONICAL_ROOT"
cd "$CANONICAL_ROOT"
ROOT="$CANONICAL_ROOT" bash deploy/production/run-canonical-production-deploy-20260904.sh

echo ""
echo "===== RETIRE VERSIONED PRODUCTION WORKSPACE ====="
if [[ -d "$LEGACY_ROOT" ]]; then
  MOUNTS="$(docker ps -q | xargs -r docker inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' 2>/dev/null | grep -F "$LEGACY_ROOT" || true)"
  if [[ -n "$MOUNTS" ]]; then
    echo "LEGACY_WORKSPACE_RETIRE=DEFERRED_ACTIVE_MOUNT"
    printf '%s\n' "$MOUNTS"
  else
    ARCHIVE="$BACKUP_ROOT/desifaces-v2-pre-v3-$STAMP"
    mv "$LEGACY_ROOT" "$ARCHIVE"
    echo "LEGACY_WORKSPACE_RETIRED=$ARCHIVE"
  fi
else
  echo "LEGACY_WORKSPACE_RETIRE=NOT_REQUIRED"
fi

rm -f "$REMOTE_PACKAGE"
COUNT="$(find /home/azureuser/workspace -mindepth 1 -maxdepth 1 -type d -name 'desifaces*' | wc -l | tr -d ' ')"
echo "workspace_desifaces_count=$COUNT"
if [[ "$COUNT" == "1" ]]; then
  echo "PRODUCTION_SINGLE_PACKAGE=PASS"
else
  echo "PRODUCTION_SINGLE_PACKAGE=DEFERRED count=$COUNT"
fi

echo "============================================================"
echo " CANONICAL PRODUCTION RELEASE PASS"
echo "============================================================"
echo "canonical_root=$CANONICAL_ROOT"
REMOTE

log ""
log "============================================================"
log " MAC PACKAGE + PRODUCTION DEPLOY PASS"
log "============================================================"
log "canonical_root=$CANONICAL_ROOT"
log "backend_application_sha=$BACKEND_APPLICATION_SHA"
log "web_sha=$WEB_SHA"
log "PRODUCTION_DEPLOY_FROM_MAC=PASS"
