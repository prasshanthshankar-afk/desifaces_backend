#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
PRICING_CONTAINER="${PRICING_CONTAINER:-df-svc-pricing}"

for c in "$DB_CONTAINER" "$PRICING_CONTAINER"; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "FAIL: missing container $c"; exit 2; }
done

DATABASE_URL="$(docker exec "$PRICING_CONTAINER" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DATABASE_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DATABASE_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

echo "============================================================"
echo " desifaces DEV — CREDIT ECONOMICS AUDIT"
echo " READ ONLY"
echo "============================================================"
echo "db_mutation=NONE"
echo "production=UNTOUCHED"

echo
echo "===== PRICING ENGINE CREDIT VALUE ====="
"${PSQL[@]}" -P pager=off -c "
select currency,money_per_credit,rounding_mode,effective_from,effective_to
from public.pricing_credit_value
where effective_from <= now()
  and (effective_to is null or effective_to > now())
order by currency;
"

echo
echo "===== ACTIVE PLAN EFFECTIVE MONEY PER CREDIT ====="
"${PSQL[@]}" -P pager=off -c "
with plans as (
  select
    p.plan_code,
    p.tier_code,
    p.interval_code,
    upper(p.currency) as currency,
    p.country_code,
    p.price_money,
    t.monthly_grant_credits,
    coalesce(
      nullif(p.metadata_json->>'included_credits_total','')::numeric,
      nullif(p.metadata_json->>'grant_credits','')::numeric,
      case
        when p.interval_code='yearly' then t.monthly_grant_credits::numeric * 12
        else t.monthly_grant_credits::numeric
      end
    ) as included_credits
  from public.pricing_plan_prices p
  join public.pricing_tiers t on t.code=p.tier_code
  where p.is_active=true
    and p.is_public=true
    and p.price_money > 0
)
select
  plan_code,tier_code,interval_code,currency,country_code,
  price_money,included_credits,
  case when included_credits > 0
       then round(price_money/included_credits,6)
       else null end as money_per_credit_effective
from plans
order by currency,tier_code,interval_code,country_code;
"

echo
echo "===== ACTIVE TOP-UP EFFECTIVE MONEY PER CREDIT ====="
"${PSQL[@]}" -P pager=off -c "
select
  code,
  upper(currency) as currency,
  country_code,
  credits,
  price_money,
  case when credits > 0
       then round(price_money/credits::numeric,6)
       else null end as money_per_credit_effective,
  metadata_json
from public.pricing_credit_packs
where coalesce(is_active,true)=true
order by currency,credits,country_code;
"

echo
echo "===== USD CUSTOMER CREDIT VALUE RANGE ====="
"${PSQL[@]}" -P pager=off -c "
with vals as (
  select
    p.price_money /
    nullif(
      coalesce(
        nullif(p.metadata_json->>'included_credits_total','')::numeric,
        nullif(p.metadata_json->>'grant_credits','')::numeric,
        case
          when p.interval_code='yearly' then t.monthly_grant_credits::numeric*12
          else t.monthly_grant_credits::numeric
        end
      ),0
    ) as value
  from public.pricing_plan_prices p
  join public.pricing_tiers t on t.code=p.tier_code
  where p.is_active=true and p.is_public=true
    and upper(p.currency)='USD' and p.price_money>0

  union all

  select price_money/nullif(credits::numeric,0)
  from public.pricing_credit_packs
  where coalesce(is_active,true)=true
    and upper(currency)='USD'
    and price_money>0 and credits>0
)
select
  round(min(value),6) as minimum_realized_usd_per_credit,
  round(max(value),6) as maximum_realized_usd_per_credit,
  round(avg(value),6) as average_realized_usd_per_credit
from vals
where value is not null;
"

echo
echo "============================================================"
echo "CREDIT_ECONOMICS_AUDIT=PASS"
echo "db_mutation=NONE"
echo "production=UNTOUCHED"
echo "============================================================"
