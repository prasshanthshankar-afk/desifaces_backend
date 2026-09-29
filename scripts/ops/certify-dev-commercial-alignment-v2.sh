#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
PRICING_CONTAINER="${PRICING_CONTAINER:-df-svc-pricing}"
FUSION_EXTENSION_CONTAINER="${FUSION_EXTENSION_CONTAINER:-df-svc-fusion-extension}"
REPO_ROOT="${REPO_ROOT:-$HOME/workspace/desifaces-v3}"

for c in "$DB_CONTAINER" "$PRICING_CONTAINER"; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "FAIL: missing container $c"; exit 2; }
done

DATABASE_URL="$(docker exec "$PRICING_CONTAINER" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DATABASE_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DATABASE_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

echo "============================================================"
echo " desifaces DEV — COMMERCIAL ALIGNMENT V2"
echo " READ ONLY / FAIL CLOSED"
echo "============================================================"
echo "required_multi_person_premium_pct=20"
echo "premium_rule=ceil(base_credits*1.20)"
echo "db_mutation=NONE"
echo "provider_generation=NONE"
echo "production=UNTOUCHED"

echo
echo "===== LIVE RUNTIME GROUP-VIDEO PRICING OWNER ====="
GROUP_VIDEO_RUNTIME_VARIANT=""
GROUP_VIDEO_RUNTIME_LEAF=""
GROUP_VIDEO_RUNTIME_PROVIDER=""
if docker inspect "$FUSION_EXTENSION_CONTAINER" >/dev/null 2>&1; then
  GROUP_VIDEO_RUNTIME_VARIANT="$(
    docker exec "$FUSION_EXTENSION_CONTAINER" sh -lc       "grep -E '^_VARIANT_CODE[[:space:]]*=' /app/app/api/routes/v3_scene_pricing.py 2>/dev/null | head -n1 | sed -E 's/.*=[[:space:]]*[\"'\'' ]*([^\"'\'' ]+).*/\1/'"       2>/dev/null || true
  )"
  GROUP_VIDEO_RUNTIME_LEAF="$(
    docker exec "$FUSION_EXTENSION_CONTAINER" sh -lc       "grep -E '^_LEAF_SKU_CODE[[:space:]]*=' /app/app/api/routes/v3_scene_pricing.py 2>/dev/null | head -n1 | sed -E 's/.*=[[:space:]]*[\"'\'' ]*([^\"'\'' ]+).*/\1/'"       2>/dev/null || true
  )"
  GROUP_VIDEO_RUNTIME_PROVIDER="$(
    docker exec "$FUSION_EXTENSION_CONTAINER" sh -lc       "grep -E '^_PROVIDER[[:space:]]*=' /app/app/api/routes/v3_scene_pricing.py 2>/dev/null | head -n1 | sed -E 's/.*=[[:space:]]*[\"'\'' ]*([^\"'\'' ]+).*/\1/'"       2>/dev/null || true
  )"
fi
echo "group_video_runtime_variant=${GROUP_VIDEO_RUNTIME_VARIANT:-UNKNOWN}"
echo "group_video_runtime_leaf=${GROUP_VIDEO_RUNTIME_LEAF:-UNKNOWN}"
echo "group_video_runtime_provider=${GROUP_VIDEO_RUNTIME_PROVIDER:-UNKNOWN}"

echo
echo "===== SOURCE PREMIUM POLICY MARKERS ====="
if [[ -f "$REPO_ROOT/migrations/2026_08_30_multi_person_premium_pricing.sql" ]]; then
  grep -nE 'premium unit rate|premium_rate_multiplier|FACE_MULTI_PERSON|FUSION_MULTI_PERSON'     "$REPO_ROOT/migrations/2026_08_30_multi_person_premium_pricing.sql" | head -n 20 || true
else
  echo "source_migration=UNAVAILABLE"
fi

