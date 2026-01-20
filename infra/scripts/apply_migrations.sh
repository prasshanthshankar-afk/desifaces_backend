# infra/scripts/apply_migrations.sh
#!/usr/bin/env bash
set -euo pipefail

# -------------------------------
# Config (override via env)
# -------------------------------
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-desifaces}"
DB_USER="${DB_USER:-desifaces_admin}"
DB_PASSWORD="${DB_PASSWORD:-desifaces_mahadev}"

MIGRATIONS_DIR="${MIGRATIONS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../migrations" && pwd)}"

export PGPASSWORD="$DB_PASSWORD"

psql_base=(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1)

echo "== DesiFaces migrations =="
echo "DB: postgresql://${DB_USER}:*****@${DB_HOST}:${DB_PORT}/${DB_NAME}"
echo "Migrations dir: ${MIGRATIONS_DIR}"
echo

# -------------------------------
# Helper
# -------------------------------
apply_file() {
  local f="$1"
  if [[ ! -f "$f" ]]; then
    echo "ERROR: migration not found: $f" >&2
    exit 1
  fi
  echo "-> Applying $(basename "$f")"
  "${psql_base[@]}" -f "$f" >/dev/null
}

# -------------------------------
# Apply in order
# -------------------------------
apply_file "${MIGRATIONS_DIR}/000_bootstrap.sql"
apply_file "${MIGRATIONS_DIR}/010_core_auth.sql"
apply_file "${MIGRATIONS_DIR}/011_core_audit_and_feature_flags.sql"

# Studio Kernel (shared)
apply_file "${MIGRATIONS_DIR}/020_studio_kernel.sql"

# Media Library (shared)
apply_file "${MIGRATIONS_DIR}/020_media_assets.sql"

# Studio extensions
apply_file "${MIGRATIONS_DIR}/030_face_jobs.sql"
apply_file "${MIGRATIONS_DIR}/040_audio_jobs.sql"
apply_file "${MIGRATIONS_DIR}/050_fusion_jobs.sql"

echo
echo "✅ All migrations applied successfully."