#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || {
  echo "FAIL: DEV host required"
  exit 1
}

echo "============================================================"
echo " desifaces DEV — TERMINAL / HOST HEALTH"
echo "============================================================"

echo
echo "===== UPTIME / LOAD ====="
uptime || true

echo
echo "===== MEMORY ====="
free -h || true

echo
echo "===== DISK ====="
df -h / /home 2>/dev/null || df -h / || true

echo
echo "===== KERNEL OOM / KILLS (LAST 2H) ====="
timeout 8s journalctl -k --since "2 hours ago" --no-pager 2>/dev/null   | grep -Ei 'out of memory|oom-kill|killed process|segfault'   | tail -n 30 || true

echo
echo "===== SSH SESSION EVENTS (LAST 2H) ====="
timeout 8s journalctl -u ssh --since "2 hours ago" --no-pager 2>/dev/null   | grep -Ei 'disconnect|timed out|timeout|closed|reset|broken pipe|failed'   | tail -n 40 || true

echo
echo "===== DOCKER ENGINE ====="
timeout 8s docker info --format 'containers={{.Containers}} running={{.ContainersRunning}} paused={{.ContainersPaused}} stopped={{.ContainersStopped}} images={{.Images}} driver={{.Driver}}' 2>/dev/null || true

echo
echo "===== LARGE LOG FILES UNDER /tmp ====="
find /tmp -maxdepth 1 -type f -printf '%s %p\n' 2>/dev/null   | sort -nr   | head -n 15   | awk '{printf "%.1f MiB %s\n",$1/1048576,$2}' || true

echo
echo "============================================================"
echo " TERMINAL_HOST_HEALTH_CAPTURE=PASS"
echo "============================================================"