echo
echo "===== ACTIVE FACE VARIANT / LEAF CONTRACT ====="
"${PSQL[@]}" -P pager=off -c "
select
  v.code as variant_code,
  v.name,
  v.is_active,
  vl.sku_code,
  vl.qty_mode,
  vl.qty_value,
  vl.qty_param,
  s.unit,
  s.provider_hint,
  s.default_unit_credits,
  s.metadata_json as sku_metadata,
  v.metadata_json as variant_metadata
from public.pricing_variants v
left join public.pricing_variant_lines vl on vl.variant_code=v.code
left join public.pricing_skus s on s.code=vl.sku_code
where v.code in ('FACE_T2I','FACE_I2I','FACE_MULTI_PERSON')
order by v.code,vl.sku_code;
"

echo
echo "===== FACE EXACT +20% CREDIT GATE BY ACTIVE WEB PRICEBOOK ====="
"${PSQL[@]}" -P pager=off -c "
with pbs as (
  select id,name,currency,channel,country_code,tier_code,multiplier
  from public.pricing_pricebooks
  where is_active=true
    and channel='web'
    and currency in ('USD','INR')
    and effective_from <= now()
    and (effective_to is null or effective_to > now())
),
rates as (
  select
    pb.id as pricebook_id,
    pb.name as pricebook_name,
    pb.currency,
    pb.country_code,
    pb.tier_code,
    vl.variant_code,
    sum(
      ceil(
        (case when vl.qty_mode='fixed' then coalesce(vl.qty_value,0) else 1 end)
        * coalesce(sp.unit_credits_override,s.default_unit_credits)::numeric
        * coalesce(pb.multiplier,1)::numeric
      )
    )::bigint as credits_for_one_request
  from pbs pb
  join public.pricing_variant_lines vl
    on vl.variant_code in ('FACE_T2I','FACE_I2I','FACE_MULTI_PERSON')
  join public.pricing_skus s on s.code=vl.sku_code and s.status='active'
  left join public.pricing_sku_prices sp
    on sp.pricebook_id=pb.id and sp.sku_code=s.code
  group by pb.id,pb.name,pb.currency,pb.country_code,pb.tier_code,vl.variant_code
),
base as (
  select * from rates where variant_code in ('FACE_T2I','FACE_I2I')
),
mp as (
  select * from rates where variant_code='FACE_MULTI_PERSON'
)
select
  b.pricebook_name,
  b.currency,
  coalesce(b.country_code,'GLOBAL') as country,
  coalesce(b.tier_code,'ANY') as tier,
  b.variant_code as base_variant,
  b.credits_for_one_request as base_credits,
  m.credits_for_one_request as multi_credits,
  ceil(b.credits_for_one_request::numeric*1.20)::bigint as expected_multi_credits,
  case
    when m.credits_for_one_request is null then 'FAIL:MISSING_MULTI_PRICE'
    when m.credits_for_one_request = ceil(b.credits_for_one_request::numeric*1.20)::bigint then 'PASS'
    else 'FAIL'
  end as exact_20pct_rule
from base b
left join mp m on m.pricebook_id=b.pricebook_id
order by b.currency,b.pricebook_name,b.variant_code;
"

echo
echo "===== ACTIVE GROUP-VIDEO / SINGLE-VIDEO CATALOG ====="
"${PSQL[@]}" -P pager=off -c "
select
  v.code as variant_code,
  v.name,
  v.is_active,
  vl.sku_code,
  vl.qty_mode,
  vl.qty_value,
  vl.qty_param,
  s.unit,
  s.provider_hint,
  s.default_unit_credits,
  s.metadata_json as sku_metadata,
  v.metadata_json as variant_metadata
from public.pricing_variants v
left join public.pricing_variant_lines vl on vl.variant_code=v.code
left join public.pricing_skus s on s.code=vl.sku_code
where v.code in (
  'FUSION_TALKING_VIDEO',
  'FUSION_MULTI_PERSON',
  'TALKING_VIDEO',
  'TALKING_VIDEO_PREMIUM_SECOND'
)
order by v.code,vl.sku_code;
"

