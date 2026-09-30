#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
PRICING_CONTAINER="${PRICING_CONTAINER:-df-svc-pricing}"

DATABASE_URL="$(docker exec "$PRICING_CONTAINER" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DATABASE_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DATABASE_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

echo "============================================================"
echo " desifaces DEV — NARROW LAUNCH SKU / COGS CERTIFICATION V4"
echo " READ ONLY / FAIL CLOSED"
echo "============================================================"
echo "scope=normal_face_audio_video,multi_person_group_face_audio_video"
echo "workflow_mutation=NONE"
echo "generation_mutation=NONE"
echo "db_mutation=NONE"
echo "production=UNTOUCHED"

for t in pricing_variants pricing_variant_lines pricing_skus pricing_sku_prices pricing_pricebooks pricing_sku_costs pricing_credit_packs pricing_plan_prices pricing_tiers pricing_credit_reservations; do
  x="$("${PSQL[@]}" -Atq -c "select case when to_regclass('public.$t') is null then 0 else 1 end;")"
  [[ "$x" == "1" ]] || { echo "FAIL: missing public.$t"; exit 1; }
done
echo "SCHEMA_CONTRACT=PASS"

RELEVANT="
with relevant(code) as (
  values
    ('FACE_T2I'),
    ('FACE_I2I'),
    ('FACE_IMAGE_STD_BATCH'),
    ('FACE_IMAGE_HD_BATCH'),
    ('FACE_EDIT_PREMIUM_BATCH'),
    ('face.creator.generate.t2i'),
    ('face.creator.generate.i2i'),
    ('FACE_MULTI_PERSON'),
    ('FACE_MULTI_PERSON_I2I'),
    ('AUDIO_TTS'),
    ('AUDIO_MULTI_PERSON'),
    ('FUSION_TALKING_VIDEO'),
    ('FUSION_MULTI_PERSON')
),
leaf as (
  select v.code variant_code,lower(v.category) category,
         vl.sku_code,vl.qty_mode,vl.qty_value,vl.qty_param,
         s.unit,s.provider_hint,s.default_unit_credits,s.status sku_status,s.metadata_json sku_metadata
  from relevant r
  join public.pricing_variants v on v.code=r.code and v.is_active=true
  left join public.pricing_variant_lines vl on vl.variant_code=v.code
  left join public.pricing_skus s on s.code=vl.sku_code
)
"

echo
echo "===== 1. CATALOG ====="
"${PSQL[@]}" -P pager=off -c "
$RELEVANT
select category,variant_code,sku_code,unit,provider_hint,default_unit_credits,qty_mode,qty_value,qty_param,sku_status
from leaf
order by category,variant_code,sku_code;
"
CATALOG_BAD="$("${PSQL[@]}" -Atq -c "
$RELEVANT
select count(*) from leaf
where sku_code is null or sku_status is distinct from 'active'
   or coalesce(default_unit_credits,0)<=0
   or qty_mode not in ('fixed','param','metered');
")"
[[ "$CATALOG_BAD" == "0" ]] && echo "CATALOG_INTEGRITY=PASS" || echo "CATALOG_INTEGRITY=FAIL rows=$CATALOG_BAD"

echo
echo "===== 2. TOP-UP FLOOR VS ACTIVE PUBLIC PLANS ====="
"${PSQL[@]}" -P pager=off -c "
with plan_floor as (
  select currency,min(value) value
  from (
    select upper(p.currency) currency,
           p.price_money/nullif(coalesce(
             nullif(p.metadata_json->>'included_credits_total','')::numeric,
             nullif(p.metadata_json->>'grant_credits','')::numeric,
             case when p.interval_code='yearly' then t.monthly_grant_credits::numeric*12 else t.monthly_grant_credits::numeric end
           ),0) value
    from public.pricing_plan_prices p
    join public.pricing_tiers t on t.code=p.tier_code
    where p.is_active=true and p.is_public=true and p.price_money>0
      and upper(p.currency) in ('USD','INR')
  ) x
  where value>0 group by currency
)
select p.code,p.currency,p.credits,p.price_money,
       round(p.price_money/p.credits::numeric,8) pack_money_per_credit,
       round(f.value,8) minimum_plan_money_per_credit,
       case when p.price_money/p.credits::numeric>=f.value then 'PASS' else 'FAIL' end gate
