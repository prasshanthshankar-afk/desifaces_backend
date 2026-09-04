#!/usr/bin/env bash
set -Eeuo pipefail

REPO="prasshanthshankar-afk/desifaces_backend"
RELEASE_SHA="94291567edfe8f3deb236942319f5a6307de60b7"
APPLICATION_SHA="9b76329062b9e338e7276c7b7154e12fd5128e3d"
SSH_HOST="${SSH_HOST:-desifaces-gpu}"
ROOT="/home/azureuser/workspace/desifaces"
BASELINE_BACKUP="/home/azureuser/backups/desifaces-release-20260904T011508Z/desifaces-20260904T011508Z.dump"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="/tmp/desifaces-backend-closeout-$STAMP"
PATCH="$RUN/backend-closeout.tar.gz"
REMOTE_PATCH="/tmp/desifaces-backend-closeout-$STAMP.tar.gz"

fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
for x in gh git tar ssh scp; do need "$x"; done
[[ "$(uname -s)" == "Darwin" ]] || fail "run this from the Mac release environment"
trap 'rm -rf "$RUN" >/dev/null 2>&1 || true' EXIT
mkdir -p "$RUN/repo" "$RUN/patch"

echo "============================================================"
echo " desifaces.ai — PRODUCTION DB + BACKEND CLOSEOUT"
echo "============================================================"
echo "release_sha=$RELEASE_SHA"
echo "application_sha=$APPLICATION_SHA"
echo "web_action=NONE"
echo "mobile_action=NONE"

echo ""
echo "===== 1. VERIFY PRODUCTION PRESERVATION PRECONDITIONS ====="
H="$(ssh -o BatchMode=yes -o ConnectTimeout=12 "$SSH_HOST" 'hostname -s')" || fail "cannot SSH to $SSH_HOST"
[[ "$H" == desifaces-gpu* ]] || fail "wrong production host: $H"
ssh "$SSH_HOST" "test -f '$ROOT/RELEASE' && test -f '$ROOT/infra/.env' && test -s '$BASELINE_BACKUP' && docker inspect desifaces-db >/dev/null 2>&1 && docker inspect desifaces-redis >/dev/null 2>&1" || fail "production DB/Redis/package/baseline backup preflight failed"
echo "PRODUCTION_PRESERVATION_PREFLIGHT=PASS"

echo ""
echo "===== 2. FETCH CI-CERTIFIED BACKEND PATCH ON MAC ====="
gh repo clone "$REPO" "$RUN/repo" -- --filter=blob:none --no-checkout >/dev/null
(
  cd "$RUN/repo"
  git checkout --detach "$RELEASE_SHA" >/dev/null
  [[ "$(git rev-parse HEAD)" == "$RELEASE_SHA" ]]
)
for p in \
  services/svc-assistant/app/app/llm.py \
  services/svc-assistant/app/app/main.py \
  services/svc-assistant/app/tests/test_llm_startup_resilience.py \
  deploy/production/certify-production-data-backend-20260904.sh; do
  mkdir -p "$RUN/patch/$(dirname "$p")"
  cp "$RUN/repo/$p" "$RUN/patch/$p"
done
tar --no-xattrs -C "$RUN/patch" -czf "$PATCH" . 2>/dev/null || tar -C "$RUN/patch" -czf "$PATCH" .
[[ -s "$PATCH" ]] || fail "backend closeout patch is empty"
echo "BACKEND_PATCH_PACKAGE=PASS"

echo ""
echo "===== 3. TRANSFER PATCH + CERTIFY PRODUCTION DB/BACKEND ====="
scp -q "$PATCH" "$SSH_HOST:$REMOTE_PATCH"
ssh -tt "$SSH_HOST" "ROOT='$ROOT' REMOTE_PATCH='$REMOTE_PATCH' STAMP='$STAMP' BASELINE_BACKUP='$BASELINE_BACKUP' APPLICATION_SHA='$APPLICATION_SHA' RELEASE_SHA='$RELEASE_SHA' bash -s" <<'REMOTE'
set -Eeuo pipefail
BACKUP="/home/azureuser/backups/desifaces-backend-source-pre-closeout-$STAMP"
mkdir -p "$BACKUP"
FILES=(
  services/svc-assistant/app/app/llm.py
  services/svc-assistant/app/app/main.py
  services/svc-assistant/app/tests/test_llm_startup_resilience.py
  deploy/production/certify-production-data-backend-20260904.sh
)
for p in "${FILES[@]}"; do
  if [[ -f "$ROOT/$p" ]]; then mkdir -p "$BACKUP/$(dirname "$p")"; cp -p "$ROOT/$p" "$BACKUP/$p"; fi
done
cp -p "$ROOT/RELEASE" "$BACKUP/RELEASE"
rollback(){
  rc=$?
  set +e
  if (( rc != 0 )); then
    for p in "${FILES[@]}"; do [[ -f "$BACKUP/$p" ]] && cp -p "$BACKUP/$p" "$ROOT/$p" || true; done
    cp -p "$BACKUP/RELEASE" "$ROOT/RELEASE" || true
    echo "BACKEND_SOURCE_ROLLBACK=APPLIED"
  fi
  rm -f "$REMOTE_PATCH"
  exit "$rc"
}
trap rollback EXIT

tar -xzf "$REMOTE_PATCH" -C "$ROOT"
chmod +x "$ROOT/deploy/production/certify-production-data-backend-20260904.sh"
BASELINE_BACKUP="$BASELINE_BACKUP" ROOT="$ROOT" bash "$ROOT/deploy/production/certify-production-data-backend-20260904.sh"
python3 - "$ROOT/RELEASE" "$APPLICATION_SHA" "$RELEASE_SHA" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); app=sys.argv[2]; rel=sys.argv[3]
lines=p.read_text().splitlines()
out=[]; seen_app=False; seen_rel=False
for line in lines:
    if line.startswith('backend_application_sha='):
        out.append('backend_application_sha='+app); seen_app=True
    elif line.startswith('backend_closeout_release_sha='):
        out.append('backend_closeout_release_sha='+rel); seen_rel=True
    else:
        out.append(line)
if not seen_app: out.append('backend_application_sha='+app)
if not seen_rel: out.append('backend_closeout_release_sha='+rel)
p.write_text('\n'.join(out).rstrip()+'\n')
PY
rm -f "$REMOTE_PATCH"
trap - EXIT
echo "PRODUCTION_DB_BACKEND_CLOSEOUT=PASS"
REMOTE

echo ""
echo "============================================================"
echo " DB + BACKEND CLOSEOUT PASS"
echo "============================================================"
echo "CUSTOMER_DATA_PRESERVATION=PASS"
echo "PRODUCTION_DATABASE=PASS"
echo "PRODUCTION_BACKEND=PASS"
echo "NEXT_PHASE=WEB"
