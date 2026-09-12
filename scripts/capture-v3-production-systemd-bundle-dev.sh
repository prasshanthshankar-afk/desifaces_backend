#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
REPO="prasshanthshankar-afk/desifaces_backend"
BRANCH="fix/v3-audio-cogs-production-readiness-20260912"
[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || { echo "FAIL: run only on desifaces-dev"; exit 1; }
command -v gh >/dev/null || { echo "FAIL: gh required"; exit 1; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ROOT="$TMP/ops/production/systemd"
mkdir -p "$ROOT/usr/local/bin" "$ROOT/usr/local/sbin" "$ROOT/etc/systemd/system"

copy_exact() {
  local src="$1" rel="$2"
  [[ -r "$src" ]] || { echo "FAIL: missing/unreadable $src"; exit 1; }
  cp -p "$src" "$ROOT/$rel"
}

copy_exact /usr/local/bin/desifaces-notification-dispatch.sh usr/local/bin/desifaces-notification-dispatch.sh
copy_exact /usr/local/sbin/desifaces-safe-docker-cleanup.sh usr/local/sbin/desifaces-safe-docker-cleanup.sh
copy_exact /etc/systemd/system/desifaces-notification-dispatch.service etc/systemd/system/desifaces-notification-dispatch.service
copy_exact /etc/systemd/system/desifaces-notification-dispatch.timer etc/systemd/system/desifaces-notification-dispatch.timer
copy_exact /etc/systemd/system/desifaces-safe-docker-cleanup.service etc/systemd/system/desifaces-safe-docker-cleanup.service
copy_exact /etc/systemd/system/desifaces-safe-docker-cleanup.timer etc/systemd/system/desifaces-safe-docker-cleanup.timer

# Fail closed if an installed scheduler file contains an obvious embedded secret.
if grep -RIE '(sk_(live|test)_[A-Za-z0-9]+|AccountKey=|-----BEGIN .*PRIVATE KEY-----|sig=[A-Za-z0-9%+/=]{20,})' "$ROOT"; then
  echo "FAIL: scheduler bundle appears to contain an embedded credential"
  exit 1
fi

(
  cd "$ROOT"
  find usr etc -type f -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
)

cat > "$ROOT/PROVENANCE.env" <<EOF
CAPTURE_HOST=$(hostname -s)
CAPTURED_AT_UTC=$STAMP
SOURCE_NOTIFICATION_TIMER=desifaces-notification-dispatch.timer
SOURCE_CLEANUP_TIMER=desifaces-safe-docker-cleanup.timer
POLICY=EXACT_DEV_CERTIFIED_BYTES_WITH_PRODUCTION_PREFLIGHT
EOF

upload() {
  local rel="$1" src="$ROOT/$1" dest="ops/production/systemd/$1" sha
  sha="$(gh api "repos/$REPO/contents/$dest?ref=$BRANCH" --jq .sha 2>/dev/null || true)"
  local content
  content="$(base64 -w0 "$src")"
  if [[ -n "$sha" ]]; then
    gh api --method PUT "repos/$REPO/contents/$dest" \
      -f message="ops(prod): refresh certified systemd scheduler bundle" \
      -f branch="$BRANCH" -f sha="$sha" -f content="$content" --silent
  else
    gh api --method PUT "repos/$REPO/contents/$dest" \
      -f message="ops(prod): capture certified systemd scheduler bundle" \
      -f branch="$BRANCH" -f content="$content" --silent
  fi
}

while IFS= read -r rel; do upload "$rel"; done < <(
  cd "$ROOT" && find usr etc -type f | sort
)
upload SHA256SUMS
upload PROVENANCE.env

echo "============================================================"
echo " SYSTEMD SCHEDULER BUNDLE CAPTURE=PASS"
echo "============================================================"
echo "source_host=$EXPECTED_HOST"
echo "branch=$BRANCH"
echo "notification_timer=CAPTURED"
echo "safe_cleanup_timer=CAPTURED"
echo "embedded_secret_scan=PASS"
echo "production_install=NOT_PERFORMED"