echo
echo "===== VIDEO ONE-UNIT CREDIT COMPARISON ====="
"${PSQL[@]}" -P pager=off -c "
with pbs as (
  select id,name,currency,channel,country_code,tier_code,multiplier
  from public.pricing_pricebooks
  where is_active=true
    and channel='web'
    and currency in ('USD','INR')
    and effective_from <= now()
    and (effective_to is null or effective_to > now())
),
rates as (
  select
    pb.id as pricebook_id,
    pb.name as pricebook_name,
    pb.currency,
    pb.country_code,
    pb.tier_code,
    vl.variant_code,
    sum(
      ceil(
        (case when vl.qty_mode='fixed' then coalesce(vl.qty_value,0) else 1 end)
        * coalesce(sp.unit_credits_override,s.default_unit_credits)::numeric
        * coalesce(pb.multiplier,1)::numeric
      )
    )::bigint as credits_for_one_unit
  from pbs pb
  join public.pricing_variant_lines vl
    on vl.variant_code in ('FUSION_TALKING_VIDEO','FUSION_MULTI_PERSON','TALKING_VIDEO','TALKING_VIDEO_PREMIUM_SECOND')
  join public.pricing_skus s on s.code=vl.sku_code and s.status='active'
  left join public.pricing_sku_prices sp
    on sp.pricebook_id=pb.id and sp.sku_code=s.code
  group by pb.id,pb.name,pb.currency,pb.country_code,pb.tier_code,vl.variant_code
)
select *
from rates
order by currency,pricebook_name,variant_code;
"

echo
echo "===== COGS COMPLETENESS FOR ACTUAL BILLABLE LEAF SKUS ====="
"${PSQL[@]}" -P pager=off -c "
with required as (
  select distinct
    vl.variant_code,
    vl.sku_code,
    s.provider_hint
  from public.pricing_variant_lines vl
  join public.pricing_skus s on s.code=vl.sku_code
  where vl.variant_code in (
    'FACE_T2I',
    'FACE_I2I',
    'FACE_MULTI_PERSON',
    'FUSION_TALKING_VIDEO',
    'FUSION_MULTI_PERSON',
    'TALKING_VIDEO_PREMIUM_SECOND'
  )
),
costs as (
  select
    c.sku_code,
    count(*)::int as active_component_count,
    string_agg(c.component_code, ', ' order by c.component_code) as components,
    sum(
      case lower(c.cost_model)
        when 'variable' then c.variable_cost_money
        when 'amortized' then
          case when c.assumed_monthly_units>0
               then c.fixed_monthly_cost_money/c.assumed_monthly_units
               else 0 end
        else
          c.variable_cost_money
          + case when c.assumed_monthly_units>0
                 then c.fixed_monthly_cost_money/c.assumed_monthly_units
                 else 0 end
      end
    ) as unit_cogs_usd,
    bool_or(
      c.component_code ~* 'fal|heygen'
      or coalesce(c.metadata_json::text,'') ~* 'fal|heygen'
    ) as has_legacy_fal_or_heygen_marker
  from public.pricing_sku_costs c
  where c.is_active=true
    and c.effective_from <= now()
    and (c.effective_to is null or c.effective_to > now())
  group by c.sku_code
)
select
  r.variant_code,
  r.sku_code,
  r.provider_hint,
  coalesce(c.active_component_count,0) as active_cost_components,
  c.components,
  round(c.unit_cogs_usd,6) as unit_cogs_usd,
  coalesce(c.has_legacy_fal_or_heygen_marker,false) as legacy_cost_marker,
  case when coalesce(c.active_component_count,0)>0 then 'PASS' else 'FAIL:MISSING_COGS' end as cogs_presence
from required r
left join costs c on c.sku_code=r.sku_code
order by r.variant_code,r.sku_code;
"

