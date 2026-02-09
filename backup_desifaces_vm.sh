#!/usr/bin/env bash
# backup_desifaces_vm.sh — DesiFaces VM backup (code + env/config + compose + postgres dump + optional volumes)
#
# Defaults match your VM:
#   REPO=/home/azureuser/workspace/desifaces-v2
#   COMPOSE_DIR=/home/azureuser/workspace/desifaces-v2/infra
#   OUT_BASE=/home/azureuser/vm_backups/desifaces
#
# Usage:
#   chmod +x ./backup_desifaces_vm.sh
#   ./backup_desifaces_vm.sh
#
# Optional overrides:
#   ENCRYPT=1 INCLUDE_VOLUMES=1 VOLUME_LIST="vol1 vol2" DB_SVC=desifaces-db DB_NAME=desifaces ./backup_desifaces_vm.sh
#   INCLUDE_PRINTENV=0 (skip printenv snapshot)
#   INCLUDE_LETSENCRYPT=1 (include /etc/letsencrypt snapshot - sensitive)

set -euo pipefail

# -----------------------------
# CONFIG (override via env vars)
# -----------------------------
TS="${TS:-$(date +%Y%m%d_%H%M%S)}"

REPO="${REPO:-/home/azureuser/workspace/desifaces-v2}"
COMPOSE_DIR="${COMPOSE_DIR:-$REPO/infra}"

OUT_BASE="${OUT_BASE:-/home/azureuser/vm_backups/desifaces}"
OUT_DIR="$OUT_BASE/backup_$TS"
ARCHIVE_DIR="$OUT_BASE/archives"

# DB (docker compose service + database)
DB_SVC="${DB_SVC:-desifaces-db}"
DB_USER="${DB_USER:-postgres}"
DB_NAME="${DB_NAME:-desifaces}"

# Optional: volume backups
INCLUDE_VOLUMES="${INCLUDE_VOLUMES:-0}"         # 0/1
VOLUME_LIST="${VOLUME_LIST:-}"                  # "volA volB volC"

# Optional: snapshot of current shell env (contains secrets!)
INCLUDE_PRINTENV="${INCLUDE_PRINTENV:-1}"       # 0/1

# Optional: include SSH keys (VERY sensitive). Default off.
INCLUDE_SSH_KEYS="${INCLUDE_SSH_KEYS:-0}"       # 0/1

# Optional: include Let's Encrypt certs (VERY sensitive; may contain private keys)
INCLUDE_LETSENCRYPT="${INCLUDE_LETSENCRYPT:-0}" # 0/1

# Encrypt final archive with gpg symmetric AES256 if available
ENCRYPT="${ENCRYPT:-1}"                         # 0/1

# Exclusions for repo tarball
TAR_EXCLUDES=(
  "--exclude-vcs"
  "--exclude=**/node_modules"
  "--exclude=**/.next"
  "--exclude=**/dist"
  "--exclude=**/build"
  "--exclude=**/__pycache__"
  "--exclude=**/.pytest_cache"
  "--exclude=**/.cache"
  "--exclude=**/.venv"
)

# -----------------------------
# Helpers
# -----------------------------
log() { echo "[$(date -Is)] $*"; }
have_cmd() { command -v "$1" >/dev/null 2>&1; }

try() { # run command but don't fail the script
  set +e
  "$@"
  local rc=$?
  set -e
  return $rc
}

sudo_ok() { sudo -n true >/dev/null 2>&1; }

# -----------------------------
# Setup
# -----------------------------
mkdir -p "$OUT_DIR" "$ARCHIVE_DIR"
log "Backup output: $OUT_DIR"
log "Repo: $REPO"
log "Compose: $COMPOSE_DIR"

if [[ ! -d "$REPO" ]]; then
  echo "ERROR: REPO not found: $REPO"
  exit 1
fi

if ! have_cmd tar; then
  echo "ERROR: tar not installed"
  exit 1
fi

# docker/compose optional
DOCKER_OK=0
if have_cmd docker && docker version >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  DOCKER_OK=1
fi

# -----------------------------
# 1) Code snapshot
# -----------------------------
log "Creating code tarball..."
tar -czf "$OUT_DIR/desifaces_code_$TS.tgz" \
  "${TAR_EXCLUDES[@]}" \
  -C "$(dirname "$REPO")" "$(basename "$REPO")"

# -----------------------------
# 2) Env/config files in repo
# -----------------------------
log "Collecting env/config files from repo..."
mkdir -p "$OUT_DIR/repo_files_snapshot"

