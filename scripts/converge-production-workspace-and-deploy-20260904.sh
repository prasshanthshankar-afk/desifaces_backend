#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_REPO="prasshanthshankar-afk/desifaces_backend"
BACKEND_SHA="b81c3e6a2e74528c059d14c0adffab4a67af1816"
WEB_REPO="prasshanthshankar-afk/desifaces_web"
WEB_SHA="e51de0181bc9dd74c4ace4ec5ab8891f26be83d2"
CERTIFIED_LAUNCHER_SHA="5454478c1f5e7d26306e661cf6ce09496b83fe40"
LEGACY_ROOT="/home/azureuser/workspace/desifaces-v2"
CANONICAL_ROOT="/home/azureuser/workspace/desifaces"
BACKUP_ROOT="/home/azureuser/backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE="/tmp/desifaces-canonical-stage-$STAMP"

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
for x in gh git tar docker bash grep awk sed find; do need "$x"; done

HOST="$(hostname -s 2>/dev/null || hostname)"
[[ "$HOST" == desifaces-gpu* ]] || fail "run on desifaces-gpu; current host=$HOST"
[[ -f "$LEGACY_ROOT/infra/.env" || -f "$CANONICAL_ROOT/infra/.env" ]] || fail "no production infra/.env found in legacy or canonical workspace"

cleanup(){ rm -rf "$STAGE" >/dev/null 2>&1 || true; }
trap cleanup EXIT
mkdir -p "$BACKUP_ROOT" "$STAGE"
chmod 700 "$STAGE"

log "============================================================"
log " desifaces.ai — CANONICAL PRODUCTION WORKSPACE + DEPLOY"
log "============================================================"
log "host=$HOST"
log "canonical_root=$CANONICAL_ROOT"
log "legacy_root=$LEGACY_ROOT"
log "backend_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "launcher_sha=$CERTIFIED_LAUNCHER_SHA"