echo
echo "===== PROVIDER ALIGNMENT GATE ====="
"${PSQL[@]}" -P pager=off -c "
with required as (
  select distinct vl.variant_code,vl.sku_code,lower(coalesce(s.provider_hint,'')) as provider_hint
  from public.pricing_variant_lines vl
  join public.pricing_skus s on s.code=vl.sku_code
  where vl.variant_code in ('FACE_T2I','FACE_I2I','FACE_MULTI_PERSON','FUSION_TALKING_VIDEO')
)
select
  variant_code,
  sku_code,
  provider_hint,
  case
    when variant_code in ('FACE_T2I','FACE_I2I','FACE_MULTI_PERSON')
      then case when provider_hint ~ 'openai|gpt' then 'PASS' else 'FAIL:FACE_PROVIDER_STALE_OR_UNKNOWN' end
    when variant_code='FUSION_TALKING_VIDEO'
      then case when provider_hint ~ 'veed|provider-neutral|neutral' then 'PASS' else 'FAIL:GROUP_VIDEO_PROVIDER_STALE_OR_UNKNOWN' end
    else 'INFO'
  end as provider_alignment
from required
order by variant_code,sku_code;
"

echo
echo "===== LOWEST REALIZED USD CREDIT VALUE ====="
"${PSQL[@]}" -P pager=off -c "
with vals as (
  select
    'plan:'||p.plan_code as source,
    p.price_money /
    nullif(
      coalesce(
        nullif(p.metadata_json->>'included_credits_total','')::numeric,
        nullif(p.metadata_json->>'grant_credits','')::numeric,
        case when p.interval_code='yearly'
             then t.monthly_grant_credits::numeric*12
             else t.monthly_grant_credits::numeric end
      ),0
    ) as usd_per_credit
  from public.pricing_plan_prices p
  join public.pricing_tiers t on t.code=p.tier_code
  where p.is_active=true and p.is_public=true
    and upper(p.currency)='USD' and p.price_money>0

  union all

  select 'pack:'||code, price_money/nullif(credits::numeric,0)
  from public.pricing_credit_packs
  where coalesce(is_active,true)=true
    and upper(currency)='USD'
    and price_money>0 and credits>0
)
select source,round(usd_per_credit,6) as usd_per_credit
from vals
where usd_per_credit=(select min(usd_per_credit) from vals)
order by source;
"

echo
echo "===== WORST-CASE REALIZED MARGIN PER ONE NATIVE UNIT (USD) ====="
"${PSQL[@]}" -P pager=off -c "
with vals as (
  select
    p.price_money /
    nullif(
      coalesce(
        nullif(p.metadata_json->>'included_credits_total','')::numeric,
        nullif(p.metadata_json->>'grant_credits','')::numeric,
        case when p.interval_code='yearly'
             then t.monthly_grant_credits::numeric*12
             else t.monthly_grant_credits::numeric end
      ),0
    ) as usd_per_credit
  from public.pricing_plan_prices p
  join public.pricing_tiers t on t.code=p.tier_code
  where p.is_active=true and p.is_public=true
    and upper(p.currency)='USD' and p.price_money>0
  union all
  select price_money/nullif(credits::numeric,0)
  from public.pricing_credit_packs
  where coalesce(is_active,true)=true and upper(currency)='USD'
    and price_money>0 and credits>0
),
minv as (select min(usd_per_credit) as usd_per_credit from vals),
pb as (
  select *
  from public.pricing_pricebooks
  where is_active=true and channel='web' and upper(currency)='USD'
    and effective_from <= now()
    and (effective_to is null or effective_to > now())
  order by
    case when coalesce(country_code,'')='' then 0 else 1 end,
    case when tier_code is null then 0 else 1 end,
    effective_from desc
  limit 1
),
variant_lines as (
  select
    vl.variant_code,
    vl.sku_code,
    (case when vl.qty_mode='fixed' then coalesce(vl.qty_value,0) else 1 end)::numeric as qty,
    ceil(
      (case when vl.qty_mode='fixed' then coalesce(vl.qty_value,0) else 1 end)
      * coalesce(sp.unit_credits_override,s.default_unit_credits)::numeric
      * coalesce(pb.multiplier,1)::numeric
    )::numeric as line_credits
  from pb
  join public.pricing_variant_lines vl
    on vl.variant_code in ('FACE_T2I','FACE_I2I','FACE_MULTI_PERSON','FUSION_TALKING_VIDEO','FUSION_MULTI_PERSON')
  join public.pricing_skus s on s.code=vl.sku_code
  left join public.pricing_sku_prices sp on sp.pricebook_id=pb.id and sp.sku_code=s.code
),
costs as (
  select
    c.sku_code,
    sum(
      case lower(c.cost_model)
        when 'variable' then c.variable_cost_money
        when 'amortized' then case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
        else c.variable_cost_money + case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
      end
    ) as unit_cogs_usd
  from public.pricing_sku_costs c
  where c.is_active=true
    and c.effective_from <= now()
    and (c.effective_to is null or c.effective_to > now())
  group by c.sku_code
),
agg as (
  select
    vl.variant_code,
    sum(vl.line_credits)::numeric as credits,
    sum(vl.qty*c.unit_cogs_usd)::numeric as cogs_usd,
    bool_and(c.unit_cogs_usd is not null) as cogs_complete
  from variant_lines vl
  left join costs c on c.sku_code=vl.sku_code
  group by vl.variant_code
)
select
  a.variant_code,
  a.credits,
  round(m.usd_per_credit,6) as minimum_realized_usd_per_credit,
  round(a.credits*m.usd_per_credit,6) as minimum_realized_revenue_usd,
  case when a.cogs_complete then round(a.cogs_usd,6) else null end as cogs_usd,
  case when a.cogs_complete then round(a.credits*m.usd_per_credit-a.cogs_usd,6) else null end as gross_margin_usd,
  case
    when not a.cogs_complete then 'FAIL:MISSING_COGS'
    when a.credits*m.usd_per_credit-a.cogs_usd < 0 then 'FAIL:NEGATIVE_MARGIN'
    else 'PASS'
  end as worst_case_margin_gate