from public.pricing_credit_packs p
join plan_floor f on f.currency=upper(p.currency)
where p.is_active=true
order by p.currency,p.credits;
"
TOPUP_BAD="$("${PSQL[@]}" -Atq -c "
with plan_floor as (
  select currency,min(value) value
  from (
    select upper(p.currency) currency,
           p.price_money/nullif(coalesce(
             nullif(p.metadata_json->>'included_credits_total','')::numeric,
             nullif(p.metadata_json->>'grant_credits','')::numeric,
             case when p.interval_code='yearly' then t.monthly_grant_credits::numeric*12 else t.monthly_grant_credits::numeric end
           ),0) value
    from public.pricing_plan_prices p
    join public.pricing_tiers t on t.code=p.tier_code
    where p.is_active=true and p.is_public=true and p.price_money>0
      and upper(p.currency) in ('USD','INR')
  ) x where value>0 group by currency
)
select count(*)
from public.pricing_credit_packs p
join plan_floor f on f.currency=upper(p.currency)
where p.is_active=true and p.credits>0
  and p.price_money/p.credits::numeric<f.value;
")"
[[ "$TOPUP_BAD" == "0" ]] && echo "TOPUP_CREDIT_FLOOR=PASS" || echo "TOPUP_CREDIT_FLOOR=FAIL rows=$TOPUP_BAD"

echo
echo "===== 3. COGS ====="
"${PSQL[@]}" -P pager=off -c "
$RELEVANT,
costs as (
  select c.sku_code,
         sum(case lower(c.cost_model)
             when 'variable' then c.variable_cost_money
             when 'amortized' then case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
             when 'blended' then c.variable_cost_money+case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
             else 0 end) unit_cogs_usd,
         string_agg(c.component_code,', ' order by c.component_code) components
  from public.pricing_sku_costs c
  where c.is_active=true and c.effective_from<=now()
    and (c.effective_to is null or c.effective_to>now())
  group by c.sku_code
)
select l.category,l.variant_code,l.sku_code,l.provider_hint,l.unit,
       round(c.unit_cogs_usd,6) unit_cogs_usd,c.components,
       case when coalesce(c.unit_cogs_usd,0)>0 then 'PASS' else 'FAIL' end gate
from leaf l left join costs c on c.sku_code=l.sku_code
order by l.category,l.variant_code,l.sku_code;
"
COGS_BAD="$("${PSQL[@]}" -Atq -c "
$RELEVANT,
costs as (
  select c.sku_code,
         sum(case lower(c.cost_model)
             when 'variable' then c.variable_cost_money
             when 'amortized' then case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
             when 'blended' then c.variable_cost_money+case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
             else 0 end) unit_cogs_usd
  from public.pricing_sku_costs c
  where c.is_active=true and c.effective_from<=now()
    and (c.effective_to is null or c.effective_to>now())
  group by c.sku_code
)
select count(*) from leaf l
left join costs c on c.sku_code=l.sku_code
where coalesce(c.unit_cogs_usd,0)<=0;
")"
[[ "$COGS_BAD" == "0" ]] && echo "COGS_COMPLETENESS=PASS" || echo "COGS_COMPLETENESS=FAIL rows=$COGS_BAD"

echo
echo "===== 4. +20% IMAGE / VIDEO PREMIUM ====="
"${PSQL[@]}" -P pager=off -c "
select code,default_unit_credits,
       metadata_json->>'source_sku' source_sku,
       metadata_json->>'premium_rate_multiplier' premium_rate_multiplier
from public.pricing_skus
where code in ('FACE_MULTI_PERSON','FACE_MULTI_PERSON_I2I','FUSION_MULTI_PERSON')
order by code;
"
PREMIUM_BAD="$("${PSQL[@]}" -Atq -c "
with pairs(multi_sku,base_sku) as (
  values
    ('FACE_MULTI_PERSON','IMG_STD_RUN'),
    ('FACE_MULTI_PERSON_I2I','FACE_EDIT_PREMIUM_RUN'),
    ('FUSION_MULTI_PERSON','FUSION_TALK_MIN')
),
effective as (
  select pb.id,p.multi_sku,
         ceil(coalesce(bs.unit_credits_override,b.default_unit_credits)::numeric*coalesce(pb.multiplier,1))::bigint base_credits,
         ceil(coalesce(ms.unit_credits_override,m.default_unit_credits)::numeric*coalesce(pb.multiplier,1))::bigint multi_credits
  from public.pricing_pricebooks pb
  cross join pairs p
  join public.pricing_skus b on b.code=p.base_sku
  join public.pricing_skus m on m.code=p.multi_sku
  left join public.pricing_sku_prices bs on bs.pricebook_id=pb.id and bs.sku_code=p.base_sku
  left join public.pricing_sku_prices ms on ms.pricebook_id=pb.id and ms.sku_code=p.multi_sku
  where pb.is_active=true and pb.effective_from<=now()
    and (pb.effective_to is null or pb.effective_to>now())
)
select count(*) from effective
where multi_credits<ceil(base_credits*1.20)::bigint;
")"
[[ "$PREMIUM_BAD" == "0" ]] && echo "MULTI_PERSON_IMAGE_VIDEO_PREMIUM_20PCT=PASS" || echo "MULTI_PERSON_IMAGE_VIDEO_PREMIUM_20PCT=FAIL rows=$PREMIUM_BAD"