prepare_canonical(){
  local env_source tmp_backend tmp_web
  env_source="$CANONICAL_ROOT/infra/.env"
  [[ -f "$env_source" ]] || env_source="$LEGACY_ROOT/infra/.env"

  if [[ -f "$CANONICAL_ROOT/RELEASE" ]] \
     && grep -qx "backend_sha=$BACKEND_SHA" "$CANONICAL_ROOT/RELEASE" \
     && grep -qx "web_sha=$WEB_SHA" "$CANONICAL_ROOT/RELEASE" \
     && [[ -f "$CANONICAL_ROOT/infra/.env" ]]; then
    log "CANONICAL_PACKAGE=ALREADY_READY"
    return 0
  fi

  [[ ! -e "$CANONICAL_ROOT" || -z "$(find "$CANONICAL_ROOT" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]] \
    || fail "$CANONICAL_ROOT already exists with unrecognized content; refusing to overwrite"

  tmp_backend="$STAGE/backend-repo"
  tmp_web="$STAGE/web-repo"
  mkdir -p "$STAGE/package"

  gh repo clone "$BACKEND_REPO" "$tmp_backend" -- --filter=blob:none --no-checkout >/dev/null
  (cd "$tmp_backend" && git checkout --detach "$BACKEND_SHA" >/dev/null && git archive HEAD | tar -x -C "$STAGE/package")

  gh repo clone "$WEB_REPO" "$tmp_web" -- --filter=blob:none --no-checkout >/dev/null
  (cd "$tmp_web" && git checkout --detach "$WEB_SHA" >/dev/null)
  mkdir -p "$STAGE/package/web-app"
  (cd "$tmp_web" && git archive HEAD | tar -x -C "$STAGE/package/web-app")

  mkdir -p "$STAGE/package/infra"
  cp "$env_source" "$STAGE/package/infra/.env"
  chmod 600 "$STAGE/package/infra/.env"

  cat > "$STAGE/package/RELEASE" <<EOF
product=desifaces.ai
release=v3-production-20260904
backend_sha=$BACKEND_SHA
web_sha=$WEB_SHA
certified_launcher_sha=$CERTIFIED_LAUNCHER_SHA
created_utc=$STAMP
source_policy=immutable_export_no_git
EOF

  # Production must be a release package, not a development Git workspace.
  if find "$STAGE/package" -name .git -type d -print -quit | grep -q .; then
    fail "canonical package unexpectedly contains .git metadata"
  fi

  rmdir "$CANONICAL_ROOT" >/dev/null 2>&1 || true
  mv "$STAGE/package" "$CANONICAL_ROOT"
  chmod 700 "$CANONICAL_ROOT/infra"
  log "CANONICAL_PACKAGE=CREATED root=$CANONICAL_ROOT"
}

prepare_canonical

log ""
log "===== CANONICAL PACKAGE CERTIFICATION ====="
[[ -f "$CANONICAL_ROOT/docker-compose.yml" ]] || fail "canonical backend compose missing"
[[ -f "$CANONICAL_ROOT/deploy/production/docker-compose.v3-app.production.yml" ]] || fail "canonical production overlay missing"
[[ -f "$CANONICAL_ROOT/deploy/production/migrations-v3-production-20260903.txt" ]] || fail "canonical migration manifest missing"
[[ -f "$CANONICAL_ROOT/web-app/web/Dockerfile" ]] || fail "canonical web Dockerfile missing"
[[ -f "$CANONICAL_ROOT/infra/.env" ]] || fail "canonical production env missing"
! find "$CANONICAL_ROOT" -name .git -type d -print -quit | grep -q . || fail "Git metadata found in production package"
grep -qx "backend_sha=$BACKEND_SHA" "$CANONICAL_ROOT/RELEASE"
grep -qx "web_sha=$WEB_SHA" "$CANONICAL_ROOT/RELEASE"
log "CANONICAL_PACKAGE_CERTIFICATION=PASS"

log ""
log "===== DEPLOY + TEST FROM CERTIFIED RELEASE ====="
PROD_ROOT="$CANONICAL_ROOT" \
PROD_ENV="$CANONICAL_ROOT/infra/.env" \
bash -c "$(gh api \
  "repos/$BACKEND_REPO/contents/scripts/deploy-v3-production-gpu-20260904.sh?ref=$CERTIFIED_LAUNCHER_SHA" \
  --jq .content | base64 -d)"
log "CANONICAL_V3_DEPLOY=PASS"

log ""
log "===== RETIRE VERSIONED PRODUCTION WORKSPACE ====="
if [[ -d "$LEGACY_ROOT" ]]; then
  # Never move a source directory that is still bind-mounted into a running container.
  MOUNTS="$(docker ps -q | xargs -r docker inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' 2>/dev/null | grep -F "$LEGACY_ROOT" || true)"
  if [[ -n "$MOUNTS" ]]; then
    log "LEGACY_WORKSPACE_RETIRE=BLOCKED_ACTIVE_MOUNT"
    printf '%s\n' "$MOUNTS"
    fail "a running container still bind-mounts $LEGACY_ROOT; production deploy passed but legacy workspace was intentionally retained"
  fi
  LEGACY_ARCHIVE="$BACKUP_ROOT/desifaces-v2-pre-v3-$STAMP"
  mv "$LEGACY_ROOT" "$LEGACY_ARCHIVE"
  log "LEGACY_WORKSPACE_RETIRED=$LEGACY_ARCHIVE"
else
  log "LEGACY_WORKSPACE_RETIRE=NOT_REQUIRED"
fi

COUNT="$(find /home/azureuser/workspace -mindepth 1 -maxdepth 1 -type d -name 'desifaces*' | wc -l | tr -d ' ')"
[[ "$COUNT" == "1" ]] || fail "expected exactly one desifaces* directory in production workspace, found $COUNT"
[[ -d "$CANONICAL_ROOT" ]] || fail "canonical production workspace missing after convergence"

log ""
log "============================================================"
log " PRODUCTION WORKSPACE CONVERGENCE PASS"
log "============================================================"
log "canonical_root=$CANONICAL_ROOT"
log "backend_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "workspace_versions=1"
log "PRODUCTION_SINGLE_PACKAGE=PASS"