from agg a cross join minv m
order by a.variant_code;
"

echo
echo "===== RECENT FACE / GROUP-VIDEO PRICING EVIDENCE ====="
"${PSQL[@]}" -P pager=off -c "
select
  created_at,
  status,
  quote_json->>'service_name' as service_name,
  quote_json->>'service_action' as service_action,
  quote_json->>'variant_code' as variant_code,
  quote_json->>'sku_code' as sku_code,
  quote_json->'params'->>'mode' as mode,
  quote_json->'params'->>'participant_count' as participant_count,
  quote_json->'params'->>'workflow_id' as workflow_id,
  quote_json->>'reserved_credits' as reserved_credits,
  quote_json->>'total_credits' as total_credits
from public.pricing_credit_reservations
where
  quote_json->>'service_action' in ('face.creator.generate.t2i','face.creator.generate.i2i','fusion.video.generate')
  or quote_json::text ~* 'shared_scene|fusion_scene_parent|FACE_MULTI_PERSON|FUSION_MULTI_PERSON'
order by created_at desc
limit 30;
"

FACE_MISMATCH="$(
"${PSQL[@]}" -Atq -c "
with pbs as (
  select id,multiplier
  from public.pricing_pricebooks
  where is_active=true and channel='web' and currency in ('USD','INR')
    and effective_from <= now() and (effective_to is null or effective_to > now())
),
rates as (
  select pb.id,vl.variant_code,
    sum(ceil((case when vl.qty_mode='fixed' then coalesce(vl.qty_value,0) else 1 end)
      * coalesce(sp.unit_credits_override,s.default_unit_credits)::numeric
      * coalesce(pb.multiplier,1)::numeric))::bigint as credits
  from pbs pb
  join public.pricing_variant_lines vl on vl.variant_code in ('FACE_T2I','FACE_I2I','FACE_MULTI_PERSON')
  join public.pricing_skus s on s.code=vl.sku_code and s.status='active'
  left join public.pricing_sku_prices sp on sp.pricebook_id=pb.id and sp.sku_code=s.code
  group by pb.id,vl.variant_code
),
b as (select * from rates where variant_code in ('FACE_T2I','FACE_I2I')),
m as (select * from rates where variant_code='FACE_MULTI_PERSON')
select count(*)
from b left join m using(id)
where m.credits is null or m.credits <> ceil(b.credits::numeric*1.20)::bigint;
"
)"

