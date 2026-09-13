#!/usr/bin/env bash
set -Eeuo pipefail

# Source-access adapter for the certified preserve-data production cutover.
# This adapter changes SOURCE ACQUISITION ONLY. It does not change the database
# policy: PROD remains authoritative; DEV data import and live DB restore remain
# forbidden; exactly one targeted COGS migration remains allowlisted.

BASE_COMMIT="92d5568b34c1eb808b76f8461d92507409fa11fa"
BASE_BLOB="f496ef1e3c49953ee4880a3e3e5c81e25b4baea5"
BASE_URL="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${BASE_COMMIT}/scripts/launch-v3-production-preserve-data-20260912.sh"
BACKEND_REPO="prasshanthshankar-afk/desifaces_backend"
BACKEND_SHA="18dfd6a3a4941307a466960108e4573f3b9ff555"
WEB_REPO="prasshanthshankar-afk/desifaces_web"
WEB_SHA="21d1c8d4083c7f9705b957807e12d3d2bdf518d8"
WEB_GHCR="ghcr.io/prasshanthshankar-afk/desifaces-web:${WEB_SHA}"
RUN="/tmp/desifaces-source-access-$(date -u +%Y%m%dT%H%M%SZ)"
BASE="$RUN/preserve-data-launcher.sh"
SHIMDIR="$RUN/bin"
WEB_MODE=""
WEB_REMOTE=""

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }

[[ "${DESIFACES_PRODUCTION_CUTOVER_APPROVED:-}" == "YES" ]] || fail "explicit production approval missing"
for x in curl git tar gzip docker ssh; do need "$x"; done
mkdir -p "$RUN" "$SHIMDIR"

HOST="$(hostname -s 2>/dev/null || hostname)"
[[ "$HOST" == "desifaces-gpu" ]] || fail "unexpected production guest host: $HOST"

log "===== SOURCE ACCESS ADAPTER — PRE-MUTATION ====="
log "prod_data_source=EXISTING_PRODUCTION_ONLY"
log "dev_data_import=FORBIDDEN"
log "live_db_restore=FORBIDDEN"

# Fetch and pin the already-certified preserve-data launcher from the public
# backend repository. This is read-only source retrieval.
curl -fsSL "$BASE_URL" -o "$BASE"
[[ "$(git hash-object "$BASE")" == "$BASE_BLOB" ]] || fail "base preserve-data launcher provenance mismatch"
bash -n "$BASE"
log "BASE_PRESERVE_DATA_LAUNCHER=PASS"

# Backend source is public, so no GitHub CLI authentication is necessary.
curl -fsSL -o /dev/null "https://api.github.com/repos/${BACKEND_REPO}/commits/${BACKEND_SHA}" || fail "backend release commit inaccessible"
log "BACKEND_PUBLIC_RELEASE_ACCESS=PASS"

# Resolve private Web source access without requiring gh authentication.
# Prefer existing SSH credentials, then existing HTTPS credential helper. If
# neither exists, use the exact Web CI image tag if the registry is readable
# with current Docker credentials/visibility. All checks happen before the base
# launcher can touch production source, DB, containers or nginx.
if GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10' \
     timeout 20 git ls-remote "git@github.com:${WEB_REPO}.git" "$WEB_SHA" 2>/dev/null | grep -q "$WEB_SHA"; then
  WEB_MODE="git_ssh"
  WEB_REMOTE="git@github.com:${WEB_REPO}.git"
elif GIT_TERMINAL_PROMPT=0 timeout 20 git ls-remote "https://github.com/${WEB_REPO}.git" "$WEB_SHA" 2>/dev/null | grep -q "$WEB_SHA"; then
  WEB_MODE="git_https"
  WEB_REMOTE="https://github.com/${WEB_REPO}.git"
elif timeout 60 docker pull "$WEB_GHCR" >/dev/null 2>&1; then
  WEB_MODE="ghcr_exact_commit_tag"
else
  fail "private Web release inaccessible: no SSH/HTTPS Git access and exact GHCR image cannot be pulled"
fi

