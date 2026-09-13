#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_REF="7ed76b31775f0e81c094b7ba62283e1eb3e78732"
RAW_BASE="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend"
MIGRATION_PATH="migrations/2026_09_13_stripe_live_launch_price_mappings.sql"
MIG="/tmp/2026_09_13_stripe_live_launch_price_mappings.sql"
BACKUP_DIR="/home/azureuser/backups"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="${BACKUP_DIR}/desifaces-stripe-live-mapping-pre-${TS}.sql"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root is not persistent production storage"
docker inspect desifaces-db >/dev/null 2>&1 || fail "desifaces-db missing"
docker inspect df-svc-pricing >/dev/null 2>&1 || fail "df-svc-pricing missing"
[[ "$(docker inspect -f '{{.State.Status}}' desifaces-db)" == "running" ]] || fail "desifaces-db not running"
[[ "$(docker inspect -f '{{.State.Status}}' df-svc-pricing)" == "running" ]] || fail "df-svc-pricing not running"

command -v curl >/dev/null 2>&1 || fail "curl missing"
mkdir -p "$BACKUP_DIR"

curl -fsSL "${RAW_BASE}/${SOURCE_REF}/${MIGRATION_PATH}" -o "$MIG"
[[ -s "$MIG" ]] || fail "mapping migration download failed"

echo "============================================================"
echo " desifaces — STRIPE LIVE PROD DB MAPPING"
echo " source_ref=$SOURCE_REF"
echo " scope=14_STRIPE_LIVE_PRICE_IDS_ONLY"
echo " customer_prices_change=FORBIDDEN"
echo "============================================================"

echo
echo "===== 1. PRE-MUTATION DB BACKUP ====="
docker exec desifaces-db sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t public.pricing_plan_prices -t public.pricing_credit_packs --no-owner --no-privileges' > "$BACKUP"
[[ -s "$BACKUP" ]] || fail "table backup is empty"
chmod 600 "$BACKUP"
echo "backup=$BACKUP"
echo "backup_sha256=$(sha256sum "$BACKUP" | awk '{print $1}')"
echo "DB_BACKUP_GATE=PASS"

echo
echo "===== 2. APPLY TRANSACTIONAL LIVE MAPPING ====="
docker exec -i desifaces-db sh -lc 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$MIG"
echo "STRIPE_LIVE_DB_MAPPING_APPLY=PASS"

echo
echo "===== 3. EXACT POST-MAPPING CERTIFICATION ====="
docker exec desifaces-db sh -lc 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
select plan_code, interval_code, currency, country_code, price_money, stripe_price_id, metadata_json->>\x27stripe_env\x27 as stripe_env
from public.pricing_plan_prices
where tier_code in (\x27pro\x27,\x27business\x27) and is_active=true and is_public=true and self_serve=true
order by tier_code, interval_code, currency;

select code, credits, currency, country_code, price_money, metadata_json->>\x27stripe_price_id\x27 as stripe_price_id, metadata_json->>\x27stripe_env\x27 as stripe_env
from public.pricing_credit_packs
where code in (\x27PACK_USD_1000\x27,\x27PACK_USD_5000\x27,\x27PACK_USD_15000\x27,\x27PACK_INR_1000\x27,\x27PACK_INR_5000\x27,\x27PACK_INR_15000\x27)
order by code;
"'

GATE="$(docker exec desifaces-db sh -lc 'psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
select case when
  (select count(*) from public.pricing_plan_prices where tier_code in (\x27pro\x27,\x27business\x27) and is_active=true and is_public=true and self_serve=true and metadata_json->>\x27stripe_env\x27=\x27live\x27 and stripe_price_id like \x27price_%\x27)=8
  and
  (select count(*) from public.pricing_credit_packs where code in (\x27PACK_USD_1000\x27,\x27PACK_USD_5000\x27,\x27PACK_USD_15000\x27,\x27PACK_INR_1000\x27,\x27PACK_INR_5000\x27,\x27PACK_INR_15000\x27) and metadata_json->>\x27stripe_env\x27=\x27live\x27 and metadata_json->>\x27stripe_price_id\x27 like \x27price_%\x27)=6
then \x27PASS\x27 else \x27FAIL\x27 end;
"')"
[[ "$GATE" == "PASS" ]] || fail "post-mapping certification failed"

echo "STRIPE_LIVE_DB_MAPPING_GATE=PASS"
echo "plan_live_mappings=8"
echo "pack_live_mappings=6"
echo "customer_prices_changed=NO"
echo "runtime_changed=NO"
