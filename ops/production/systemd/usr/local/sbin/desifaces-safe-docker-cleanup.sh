#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="/var/log/desifaces-safe-docker-cleanup.log"
LOCK_FILE="/var/lock/desifaces-safe-docker-cleanup.lock"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date -Is) cleanup already running; exiting" | tee -a "$LOG_FILE"
  exit 0
fi

{
  echo "============================================================"
  echo "$(date -Is) desifaces SAFE Docker cleanup starting"
  echo "Host: $(hostname)"
  echo

  echo "== BEFORE: disk =="
  df -h /
  echo

  echo "== BEFORE: docker usage =="
  docker system df || true
  echo

  echo "== SAFETY: Docker volumes are NOT pruned by this script =="
  docker volume ls -q | wc -l | awk '{print "Docker volume count before: " $1}'
  echo

  echo "== Remove stopped containers older than 7 days =="
  docker container prune -f --filter "until=168h" || true
  echo

  echo "== Remove Docker build cache older than 7 days =="
  docker builder prune -af --filter "until=168h" || true
  echo

  echo "== Remove unused Docker images older than 14 days =="
  docker image prune -af --filter "until=336h" || true
  echo

  echo "== Clean apt cache =="
  apt-get clean || true
  echo

  echo "== Vacuum system journal older than 14 days =="
  journalctl --vacuum-time=14d || true
  echo

  echo "== Truncate Docker JSON logs larger than 200MB =="
  if [ -d /var/lib/docker/containers ]; then
    find /var/lib/docker/containers \
      -type f \
      -name '*-json.log' \
      -size +200M \
      -print \
      -exec truncate -s 0 {} \; || true
  fi
  echo

  echo "== SAFETY: volume count after cleanup =="
  docker volume ls -q | wc -l | awk '{print "Docker volume count after: " $1}'
  echo

  echo "== AFTER: docker usage =="
  docker system df || true
  echo

  echo "== AFTER: disk =="
  df -h /
  echo

  echo "$(date -Is) desifaces SAFE Docker cleanup completed"
  echo "============================================================"
  echo
} | tee -a "$LOG_FILE"