MISSING_COGS="$(
"${PSQL[@]}" -Atq -c "
with required as (
  select distinct vl.sku_code
  from public.pricing_variant_lines vl
  where vl.variant_code in ('FACE_T2I','FACE_I2I','FACE_MULTI_PERSON','FUSION_TALKING_VIDEO')
)
select count(*)
from required r
where not exists (
  select 1 from public.pricing_sku_costs c
  where c.sku_code=r.sku_code and c.is_active=true
    and c.effective_from <= now()
    and (c.effective_to is null or c.effective_to > now())
);
"
)"

STALE_PROVIDER="$(
"${PSQL[@]}" -Atq -c "
with required as (
  select distinct vl.variant_code,lower(coalesce(s.provider_hint,'')) provider_hint
  from public.pricing_variant_lines vl
  join public.pricing_skus s on s.code=vl.sku_code
  where vl.variant_code in ('FACE_T2I','FACE_I2I','FACE_MULTI_PERSON','FUSION_TALKING_VIDEO')
)
select count(*)
from required
where
  (variant_code in ('FACE_T2I','FACE_I2I','FACE_MULTI_PERSON') and provider_hint !~ 'openai|gpt')
  or
  (variant_code='FUSION_TALKING_VIDEO' and provider_hint !~ 'veed|provider-neutral|neutral');
"
)"

GROUP_VIDEO_GATE="PASS"
if [[ -z "$GROUP_VIDEO_RUNTIME_VARIANT" || "$GROUP_VIDEO_RUNTIME_VARIANT" == "FUSION_TALKING_VIDEO" ]]; then
  # Current Next3 shared-scene runtime uses the ordinary single-person parent variant.
  # That does not establish an additional 20% multi-person premium.
  GROUP_VIDEO_GATE="FAIL"
fi

echo
echo "============================================================"
echo " FINAL COMMERCIAL GATES"
echo "============================================================"
if [[ "${FACE_MISMATCH:-999}" == "0" ]]; then
  echo "FACE_T2I_I2I_EXACT_20PCT=PASS"
else
  echo "FACE_T2I_I2I_EXACT_20PCT=FAIL mismatches=${FACE_MISMATCH:-unknown}"
fi

if [[ "$GROUP_VIDEO_GATE" == "PASS" ]]; then
  echo "GROUP_VIDEO_DISTINCT_20PCT_RUNTIME=PASS"
else
  echo "GROUP_VIDEO_DISTINCT_20PCT_RUNTIME=FAIL runtime_variant=${GROUP_VIDEO_RUNTIME_VARIANT:-UNKNOWN}"
fi

if [[ "${MISSING_COGS:-999}" == "0" ]]; then
  echo "ACTIVE_BILLABLE_SKU_COGS=PASS"
else
  echo "ACTIVE_BILLABLE_SKU_COGS=FAIL missing=${MISSING_COGS:-unknown}"
fi

if [[ "${STALE_PROVIDER:-999}" == "0" ]]; then
  echo "ACTIVE_PROVIDER_ALIGNMENT=PASS"
else
  echo "ACTIVE_PROVIDER_ALIGNMENT=FAIL stale_or_unknown=${STALE_PROVIDER:-unknown}"
fi

echo "db_mutation=NONE"
echo "provider_generation=NONE"
echo "production=UNTOUCHED"

if [[ "${FACE_MISMATCH:-999}" != "0" || "$GROUP_VIDEO_GATE" != "PASS" || "${MISSING_COGS:-999}" != "0" || "${STALE_PROVIDER:-999}" != "0" ]]; then
  echo "COMMERCIAL_ALIGNMENT_V2=FAIL"
  echo "============================================================"
  exit 1
fi

echo "COMMERCIAL_ALIGNMENT_V2=PASS"
echo "============================================================"
