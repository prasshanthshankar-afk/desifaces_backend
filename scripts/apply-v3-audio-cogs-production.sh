#!/usr/bin/env bash
set -Eeuo pipefail

[[ "${DF_PRODUCTION_CONFIRM:-}" == "YES" ]] || { echo "FAIL: set DF_PRODUCTION_CONFIRM=YES"; exit 1; }
[[ -n "${DF_PRODUCTION_HOSTNAME:-}" ]] || { echo "FAIL: set DF_PRODUCTION_HOSTNAME"; exit 1; }
ACTUAL_HOST="$(hostname -s)"
[[ "$ACTUAL_HOST" == "$DF_PRODUCTION_HOSTNAME" ]] || { echo "FAIL: host mismatch actual=$ACTUAL_HOST expected=$DF_PRODUCTION_HOSTNAME"; exit 1; }
[[ "$ACTUAL_HOST" != "desifaces-dev" ]] || { echo "FAIL: production migration forbidden on DEV"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MIGRATION="$REPO_ROOT/migrations/2026_09_12_audio_tts_cost_basis.sql"
EXPECTED_BLOB="f0c9bc640b183571e8f8ee5eccb6709b0b81fa59"
[[ -f "$MIGRATION" ]] || { echo "FAIL: migration missing"; exit 1; }
[[ "$(git hash-object "$MIGRATION")" == "$EXPECTED_BLOB" ]] || { echo "FAIL: immutable migration provenance mismatch"; exit 1; }
echo "MIGRATION_PROVENANCE=PASS"

DB="${DF_DB_CONTAINER:-}"
if [[ -z "$DB" ]]; then
  for candidate in desifaces-v3-db df-v3-db desifaces-db; do
    if docker inspect "$candidate" >/dev/null 2>&1; then DB="$candidate"; break; fi
  done
fi
[[ -n "$DB" ]] || { echo "FAIL: postgres container not found"; exit 1; }
PGUSER="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_USER"{print $2; exit}')"
PGDB="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_DB"{print $2; exit}')"
PGUSER="${PGUSER:-postgres}"; PGDB="${PGDB:-postgres}"
PSQL=(docker exec -i "$DB" psql -X -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB")

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$HOME/backups/desifaces-production-audio-cogs-$STAMP.sql"
mkdir -p "$(dirname "$BACKUP")"
docker exec "$DB" pg_dump -U "$PGUSER" -d "$PGDB" --data-only --inserts --table=public.pricing_sku_costs > "$BACKUP"
chmod 600 "$BACKUP"
echo "COGS_TABLE_BACKUP=PASS path=$BACKUP"

pricing_hash() {
  "${PSQL[@]}" -At -c "
  select md5(coalesce(string_agg(j,'|' order by j),'')) from (
    select to_jsonb(s)::text j from public.pricing_skus s
     where code in ('AUDIO_TTS_1K_CHARS','IMG_STD_RUN','FUSION_TALK_MIN')
    union all
    select to_jsonb(v)::text j from public.pricing_variant_lines v
     where sku_code in ('AUDIO_TTS_1K_CHARS','IMG_STD_RUN','FUSION_TALK_MIN')
  ) q;"
}
BEFORE="$(pricing_hash)"

cat "$MIGRATION" | "${PSQL[@]}"
AFTER="$(pricing_hash)"
[[ "$BEFORE" == "$AFTER" ]] || { echo "FAIL: customer pricing changed; stop production cutover"; exit 1; }
echo "CUSTOMER_PRICING_IMMUTABILITY=PASS"

DF_DB_CONTAINER="$DB" bash "$REPO_ROOT/scripts/certify-v3-production-economics-costs.sh"

echo "============================================================"
echo " PRODUCTION AUDIO COGS MIGRATION=PASS"
echo "============================================================"
echo "host=$ACTUAL_HOST"
echo "customer_pricing_change=NONE"
echo "audio_cost_usd_per_1k_chars=0.016"
echo "economics_fail_closed=PASS"
echo "backup=$BACKUP"