echo
echo "===== 5. WORST-CASE REALIZED USD MARGIN ====="
"${PSQL[@]}" -P pager=off -c "
$RELEVANT,
credit_values as (
  select p.price_money/nullif(coalesce(
           nullif(p.metadata_json->>'included_credits_total','')::numeric,
           nullif(p.metadata_json->>'grant_credits','')::numeric,
           case when p.interval_code='yearly' then t.monthly_grant_credits::numeric*12 else t.monthly_grant_credits::numeric end
         ),0) value
  from public.pricing_plan_prices p
  join public.pricing_tiers t on t.code=p.tier_code
  where p.is_active=true and p.is_public=true and upper(p.currency)='USD' and p.price_money>0
  union all
  select price_money/nullif(credits::numeric,0)
  from public.pricing_credit_packs
  where is_active=true and upper(currency)='USD' and price_money>0 and credits>0
),
min_credit as (select min(value) value from credit_values),
pb as (
  select * from public.pricing_pricebooks
  where is_active=true and upper(currency)='USD' and channel='web'
    and effective_from<=now() and (effective_to is null or effective_to>now())
  order by case when coalesce(country_code,'')='' then 0 else 1 end,
           case when tier_code is null then 0 else 1 end,
           effective_from desc limit 1
),
costs as (
  select c.sku_code,
         sum(case lower(c.cost_model)
             when 'variable' then c.variable_cost_money
             when 'amortized' then case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
             when 'blended' then c.variable_cost_money+case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
             else 0 end) unit_cogs
  from public.pricing_sku_costs c
  where c.is_active=true and c.effective_from<=now()
    and (c.effective_to is null or c.effective_to>now())
  group by c.sku_code
),
agg as (
  select l.category,l.variant_code,
         sum(ceil((case when l.qty_mode='fixed' then coalesce(l.qty_value,0) else 1 end)
             *coalesce(sp.unit_credits_override,l.default_unit_credits)::numeric*coalesce(pb.multiplier,1))) credits,
         sum((case when l.qty_mode='fixed' then coalesce(l.qty_value,0) else 1 end)*c.unit_cogs) cogs
  from leaf l cross join pb
  left join public.pricing_sku_prices sp on sp.pricebook_id=pb.id and sp.sku_code=l.sku_code
  left join costs c on c.sku_code=l.sku_code
  group by l.category,l.variant_code
)
select a.category,a.variant_code,a.credits,
       round(m.value,6) min_realized_usd_per_credit,
       round(a.credits*m.value,6) min_realized_revenue_usd,
       round(a.cogs,6) cogs_usd,
       round(a.credits*m.value-a.cogs,6) gross_margin_usd,
       case when a.cogs is null then 'FAIL:MISSING_COGS'
            when a.credits*m.value-a.cogs<0 then 'FAIL:NEGATIVE_MARGIN'
            else 'PASS' end gate
from agg a cross join min_credit m
order by a.category,a.variant_code;
"
MARGIN_BAD="$("${PSQL[@]}" -Atq -c "
$RELEVANT,
credit_values as (
  select p.price_money/nullif(coalesce(
           nullif(p.metadata_json->>'included_credits_total','')::numeric,
           nullif(p.metadata_json->>'grant_credits','')::numeric,
           case when p.interval_code='yearly' then t.monthly_grant_credits::numeric*12 else t.monthly_grant_credits::numeric end
         ),0) value
  from public.pricing_plan_prices p join public.pricing_tiers t on t.code=p.tier_code
  where p.is_active=true and p.is_public=true and upper(p.currency)='USD' and p.price_money>0
  union all
  select price_money/nullif(credits::numeric,0)
  from public.pricing_credit_packs
  where is_active=true and upper(currency)='USD' and price_money>0 and credits>0
),
min_credit as (select min(value) value from credit_values),
pb as (
  select * from public.pricing_pricebooks
  where is_active=true and upper(currency)='USD' and channel='web'
    and effective_from<=now() and (effective_to is null or effective_to>now())
  order by case when coalesce(country_code,'')='' then 0 else 1 end,
           case when tier_code is null then 0 else 1 end,effective_from desc limit 1
),
costs as (
  select c.sku_code,
         sum(case lower(c.cost_model)
             when 'variable' then c.variable_cost_money
             when 'amortized' then case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
             when 'blended' then c.variable_cost_money+case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
             else 0 end) unit_cogs
  from public.pricing_sku_costs c
  where c.is_active=true and c.effective_from<=now()
    and (c.effective_to is null or c.effective_to>now())
  group by c.sku_code
),
agg as (
  select l.variant_code,
         sum(ceil((case when l.qty_mode='fixed' then coalesce(l.qty_value,0) else 1 end)
             *coalesce(sp.unit_credits_override,l.default_unit_credits)::numeric*coalesce(pb.multiplier,1))) credits,
         sum((case when l.qty_mode='fixed' then coalesce(l.qty_value,0) else 1 end)*c.unit_cogs) cogs
  from leaf l cross join pb
  left join public.pricing_sku_prices sp on sp.pricebook_id=pb.id and sp.sku_code=l.sku_code
  left join costs c on c.sku_code=l.sku_code
  group by l.variant_code
)
select count(*) from agg cross join min_credit
where cogs is null or credits*value-cogs<0;
")"
[[ "$MARGIN_BAD" == "0" ]] && echo "WORST_CASE_USD_MARGIN=PASS" || echo "WORST_CASE_USD_MARGIN=FAIL variants=$MARGIN_BAD"

