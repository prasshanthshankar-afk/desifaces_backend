#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_REF="793cb2184521b33ab8b5664bf98b148895147b85"
RAW_BASE="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend"
MIGRATION_PATH="migrations/2026_09_13_stripe_live_launch_price_mappings_v2.sql"
MIG="/tmp/2026_09_13_stripe_live_launch_price_mappings_v2.sql"
CERT="/tmp/2026_09_13_stripe_live_launch_price_cert_v2.sql"
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

cat > "$CERT" <<'SQL'
\pset pager off
select plan_code, interval_code, currency, country_code, price_money,
       stripe_price_id, metadata_json->>'stripe_env' as stripe_env
from public.pricing_plan_prices
where tier_code in ('pro','business')
  and is_active=true and is_public=true and self_serve=true
order by tier_code, interval_code, currency;

select code, credits, currency, country_code, price_money,
       metadata_json->>'stripe_price_id' as stripe_price_id,
       metadata_json->>'stripe_env' as stripe_env
from public.pricing_credit_packs
where code in (
  'PACK_USD_1000','PACK_USD_5000','PACK_USD_15000',
  'PACK_INR_1000','PACK_INR_5000','PACK_INR_15000'
)
order by code;

select case when
  (select count(*) from public.pricing_plan_prices
   where (plan_code,interval_code,upper(currency),coalesce(country_code,''),stripe_price_id) in (
    ('pro_monthly_v1','monthly','USD','','price_1UFIZSPA22bn06oYoBHGn0N5'),
    ('pro_monthly_v1','monthly','INR','IN','price_1UFIZTPA22bn06oYHbJwX9wS'),
    ('pro_yearly_v1','yearly','USD','','price_1UFIZUPA22bn06oYkTlNWpBz'),
    ('pro_yearly_v1','yearly','INR','IN','price_1UFIZUPA22bn06oYe0qd8Iw4'),
    ('business_monthly_v1','monthly','USD','','price_1UFIZVPA22bn06oYD4huKUam'),
    ('business_monthly_v1','monthly','INR','IN','price_1UFIZVPA22bn06oYgpgLBzj7'),
    ('business_yearly_v1','yearly','USD','','price_1UFIZWPA22bn06oYkIF8XHnf'),
    ('business_yearly_v1','yearly','INR','IN','price_1UFIZWPA22bn06oYUqdp0UQ8')
   ) and metadata_json->>'stripe_env'='live')=8
  and
  (select count(*) from public.pricing_credit_packs
   where (code,metadata_json->>'stripe_price_id') in (
    ('PACK_USD_1000','price_1UFIZXPA22bn06oYKrt5xey1'),
    ('PACK_USD_5000','price_1UFIZXPA22bn06oYoubhQiFt'),
    ('PACK_USD_15000','price_1UFIZYPA22bn06oYPfKPtxkw'),
    ('PACK_INR_1000','price_1UFIZZPA22bn06oYdhPjac64'),
    ('PACK_INR_5000','price_1UFIZZPA22bn06oYMabHilgS'),
    ('PACK_INR_15000','price_1UFIZaPA22bn06oYOHOiv1fW')
   ) and metadata_json->>'stripe_env'='live')=6
then 'PASS' else 'FAIL' end as stripe_live_mapping_gate;
SQL

echo "============================================================"
echo " desifaces — STRIPE LIVE PROD DB MAPPING V2"
echo " source_ref=$SOURCE_REF"
echo " scope=EXACT_14_STRIPE_LIVE_PRICE_IDS_ONLY"
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
CERT_OUT="$(docker exec -i desifaces-db sh -lc 'psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$CERT")"
printf '%s\n' "$CERT_OUT"
GATE="$(printf '%s\n' "$CERT_OUT" | tail -n 1)"
[[ "$GATE" == "PASS" ]] || fail "post-mapping certification failed"

echo "STRIPE_LIVE_DB_MAPPING_GATE=PASS"
echo "plan_live_mappings=8"
echo "pack_live_mappings=6"
echo "customer_prices_changed=NO"
echo "runtime_changed=NO"