find "$REPO" -maxdepth 8 -type f \( \
  -name ".env" -o -name ".env.*" -o -name "*.env" -o -name "*env*" \
  -o -name "docker-compose*.yml" -o -name "docker-compose*.yaml" \
  -o -name "*.yml" -o -name "*.yaml" -o -name "*.toml" -o -name "*.ini" \
  -o -name "*.conf" \
\) > "$OUT_DIR/repo_config_files_list_$TS.txt" 2>/dev/null || true

while IFS= read -r f; do
  rel="${f#$REPO/}"
  dest="$OUT_DIR/repo_files_snapshot/$rel"
  mkdir -p "$(dirname "$dest")"
  cp -a "$f" "$dest" 2>/dev/null || true
done < "$OUT_DIR/repo_config_files_list_$TS.txt"

if [[ "$INCLUDE_PRINTENV" == "1" ]]; then
  log "Saving printenv snapshot (contains secrets)..."
  printenv | sort > "$OUT_DIR/printenv_$TS.txt"
else
  log "INCLUDE_PRINTENV=0 (skipping printenv snapshot)"
fi

# -----------------------------
# 3) Docker compose snapshots
# -----------------------------
if [[ "$DOCKER_OK" == "1" && -d "$COMPOSE_DIR" ]]; then
  log "Saving docker compose resolved config + status..."
  try bash -lc "cd '$COMPOSE_DIR' && docker compose version" > "$OUT_DIR/docker_compose_version_$TS.txt" 2>&1 || true
  try bash -lc "cd '$COMPOSE_DIR' && docker compose ps -a" > "$OUT_DIR/compose_ps_$TS.txt" 2>&1 || true
  try bash -lc "cd '$COMPOSE_DIR' && docker compose config" > "$OUT_DIR/compose_resolved_config_$TS.yml" 2>&1 || true
else
  log "Docker compose not available or COMPOSE_DIR missing; skipping compose snapshots."
fi

# -----------------------------
# 4) System/app configs (best effort)
# -----------------------------
log "Saving system configs (best effort)..."
mkdir -p "$OUT_DIR/system"

cp -a ~/.bashrc ~/.profile ~/.bash_profile ~/.zshrc "$OUT_DIR/system/" 2>/dev/null || true

# SSH: include config + known_hosts always; keys only if explicitly enabled
mkdir -p "$OUT_DIR/system/ssh"
cp -a ~/.ssh/config "$OUT_DIR/system/ssh/" 2>/dev/null || true
cp -a ~/.ssh/known_hosts "$OUT_DIR/system/ssh/" 2>/dev/null || true
if [[ "$INCLUDE_SSH_KEYS" == "1" ]]; then
  log "INCLUDE_SSH_KEYS=1 (including SSH keys — sensitive!)"
  cp -a ~/.ssh/id_* "$OUT_DIR/system/ssh/" 2>/dev/null || true
else
  log "INCLUDE_SSH_KEYS=0 (not copying SSH private keys)"
fi

# Cron (fixed + quiet)
{ crontab -l 2>/dev/null || true; } > "$OUT_DIR/system/crontab_user_$TS.txt"
if sudo_ok; then
  { sudo -n crontab -l 2>/dev/null || true; } > "$OUT_DIR/system/crontab_root_$TS.txt"
else
  echo "Skipped root crontab (sudo -n not available)" > "$OUT_DIR/system/crontab_root_$TS.txt"
fi

# /etc configs: only if sudo -n works (won't hang)
if sudo_ok; then
  try sudo -n cp -a /etc/docker "$OUT_DIR/system/etc_docker" 2>/dev/null || true
  try sudo -n cp -a /etc/systemd/system "$OUT_DIR/system/etc_systemd_system" 2>/dev/null || true
  try sudo -n cp -a /etc/nginx "$OUT_DIR/system/etc_nginx" 2>/dev/null || true

  if [[ "$INCLUDE_LETSENCRYPT" == "1" ]]; then
    log "INCLUDE_LETSENCRYPT=1 (including /etc/letsencrypt — sensitive!)"
    # NOTE: may contain private keys; stored as root-owned in backup
    try sudo -n cp -a /etc/letsencrypt "$OUT_DIR/system/etc_letsencrypt" 2>/dev/null || true
  else
    log "INCLUDE_LETSENCRYPT=0 (skipping /etc/letsencrypt)"
  fi

  try sudo -n ufw status verbose > "$OUT_DIR/system/ufw_status_$TS.txt" 2>&1 || true
else
  log "sudo -n not available; skipping /etc/* and ufw snapshots."
fi

