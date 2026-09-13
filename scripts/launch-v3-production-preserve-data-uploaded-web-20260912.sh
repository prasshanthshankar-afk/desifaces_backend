#!/usr/bin/env bash
set -Eeuo pipefail

# Source-acquisition adapter for the certified preserve-data production cutover.
# The private Web tarball is supplied through a short-lived URL and is verified
# by SHA-256 before the underlying launcher is allowed to mutate production.
# Database policy is unchanged: PROD is authoritative, DEV import and live DB
# restore are forbidden, and exactly one targeted COGS migration is allowlisted.

BASE_COMMIT="92d5568b34c1eb808b76f8461d92507409fa11fa"
BASE_BLOB="f496ef1e3c49953ee4880a3e3e5c81e25b4baea5"
BASE_URL="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${BASE_COMMIT}/scripts/launch-v3-production-preserve-data-20260912.sh"
BACKEND_REPO="prasshanthshankar-afk/desifaces_backend"
BACKEND_SHA="18dfd6a3a4941307a466960108e4573f3b9ff555"
WEB_REPO="prasshanthshankar-afk/desifaces_web"
WEB_SHA="21d1c8d4083c7f9705b957807e12d3d2bdf518d8"
RUN="/tmp/desifaces-uploaded-web-source-$(date -u +%Y%m%dT%H%M%SZ)"
BASE="$RUN/preserve-data-launcher.sh"
WEB_TARBALL="$RUN/web-release.tar.gz"
SHIMDIR="$RUN/bin"

log(){ printf '%s\n' "$*"; }
fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }

[[ "${DESIFACES_PRODUCTION_CUTOVER_APPROVED:-}" == "YES" ]] || fail "explicit production approval missing"
[[ -n "${DF_WEB_SOURCE_TARBALL_URL:-}" ]] || fail "DF_WEB_SOURCE_TARBALL_URL is required"
[[ "${DF_WEB_SOURCE_TARBALL_SHA256:-}" =~ ^[0-9a-fA-F]{64}$ ]] || fail "DF_WEB_SOURCE_TARBALL_SHA256 must be a 64-char SHA-256"

for x in curl git tar gzip sha256sum; do need "$x"; done
mkdir -p "$RUN" "$SHIMDIR"
chmod 700 "$RUN"

HOST="$(hostname -s 2>/dev/null || hostname)"
[[ "$HOST" == "desifaces-gpu" ]] || fail "unexpected production guest host: $HOST"

log "===== UPLOADED WEB SOURCE ADAPTER — PRE-MUTATION ====="
log "prod_data_source=EXISTING_PRODUCTION_ONLY"
log "dev_data_import=FORBIDDEN"
log "live_db_restore=FORBIDDEN"

curl -fsSL "$BASE_URL" -o "$BASE"
[[ "$(git hash-object "$BASE")" == "$BASE_BLOB" ]] || fail "base preserve-data launcher provenance mismatch"
bash -n "$BASE"
log "BASE_PRESERVE_DATA_LAUNCHER=PASS"

# Fetch the user-staged private Web tarball through its short-lived URL. Never
# print the URL because it can contain a SAS token.
curl -fsSL --retry 3 --retry-delay 2 "$DF_WEB_SOURCE_TARBALL_URL" -o "$WEB_TARBALL"
[[ -s "$WEB_TARBALL" ]] || fail "uploaded Web tarball is empty"
ACTUAL_SHA="$(sha256sum "$WEB_TARBALL" | awk '{print $1}')"
[[ "$ACTUAL_SHA" == "${DF_WEB_SOURCE_TARBALL_SHA256,,}" ]] || fail "uploaded Web tarball SHA-256 mismatch"

tar -tzf "$WEB_TARBALL" >/tmp/desifaces-web-tar-list.$$ || fail "uploaded Web tarball is not a valid gzip tar archive"
grep -Eq '^[^/]+/web/Dockerfile$' /tmp/desifaces-web-tar-list.$$ || fail "uploaded Web tarball missing web/Dockerfile"
grep -Eq '^[^/]+/package-lock\.json$|^[^/]+/web/package-lock\.json$' /tmp/desifaces-web-tar-list.$$ || fail "uploaded Web tarball missing lockfile"
rm -f /tmp/desifaces-web-tar-list.$$
log "UPLOADED_WEB_SOURCE_SHA256=PASS"
log "WEB_RELEASE_ACCESS=PASS mode=azure_blob_short_lived_tarball"

# Public backend release is independently reachable without GitHub credentials.
curl -fsSL -o /dev/null "https://api.github.com/repos/${BACKEND_REPO}/commits/${BACKEND_SHA}" || fail "backend release commit inaccessible"
log "BACKEND_PUBLIC_RELEASE_ACCESS=PASS"

# Provide only the exact gh API surface used by the pinned preserve-data launcher.
cat > "$SHIMDIR/gh" <<SHIM
#!/usr/bin/env bash
set -Eeuo pipefail
BACKEND_REPO="$BACKEND_REPO"
BACKEND_SHA="$BACKEND_SHA"
WEB_REPO="$WEB_REPO"
WEB_SHA="$WEB_SHA"
WEB_TARBALL="$WEB_TARBALL"

if [[ "\${1:-}" == "auth" && "\${2:-}" == "status" ]]; then
  exit 0
fi
[[ "\${1:-}" == "api" ]] || { echo "unsupported gh shim command" >&2; exit 2; }
shift
endpoint=""
while ((\$#)); do
  case "\$1" in
    -H|--header|--jq) shift 2 ;;
    -*) shift ;;
    *) endpoint="\$1"; shift ;;
  esac
done
[[ -n "\$endpoint" ]] || { echo "missing gh shim endpoint" >&2; exit 2; }
case "\$endpoint" in
  "repos/\$BACKEND_REPO/commits/\$BACKEND_SHA") printf '%s\n' "\$BACKEND_SHA" ;;
  "repos/\$WEB_REPO/commits/\$WEB_SHA") printf '%s\n' "\$WEB_SHA" ;;
  "repos/\$BACKEND_REPO/tarball/\$BACKEND_SHA") exec curl -fsSL "https://api.github.com/repos/\$BACKEND_REPO/tarball/\$BACKEND_SHA" ;;
  "repos/\$WEB_REPO/tarball/\$WEB_SHA") exec cat "\$WEB_TARBALL" ;;
  *) echo "unsupported gh shim endpoint: \$endpoint" >&2; exit 2 ;;
esac
SHIM
chmod 755 "$SHIMDIR/gh"
bash -n "$SHIMDIR/gh"

export PATH="$SHIMDIR:$PATH"
log "SOURCE_ACCESS_PREMUTATION_GATE=PASS"
log "PRODUCTION_RUNTIME_MUTATION_BEFORE_GATE=NONE"
log "DATABASE_MUTATION_BEFORE_GATE=NONE"

exec bash "$BASE"
