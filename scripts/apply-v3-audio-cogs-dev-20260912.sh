#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
REPO="prasshanthshankar-afk/desifaces_backend"
BRANCH="fix/v3-audio-cogs-production-readiness-20260912"
MIGRATION="migrations/2026_09_12_audio_tts_cost_basis.sql"
EXPECTED_BLOB="f0c9bc640b183571e8f8ee5eccb6709b0b81fa59"
[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || { echo "FAIL: wrong host"; exit 1; }
command -v gh >/dev/null || { echo "FAIL: gh required"; exit 1; }

DB=""
for candidate in desifaces-v3-db df-v3-db desifaces-db; do
  if docker inspect "$candidate" >/dev/null 2>&1; then DB="$candidate"; break; fi
done
[[ -n "$DB" ]] || { echo "FAIL: postgres container not found"; exit 1; }
PGUSER="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_USER"{print $2; exit}')"
PGDB="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_DB"{print $2; exit}')"
PGUSER="${PGUSER:-postgres}"; PGDB="${PGDB:-postgres}"
PSQL=(docker exec -i "$DB" psql -X -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB")

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fetch_file() {
  local path="$1" out="$2"
  gh api "repos/$REPO/contents/$path?ref=$BRANCH" --jq .content | base64 -d > "$out"
}

fetch_file "$MIGRATION" "$TMP/migration.sql"
ACTUAL_BLOB="$(git hash-object "$TMP/migration.sql")"
[[ "$ACTUAL_BLOB" == "$EXPECTED_BLOB" ]] || { echo "FAIL: migration provenance mismatch $ACTUAL_BLOB"; exit 1; }
echo "MIGRATION_PROVENANCE=PASS"

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

echo "===== APPLY INTERNAL COGS MIGRATION ====="
cat "$TMP/migration.sql" | "${PSQL[@]}"
AFTER="$(pricing_hash)"
[[ "$BEFORE" == "$AFTER" ]] || { echo "FAIL: customer pricing/variant rows changed"; exit 1; }
echo "CUSTOMER_PRICING_IMMUTABILITY=PASS"

fetch_file scripts/certify-v3-production-economics-costs.sh "$TMP/certify.sh"
chmod +x "$TMP/certify.sh"
(cd "$HOME/workspace/desifaces-v3" && DF_DB_CONTAINER="$DB" bash "$TMP/certify.sh")

fetch_file scripts/audit-v3-story-economics-complete-readonly.sh "$TMP/economics.sh"
chmod +x "$TMP/economics.sh"
DF_DB_CONTAINER="$DB" bash "$TMP/economics.sh"

echo "============================================================"
echo " AUDIO COGS DEV CORRECTION=PASS"
echo "============================================================"
echo "environment=DEV_ONLY"
echo "audio_cost_usd_per_1k_chars=0.016"
echo "customer_pricing_change=NONE"
echo "database_change=INTERNAL_COGS_ONLY"
echo "production_touch=NONE"