echo
echo "===== 6. BILLING PARITY ====="
RESERVE_BAD="$("${PSQL[@]}" -Atq -c "
select count(*) from public.pricing_credit_reservations r
where r.created_at>=now()-interval '7 days'
  and r.status in ('reserved','committed')
  and lower(coalesce(r.quote_json->>'settlement_mode','prepaid'))<>'postpaid'
  and coalesce(r.quote_json->>'billing_mode_snapshot',r.quote_json->>'billing_mode','bill')='bill'
  and nullif(r.quote_json->>'total_credits','')::numeric is not null
  and r.reserved_credits<>(r.quote_json->>'total_credits')::numeric;
")"
FINAL_BAD="$("${PSQL[@]}" -Atq -c "
select count(*) from public.pricing_credit_reservations r
where r.created_at>=now()-interval '7 days'
  and r.status='committed'
  and lower(coalesce(r.quote_json->>'settlement_mode','prepaid'))<>'postpaid'
  and coalesce(r.quote_json->>'billing_mode_snapshot',r.quote_json->>'billing_mode','bill')='bill'
  and nullif(r.quote_json->>'total_credits','')::numeric is not null
  and nullif(r.quote_json->>'final_charged_credits','')::numeric is distinct from (r.quote_json->>'total_credits')::numeric;
")"
[[ "$RESERVE_BAD" == "0" ]] && echo "QUOTE_RESERVE_CREDIT_PARITY=PASS" || echo "QUOTE_RESERVE_CREDIT_PARITY=FAIL rows=$RESERVE_BAD"
[[ "$FINAL_BAD" == "0" ]] && echo "QUOTE_COMMIT_CREDIT_PARITY=PASS" || echo "QUOTE_COMMIT_CREDIT_PARITY=FAIL rows=$FINAL_BAD"

echo
echo "===== 7. STALE COST MARKERS ====="
STALE_BAD="$("${PSQL[@]}" -Atq -c "
select count(*) from public.pricing_sku_costs c
where c.sku_code in ('IMG_STD_RUN','IMG_HD_RUN','FUSION_TALK_MIN')
  and c.is_active=true and c.effective_from<=now()
  and (c.effective_to is null or c.effective_to>now())
  and (
    c.component_code ~* 'fal|heygen|placeholder'
    or coalesce(c.metadata_json::text,'') ~* 'fal_subscription|heygen_subscription|placeholder|update_required'
  );
")"
[[ "$STALE_BAD" == "0" ]] && echo "STALE_PROVIDER_COGS=PASS" || echo "STALE_PROVIDER_COGS=FAIL rows=$STALE_BAD"

echo
echo "============================================================"
echo " FINAL"
echo "============================================================"
echo "CATALOG_BAD=$CATALOG_BAD"
echo "TOPUP_BAD=$TOPUP_BAD"
echo "COGS_BAD=$COGS_BAD"
echo "PREMIUM_BAD=$PREMIUM_BAD"
echo "MARGIN_BAD=$MARGIN_BAD"
echo "RESERVE_BAD=$RESERVE_BAD"
echo "FINAL_BAD=$FINAL_BAD"
echo "STALE_BAD=$STALE_BAD"
echo "workflow_mutation=NONE"
echo "generation_mutation=NONE"
echo "db_mutation=NONE"
echo "production=UNTOUCHED"

if [[ "$CATALOG_BAD" != "0" || "$TOPUP_BAD" != "0" || "$COGS_BAD" != "0" || "$PREMIUM_BAD" != "0" || "$MARGIN_BAD" != "0" || "$RESERVE_BAD" != "0" || "$FINAL_BAD" != "0" || "$STALE_BAD" != "0" ]]; then
  echo "NARROW_LAUNCH_SKU_COGS_V4=FAIL"
  exit 1
fi

echo "NARROW_LAUNCH_SKU_COGS_V4=PASS"