# -----------------------------
# 5) Postgres dumps (best effort)
# -----------------------------
if [[ "$DOCKER_OK" == "1" && -d "$COMPOSE_DIR" ]]; then
  log "Dumping Postgres (globals + custom dump) via docker compose..."
  try bash -lc "cd '$COMPOSE_DIR' && docker compose exec -T '$DB_SVC' pg_dumpall --globals-only -U '$DB_USER'" \
    > "$OUT_DIR/postgres_globals_$TS.sql" 2> "$OUT_DIR/postgres_globals_err_$TS.txt" || true

  try bash -lc "cd '$COMPOSE_DIR' && docker compose exec -T '$DB_SVC' pg_dump -U '$DB_USER' -d '$DB_NAME' -Fc" \
    > "$OUT_DIR/postgres_${DB_NAME}_$TS.dump" 2> "$OUT_DIR/postgres_dump_err_$TS.txt" || true
else
  log "Docker compose not available; skipping Postgres dump."
fi

# -----------------------------
# 6) Optional: Docker volume tarballs
# -----------------------------
if [[ "$INCLUDE_VOLUMES" == "1" && "$DOCKER_OK" == "1" ]]; then
  log "Backing up docker volumes..."
  docker volume ls > "$OUT_DIR/docker_volumes_$TS.txt" 2>&1 || true

  if [[ -z "$VOLUME_LIST" ]]; then
    log "INCLUDE_VOLUMES=1 but VOLUME_LIST is empty; skipping volume backups."
  else
    for vol in $VOLUME_LIST; do
      log "Backing up volume: $vol"
      try docker run --rm \
        -v "${vol}:/v:ro" \
        -v "$OUT_DIR:/b" \
        alpine sh -c "cd /v && tar -czf /b/volume_${vol}_$TS.tgz ."
    done
  fi
else
  [[ "$INCLUDE_VOLUMES" == "1" ]] && log "INCLUDE_VOLUMES=1 but docker not available; skipping volume backups."
fi

# -----------------------------
# 7) Inventory + manifest
# -----------------------------
log "Writing inventory + manifest..."
{
  echo "timestamp=$TS"
  echo "repo=$REPO"
  echo "compose_dir=$COMPOSE_DIR"
  echo "docker_ok=$DOCKER_OK"
  echo "db_svc=$DB_SVC"
  echo "db_user=$DB_USER"
  echo "db_name=$DB_NAME"
  echo "include_printenv=$INCLUDE_PRINTENV"
  echo "include_ssh_keys=$INCLUDE_SSH_KEYS"
  echo "include_letsencrypt=$INCLUDE_LETSENCRYPT"
  echo "include_volumes=$INCLUDE_VOLUMES"
  echo "volume_list=$VOLUME_LIST"
} > "$OUT_DIR/manifest_$TS.txt"

uname -a > "$OUT_DIR/uname_$TS.txt" 2>&1 || true
try lsb_release -a > "$OUT_DIR/lsb_release_$TS.txt" 2>&1 || true
try df -h > "$OUT_DIR/df_h_$TS.txt" 2>&1 || true
try free -h > "$OUT_DIR/free_h_$TS.txt" 2>&1 || true

if [[ "$DOCKER_OK" == "1" ]]; then
  docker version > "$OUT_DIR/docker_version_$TS.txt" 2>&1 || true
  docker info > "$OUT_DIR/docker_info_$TS.txt" 2>&1 || true
  docker images > "$OUT_DIR/docker_images_$TS.txt" 2>&1 || true
fi

# -----------------------------
# 8) Pack into one archive (+ optional encryption)
# -----------------------------
log "Packing backup folder to archive..."
ARCHIVE="$ARCHIVE_DIR/desifaces_backup_$TS.tgz"

# Use --ignore-failed-read so root-only files (if any) don't abort the archive creation
# If sudo -n is available, pack with sudo for completeness (especially if INCLUDE_LETSENCRYPT=1)
if sudo_ok; then
  sudo -n tar --ignore-failed-read -czf "$ARCHIVE" -C "$OUT_BASE" "backup_$TS"
  sudo -n chown "$(id -u):$(id -g)" "$ARCHIVE" || true
else
  tar --ignore-failed-read -czf "$ARCHIVE" -C "$OUT_BASE" "backup_$TS"
fi

if [[ "$ENCRYPT" == "1" ]]; then
  if have_cmd gpg; then
    log "Encrypting archive with gpg (AES256 symmetric)..."
    gpg --symmetric --cipher-algo AES256 "$ARCHIVE"
    rm -f "$ARCHIVE"
    log "DONE: $ARCHIVE.gpg"
  else
    log "ENCRYPT=1 but gpg not installed; leaving unencrypted: $ARCHIVE"
  fi
else
  log "DONE: $ARCHIVE"
fi

log "Backup complete."