#!/usr/bin/env bash
set -Eeuo pipefail

[[ "${DF_PRODUCTION_CONFIRM:-}" == "YES" ]] || { echo "FAIL: set DF_PRODUCTION_CONFIRM=YES"; exit 1; }
[[ -n "${DF_PRODUCTION_HOSTNAME:-}" ]] || { echo "FAIL: set DF_PRODUCTION_HOSTNAME to the exact target hostname"; exit 1; }
ACTUAL_HOST="$(hostname -s)"
[[ "$ACTUAL_HOST" == "$DF_PRODUCTION_HOSTNAME" ]] || { echo "FAIL: host mismatch actual=$ACTUAL_HOST expected=$DF_PRODUCTION_HOSTNAME"; exit 1; }
[[ "$ACTUAL_HOST" != "desifaces-dev" ]] || { echo "FAIL: production installer forbidden on DEV"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../ops/production/systemd" 2>/dev/null && pwd || true)"
if [[ -z "$ROOT" ]]; then
  # Canonical repository layout: scripts/ + ops/ are siblings under repo root.
  ROOT="$(cd "$SCRIPT_DIR/.." && pwd)/ops/production/systemd"
fi
[[ -f "$ROOT/SHA256SUMS" ]] || { echo "FAIL: certified scheduler bundle missing"; exit 1; }

cd "$ROOT"
sha256sum -c SHA256SUMS

echo "SCHEDULER_BUNDLE_HASH_GATE=PASS"

# Exact DEV-certified bytes are source material, but production must not inherit
# explicit DEV host/path/container bindings. Fail closed and adapt deliberately.
if grep -RIE 'desifaces-dev|desifaces-v2|_dev([^A-Za-z0-9]|$)|-dev([^A-Za-z0-9]|$)' usr etc; then
  echo "FAIL: scheduler bundle contains DEV-specific binding; production adaptation required before install"
  exit 1
fi

if grep -RIE '(sk_(live|test)_[A-Za-z0-9]+|AccountKey=|-----BEGIN .*PRIVATE KEY-----|sig=[A-Za-z0-9%+/=]{20,})' usr etc; then
  echo "FAIL: scheduler bundle contains embedded credential material"
  exit 1
fi

echo "SCHEDULER_PRODUCTION_BINDING_GATE=PASS"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/var/backups/desifaces-systemd-$STAMP"
sudo mkdir -p "$BACKUP"

backup_if_present() {
  local path="$1"
  if sudo test -e "$path"; then
    sudo cp -a "$path" "$BACKUP/$(basename "$path")"
  fi
}

for path in \
  /usr/local/bin/desifaces-notification-dispatch.sh \
  /usr/local/sbin/desifaces-safe-docker-cleanup.sh \
  /etc/systemd/system/desifaces-notification-dispatch.service \
  /etc/systemd/system/desifaces-notification-dispatch.timer \
  /etc/systemd/system/desifaces-safe-docker-cleanup.service \
  /etc/systemd/system/desifaces-safe-docker-cleanup.timer; do
  backup_if_present "$path"
done

sudo install -o root -g root -m 0755 usr/local/bin/desifaces-notification-dispatch.sh /usr/local/bin/desifaces-notification-dispatch.sh
sudo install -o root -g root -m 0755 usr/local/sbin/desifaces-safe-docker-cleanup.sh /usr/local/sbin/desifaces-safe-docker-cleanup.sh
sudo install -o root -g root -m 0644 etc/systemd/system/desifaces-notification-dispatch.service /etc/systemd/system/desifaces-notification-dispatch.service
sudo install -o root -g root -m 0644 etc/systemd/system/desifaces-notification-dispatch.timer /etc/systemd/system/desifaces-notification-dispatch.timer
sudo install -o root -g root -m 0644 etc/systemd/system/desifaces-safe-docker-cleanup.service /etc/systemd/system/desifaces-safe-docker-cleanup.service
sudo install -o root -g root -m 0644 etc/systemd/system/desifaces-safe-docker-cleanup.timer /etc/systemd/system/desifaces-safe-docker-cleanup.timer

sudo systemctl daemon-reload
sudo systemctl enable --now desifaces-notification-dispatch.timer
sudo systemctl enable --now desifaces-safe-docker-cleanup.timer

for timer in desifaces-notification-dispatch.timer desifaces-safe-docker-cleanup.timer; do
  sudo systemctl is-enabled "$timer"
  sudo systemctl is-active "$timer"
done

echo "===== PRODUCTION TIMER SCHEDULE ====="
sudo systemctl list-timers --all --no-pager | grep -E 'desifaces-(notification-dispatch|safe-docker-cleanup)' || true

echo "============================================================"
echo " PRODUCTION SYSTEMD SCHEDULER INSTALL=PASS"
echo "============================================================"
echo "host=$ACTUAL_HOST"
echo "backup=$BACKUP"
echo "notification_dispatch_timer=ENABLED_ACTIVE"
echo "safe_docker_cleanup_timer=ENABLED_ACTIVE"
echo "manual_cleanup_run=NOT_PERFORMED"