log "WEB_RELEASE_ACCESS=PASS mode=$WEB_MODE"
if [[ "$WEB_MODE" == "ghcr_exact_commit_tag" ]]; then
  WEB_IMAGE_ID="$(docker image inspect "$WEB_GHCR" --format '{{.Id}}')"
  [[ -n "$WEB_IMAGE_ID" ]] || fail "pulled Web image has no image id"
  log "WEB_GHCR_IMAGE_ID=$WEB_IMAGE_ID"
  log "WEB_SOURCE_BUILD_MODE=PREBUILT_CERTIFIED_IMAGE"
else
  log "WEB_SOURCE_BUILD_MODE=EXACT_GIT_COMMIT"
fi

# Build a narrowly scoped gh-compatible shim for only the API calls made by the
# pinned preserve-data launcher. It does not expose or persist credentials.
cat > "$SHIMDIR/gh" <<'SHIM'
#!/usr/bin/env bash
set -Eeuo pipefail
BACKEND_REPO="prasshanthshankar-afk/desifaces_backend"
BACKEND_SHA="18dfd6a3a4941307a466960108e4573f3b9ff555"
WEB_REPO="prasshanthshankar-afk/desifaces_web"
WEB_SHA="21d1c8d4083c7f9705b957807e12d3d2bdf518d8"
WEB_GHCR="ghcr.io/prasshanthshankar-afk/desifaces-web:${WEB_SHA}"
MODE="${DF_WEB_SOURCE_MODE:?}"
REMOTE="${DF_WEB_SOURCE_REMOTE:-}"

if [[ "${1:-}" == "auth" && "${2:-}" == "status" ]]; then
  exit 0
fi

[[ "${1:-}" == "api" ]] || { echo "unsupported gh shim command" >&2; exit 2; }
shift
# Ignore headers/options used by the pinned launcher while retaining the endpoint.
endpoint=""
while (($#)); do
  case "$1" in
    -H|--header) shift 2 ;;
    --jq) shift 2 ;;
    -*) shift ;;
    *) endpoint="$1"; shift ;;
  esac
done
[[ -n "$endpoint" ]] || { echo "missing endpoint" >&2; exit 2; }

case "$endpoint" in
  "repos/$BACKEND_REPO/commits/$BACKEND_SHA")
    printf '%s\n' "$BACKEND_SHA"
    ;;
  "repos/$WEB_REPO/commits/$WEB_SHA")
    printf '%s\n' "$WEB_SHA"
    ;;
  "repos/$BACKEND_REPO/tarball/$BACKEND_SHA")
    exec curl -fsSL "https://api.github.com/repos/$BACKEND_REPO/tarball/$BACKEND_SHA"
    ;;
  "repos/$WEB_REPO/tarball/$WEB_SHA")
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    if [[ "$MODE" == git_ssh || "$MODE" == git_https ]]; then
      GIT_TERMINAL_PROMPT=0 git clone -q --no-checkout "$REMOTE" "$tmp/repo"
      git -C "$tmp/repo" checkout -q --detach "$WEB_SHA"
      git -C "$tmp/repo" archive --format=tar --prefix=release/ "$WEB_SHA" | gzip -c
    elif [[ "$MODE" == ghcr_exact_commit_tag ]]; then
      mkdir -p "$tmp/release/web"
      printf 'FROM %s\n' "$WEB_GHCR" > "$tmp/release/web/Dockerfile"
      tar -C "$tmp" -czf - release
    else
      echo "unsupported Web source mode: $MODE" >&2; exit 2
    fi
    ;;
  *)
    echo "unsupported gh shim endpoint: $endpoint" >&2
    exit 2
    ;;
esac
SHIM
chmod 755 "$SHIMDIR/gh"
bash -n "$SHIMDIR/gh"

export DF_WEB_SOURCE_MODE="$WEB_MODE"
export DF_WEB_SOURCE_REMOTE="$WEB_REMOTE"
export PATH="$SHIMDIR:$PATH"

log "SOURCE_ACCESS_PREMUTATION_GATE=PASS"
log "PRODUCTION_RUNTIME_MUTATION_BEFORE_GATE=NONE"
log "DATABASE_MUTATION_BEFORE_GATE=NONE"

exec bash "$BASE"
