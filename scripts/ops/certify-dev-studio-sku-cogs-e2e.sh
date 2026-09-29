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
echo " desifaces DEV — ALL STUDIO SKU / COGS E2E CERTIFICATION"
echo " READ ONLY / FAIL CLOSED"
echo "============================================================"
echo "scope=face,audio,fusion"
echo "workflow_mutation=NONE"
echo "generation_mutation=NONE"
echo "db_mutation=NONE"
echo "production=UNTOUCHED"

for required in pricing_variants pricing_variant_lines pricing_skus pricing_sku_prices pricing_pricebooks pricing_credit_value pricing_sku_costs pricing_credit_reservations; do
  EXISTS="$("${PSQL[@]}" -Atq -c "select case when to_regclass('public.${required}') is not null then 1 else 0 end;")"
  [[ "$EXISTS" == "1" ]] || { echo "FAIL: required table public.$required missing"; exit 1; }
done
echo "SCHEMA_CONTRACT=PASS"

RELEVANT_CTE="
with relevant_variants as (
  select v.code,v.name,lower(v.category) category,v.metadata_json
  from public.pricing_variants v
  where v.is_active=true
    and lower(v.category) in ('face','audio','fusion','fusion_extension')
    and upper(v.code) not like '%INTERNAL%'
    and coalesce((v.metadata_json->>'internal')::boolean,false)=false
    and coalesce((v.metadata_json->>'suppress_pricing')::boolean,false)=false
),
leaf as (
  select
    v.code variant_code,
    v.name variant_name,
    v.category,
    vl.sku_code,
    vl.qty_mode,
    vl.qty_value,
    vl.qty_param,
    s.name sku_name,
    s.unit,
    s.provider_hint,
    s.default_unit_credits,
    s.status sku_status,
    s.metadata_json sku_metadata
  from relevant_variants v
  left join public.pricing_variant_lines vl on vl.variant_code=v.code
  left join public.pricing_skus s on s.code=vl.sku_code
)
"

echo
echo "===== 1. ACTIVE CUSTOMER-BILLABLE STUDIO CATALOG ====="
"${PSQL[@]}" -P pager=off -c "
$RELEVANT_CTE
select
  category,variant_code,variant_name,sku_code,sku_name,unit,provider_hint,
  default_unit_credits,qty_mode,qty_value,qty_param,sku_status
from leaf
order by category,variant_code,sku_code;
"

CATALOG_BAD="$("${PSQL[@]}" -Atq -c "
$RELEVANT_CTE
select count(*)
from leaf
where sku_code is null
   or sku_status is distinct from 'active'
   or coalesce(default_unit_credits,0) <= 0
   or qty_mode not in ('fixed','param','metered');
")"
if [[ "${CATALOG_BAD:-999}" == "0" ]]; then
  echo "CATALOG_INTEGRITY=PASS"
else
  echo "CATALOG_INTEGRITY=FAIL bad_rows=${CATALOG_BAD:-unknown}"
fi

echo
echo "===== 2. PRICEBOOK + CREDIT VALUE COVERAGE ====="
"${PSQL[@]}" -P pager=off -c "
$RELEVANT_CTE,
pbs as (
  select id,name,currency,channel,country_code,tier_code,multiplier
  from public.pricing_pricebooks
  where is_active=true
    and effective_from <= now()
    and (effective_to is null or effective_to > now())
    and channel in ('web','mobile','api')
    and currency in ('USD','INR')
),
cv as (
  select distinct on (currency) currency,money_per_credit,rounding_mode
  from public.pricing_credit_value
  where effective_from <= now()
    and (effective_to is null or effective_to > now())
  order by currency,effective_from desc
)
select
  pb.name pricebook,
  pb.currency,
  pb.channel,
  coalesce(pb.country_code,'GLOBAL') country,
  coalesce(pb.tier_code,'ANY') tier,
  l.variant_code,
  l.sku_code,
  coalesce(sp.unit_credits_override,l.default_unit_credits) unit_credits,
  sp.unit_money_override,
  cv.money_per_credit,
  cv.rounding_mode,
  case
    when cv.money_per_credit is null or cv.money_per_credit <= 0 then 'FAIL:CREDIT_VALUE'
    when coalesce(sp.unit_credits_override,l.default_unit_credits,0) <= 0 then 'FAIL:CREDITS'
    else 'PASS'
  end coverage_gate
from leaf l
cross join pbs pb
left join public.pricing_sku_prices sp on sp.pricebook_id=pb.id and sp.sku_code=l.sku_code
left join cv on cv.currency=pb.currency
order by l.category,l.variant_code,pb.currency,pb.channel,pb.name;
"

PRICEBOOK_BAD="$("${PSQL[@]}" -Atq -c "
$RELEVANT_CTE,
pbs as (
  select id,currency
  from public.pricing_pricebooks
  where is_active=true
    and effective_from <= now()
    and (effective_to is null or effective_to > now())
    and channel in ('web','mobile','api')
    and currency in ('USD','INR')
),
cv as (
  select distinct on (currency) currency,money_per_credit
  from public.pricing_credit_value
  where effective_from <= now()
    and (effective_to is null or effective_to > now())
  order by currency,effective_from desc
)
select count(*)
from leaf l
cross join pbs pb
left join public.pricing_sku_prices sp on sp.pricebook_id=pb.id and sp.sku_code=l.sku_code
left join cv on cv.currency=pb.currency
where cv.money_per_credit is null
   or cv.money_per_credit <= 0
   or coalesce(sp.unit_credits_override,l.default_unit_credits,0) <= 0;
")"
if [[ "${PRICEBOOK_BAD:-999}" == "0" ]]; then
  echo "PRICEBOOK_CREDIT_COVERAGE=PASS"
else
  echo "PRICEBOOK_CREDIT_COVERAGE=FAIL bad_rows=${PRICEBOOK_BAD:-unknown}"
fi

echo
echo "===== 3. COGS COMPLETENESS / COST-MODEL SANITY ====="
"${PSQL[@]}" -P pager=off -c "
$RELEVANT_CTE,
costs as (
  select
    c.sku_code,
    count(*)::int component_count,
    string_agg(c.component_code,', ' order by c.component_code) components,
    string_agg(distinct c.cost_currency,',' order by c.cost_currency) currencies,
    sum(
      case lower(c.cost_model)
        when 'variable' then c.variable_cost_money
        when 'amortized' then case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
        when 'blended' then c.variable_cost_money + case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
        else 0
      end
    ) unit_cogs_usd,
    bool_or(
      c.variable_cost_money < 0
      or c.fixed_monthly_cost_money < 0
      or c.assumed_monthly_units < 0
      or lower(c.cost_model) not in ('variable','amortized','blended')
      or upper(c.cost_currency) <> 'USD'
      or (lower(c.cost_model) in ('amortized','blended') and c.fixed_monthly_cost_money > 0 and c.assumed_monthly_units <= 0)
    ) invalid_component
  from public.pricing_sku_costs c
  where c.is_active=true
    and c.effective_from <= now()
    and (c.effective_to is null or c.effective_to > now())
  group by c.sku_code
)
select
  l.category,l.variant_code,l.sku_code,l.provider_hint,l.unit,
  coalesce(c.component_count,0) component_count,
  c.components,c.currencies,round(c.unit_cogs_usd,8) unit_cogs_usd,
  case
    when coalesce(c.component_count,0)=0 then 'FAIL:MISSING_COGS'
    when coalesce(c.invalid_component,false) then 'FAIL:INVALID_COST_MODEL'
    when coalesce(c.unit_cogs_usd,0)<=0 then 'FAIL:ZERO_COGS'
    else 'PASS'
  end cogs_gate
from leaf l
left join costs c on c.sku_code=l.sku_code
order by l.category,l.variant_code,l.sku_code;
"

COGS_BAD="$("${PSQL[@]}" -Atq -c "
$RELEVANT_CTE,
costs as (
  select
    c.sku_code,
    count(*)::int component_count,
    sum(
      case lower(c.cost_model)
        when 'variable' then c.variable_cost_money
        when 'amortized' then case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
        when 'blended' then c.variable_cost_money + case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
        else 0
      end
    ) unit_cogs_usd,
    bool_or(
      c.variable_cost_money < 0
      or c.fixed_monthly_cost_money < 0
      or c.assumed_monthly_units < 0
      or lower(c.cost_model) not in ('variable','amortized','blended')
      or upper(c.cost_currency) <> 'USD'
      or (lower(c.cost_model) in ('amortized','blended') and c.fixed_monthly_cost_money > 0 and c.assumed_monthly_units <= 0)
    ) invalid_component
  from public.pricing_sku_costs c
  where c.is_active=true
    and c.effective_from <= now()
    and (c.effective_to is null or c.effective_to > now())
  group by c.sku_code
)
select count(*)
from leaf l
left join costs c on c.sku_code=l.sku_code
where coalesce(c.component_count,0)=0
   or coalesce(c.invalid_component,false)
   or coalesce(c.unit_cogs_usd,0)<=0;
")"
if [[ "${COGS_BAD:-999}" == "0" ]]; then
  echo "COGS_COMPLETENESS=PASS"
else
  echo "COGS_COMPLETENESS=FAIL bad_leaf_rows=${COGS_BAD:-unknown}"
fi

echo
echo "===== 4. MULTI-PERSON / GROUP PREMIUM CONTRACT ====="
"${PSQL[@]}" -P pager=off -c "
select
  s.code sku_code,
  s.category,
  s.default_unit_credits,
  s.metadata_json->>'premium_rate_multiplier' premium_rate_multiplier,
  s.metadata_json->>'source_sku' source_sku,
  s.metadata_json->>'pricing_policy' pricing_policy,
  case
    when s.code in ('FACE_MULTI_PERSON','AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON')
      and coalesce((s.metadata_json->>'premium')::boolean,false)=true
      then 'PREMIUM_SKU'
    else 'BASE_OR_OTHER'
  end role
from public.pricing_skus s
where s.status='active'
  and (
    s.code in ('FACE_MULTI_PERSON','AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON')
    or lower(s.category) in ('face','audio','fusion','fusion_extension')
  )
order by s.category,s.code;
"

PREMIUM_NOT_20="$("${PSQL[@]}" -Atq -c "
select count(*)
from public.pricing_skus s
where s.code in ('FACE_MULTI_PERSON','AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON')
  and s.status='active'
  and coalesce(nullif(s.metadata_json->>'premium_rate_multiplier','')::numeric,0) <> 1.20;
")"
if [[ "${PREMIUM_NOT_20:-999}" == "0" ]]; then
  echo "MULTI_PERSON_PREMIUM_METADATA_20PCT=PASS"
else
  echo "MULTI_PERSON_PREMIUM_METADATA_20PCT=FAIL rows=${PREMIUM_NOT_20:-unknown}"
fi

echo
echo "===== 5. WORST-CASE USD MARGIN BY ACTIVE VARIANT ====="
"${PSQL[@]}" -P pager=off -c "
$RELEVANT_CTE,
usd_credit_values as (
  select
    p.price_money /
    nullif(
      coalesce(
        nullif(p.metadata_json->>'included_credits_total','')::numeric,
        nullif(p.metadata_json->>'grant_credits','')::numeric,
        case when p.interval_code='yearly' then t.monthly_grant_credits::numeric*12 else t.monthly_grant_credits::numeric end
      ),0
    ) value
  from public.pricing_plan_prices p
  join public.pricing_tiers t on t.code=p.tier_code
  where p.is_active=true and p.is_public=true and upper(p.currency)='USD' and p.price_money>0
  union all
  select price_money/nullif(credits::numeric,0)
  from public.pricing_credit_packs
  where coalesce(is_active,true)=true and upper(currency)='USD' and price_money>0 and credits>0
),
min_credit as (select min(value) usd_per_credit from usd_credit_values),
pb as (
  select *
  from public.pricing_pricebooks
  where is_active=true and upper(currency)='USD' and channel='web'
    and effective_from<=now() and (effective_to is null or effective_to>now())
  order by case when coalesce(country_code,'')='' then 0 else 1 end,
           case when tier_code is null then 0 else 1 end,
           effective_from desc
  limit 1
),
costs as (
  select
    c.sku_code,
    sum(
      case lower(c.cost_model)
        when 'variable' then c.variable_cost_money
        when 'amortized' then case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
        when 'blended' then c.variable_cost_money + case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
        else 0
      end
    ) unit_cogs_usd
  from public.pricing_sku_costs c
  where c.is_active=true and c.effective_from<=now() and (c.effective_to is null or c.effective_to>now())
  group by c.sku_code
),
lines as (
  select
    l.category,l.variant_code,l.sku_code,
    (case when l.qty_mode='fixed' then coalesce(l.qty_value,0) else 1 end)::numeric qty,
    ceil(
      (case when l.qty_mode='fixed' then coalesce(l.qty_value,0) else 1 end)
      * coalesce(sp.unit_credits_override,l.default_unit_credits)::numeric
      * coalesce(pb.multiplier,1)::numeric
    )::numeric credits,
    c.unit_cogs_usd
  from leaf l
  cross join pb
  left join public.pricing_sku_prices sp on sp.pricebook_id=pb.id and sp.sku_code=l.sku_code
  left join costs c on c.sku_code=l.sku_code
),
agg as (
  select category,variant_code,
         sum(credits) credits,
         sum(qty*unit_cogs_usd) cogs_usd,
         bool_and(unit_cogs_usd is not null and unit_cogs_usd>0) cogs_complete
  from lines
  group by category,variant_code
)
select
  a.category,a.variant_code,a.credits,
  round(m.usd_per_credit,6) minimum_realized_usd_per_credit,
  round(a.credits*m.usd_per_credit,6) minimum_realized_revenue_usd,
  case when a.cogs_complete then round(a.cogs_usd,6) else null end cogs_usd,
  case when a.cogs_complete then round(a.credits*m.usd_per_credit-a.cogs_usd,6) else null end gross_margin_usd,
  case
    when not a.cogs_complete then 'FAIL:MISSING_COGS'
    when a.credits*m.usd_per_credit-a.cogs_usd < 0 then 'FAIL:NEGATIVE_MARGIN'
    else 'PASS'
  end margin_gate
from agg a cross join min_credit m
order by a.category,a.variant_code;
"

MARGIN_BAD="$("${PSQL[@]}" -Atq -c "
$RELEVANT_CTE,
usd_credit_values as (
  select p.price_money/nullif(coalesce(nullif(p.metadata_json->>'included_credits_total','')::numeric,nullif(p.metadata_json->>'grant_credits','')::numeric,case when p.interval_code='yearly' then t.monthly_grant_credits::numeric*12 else t.monthly_grant_credits::numeric end),0) value
  from public.pricing_plan_prices p join public.pricing_tiers t on t.code=p.tier_code
  where p.is_active=true and p.is_public=true and upper(p.currency)='USD' and p.price_money>0
  union all
  select price_money/nullif(credits::numeric,0) from public.pricing_credit_packs
  where coalesce(is_active,true)=true and upper(currency)='USD' and price_money>0 and credits>0
),
min_credit as (select min(value) usd_per_credit from usd_credit_values),
pb as (
  select * from public.pricing_pricebooks
  where is_active=true and upper(currency)='USD' and channel='web'
    and effective_from<=now() and (effective_to is null or effective_to>now())
  order by case when coalesce(country_code,'')='' then 0 else 1 end,
           case when tier_code is null then 0 else 1 end,effective_from desc limit 1
),
costs as (
  select c.sku_code,sum(case lower(c.cost_model)
    when 'variable' then c.variable_cost_money
    when 'amortized' then case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
    when 'blended' then c.variable_cost_money+case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
    else 0 end) unit_cogs_usd
  from public.pricing_sku_costs c
  where c.is_active=true and c.effective_from<=now() and (c.effective_to is null or c.effective_to>now())
  group by c.sku_code
),
agg as (
  select l.variant_code,
    sum(ceil((case when l.qty_mode='fixed' then coalesce(l.qty_value,0) else 1 end)
      *coalesce(sp.unit_credits_override,l.default_unit_credits)::numeric*coalesce(pb.multiplier,1)::numeric)) credits,
    sum((case when l.qty_mode='fixed' then coalesce(l.qty_value,0) else 1 end)*c.unit_cogs_usd) cogs_usd,
    bool_and(c.unit_cogs_usd is not null and c.unit_cogs_usd>0) cogs_complete
  from leaf l cross join pb
  left join public.pricing_sku_prices sp on sp.pricebook_id=pb.id and sp.sku_code=l.sku_code
  left join costs c on c.sku_code=l.sku_code
  group by l.variant_code
)
select count(*) from agg cross join min_credit
where not cogs_complete or credits*usd_per_credit-cogs_usd < 0;
")"
if [[ "${MARGIN_BAD:-999}" == "0" ]]; then
  echo "WORST_CASE_USD_MARGIN=PASS"
else
  echo "WORST_CASE_USD_MARGIN=FAIL variants=${MARGIN_BAD:-unknown}"
fi

echo
echo "===== 6. RECENT PREVIEW / RESERVE / COMMIT ECONOMICS ====="
"${PSQL[@]}" -P pager=off -c "
select
  created_at,status,
  quote_json->>'service_name' service_name,
  quote_json->>'service_action' service_action,
  quote_json->>'variant_code' variant_code,
  quote_json->>'sku_code' requested_code,
  reserved_credits,
  nullif(quote_json->>'total_credits','')::numeric quoted_credits,
  nullif(quote_json->>'final_charged_credits','')::numeric final_charged_credits,
  quote_json->'economics'->>'has_costs_complete' economics_complete,
  quote_json->'economics'->>'cogs_money_final' cogs_money_final,
  quote_json->'economics'->>'gross_margin_money_final' gross_margin_money_final,
  quote_json->'economics'->'missing_cost_skus' missing_cost_skus
from public.pricing_credit_reservations
where created_at >= now()-interval '7 days'
  and (
    lower(coalesce(quote_json->>'service_name','')) in ('svc-face','svc-audio','svc-fusion','svc-fusion-extension')
    or lower(coalesce(quote_json->>'category','')) in ('face','audio','fusion','fusion_extension')
  )
order by created_at desc
limit 100;
"

RECENT_ECON_BAD="$("${PSQL[@]}" -Atq -c "
select count(*)
from public.pricing_credit_reservations r
where r.created_at >= now()-interval '7 days'
  and r.status='committed'
  and (
    lower(coalesce(r.quote_json->>'service_name','')) in ('svc-face','svc-audio','svc-fusion','svc-fusion-extension')
    or lower(coalesce(r.quote_json->>'category','')) in ('face','audio','fusion','fusion_extension')
  )
  and (
    coalesce((r.quote_json->'economics'->>'has_costs_complete')::boolean,false)=false
    or r.quote_json->'economics'->>'cogs_money_final' is null
    or jsonb_array_length(coalesce(r.quote_json->'economics'->'missing_cost_skus','[]'::jsonb)) > 0
  );
")"
if [[ "${RECENT_ECON_BAD:-0}" == "0" ]]; then
  echo "RECENT_COMMITTED_ECONOMICS=PASS"
else
  echo "RECENT_COMMITTED_ECONOMICS=FAIL committed_rows=${RECENT_ECON_BAD:-unknown}"
fi

RESERVE_PARITY_BAD="$("${PSQL[@]}" -Atq -c "
select count(*)
from public.pricing_credit_reservations r
where r.created_at >= now()-interval '7 days'
  and r.status in ('reserved','committed')
  and lower(coalesce(r.quote_json->>'settlement_mode','prepaid')) <> 'postpaid'
  and coalesce(r.quote_json->>'billing_mode_snapshot',r.quote_json->>'billing_mode','bill')='bill'
  and nullif(r.quote_json->>'total_credits','')::numeric is not null
  and r.reserved_credits <> (r.quote_json->>'total_credits')::numeric;
")"
if [[ "${RESERVE_PARITY_BAD:-999}" == "0" ]]; then
  echo "QUOTE_RESERVE_CREDIT_PARITY=PASS"
else
  echo "QUOTE_RESERVE_CREDIT_PARITY=FAIL rows=${RESERVE_PARITY_BAD:-unknown}"
fi

FINAL_PARITY_BAD="$("${PSQL[@]}" -Atq -c "
select count(*)
from public.pricing_credit_reservations r
where r.created_at >= now()-interval '7 days'
  and r.status='committed'
  and lower(coalesce(r.quote_json->>'settlement_mode','prepaid')) <> 'postpaid'
  and coalesce(r.quote_json->>'billing_mode_snapshot',r.quote_json->>'billing_mode','bill')='bill'
  and nullif(r.quote_json->>'total_credits','')::numeric is not null
  and nullif(r.quote_json->>'final_charged_credits','')::numeric is distinct from (r.quote_json->>'total_credits')::numeric;
")"
if [[ "${FINAL_PARITY_BAD:-999}" == "0" ]]; then
  echo "QUOTE_COMMIT_CREDIT_PARITY=PASS"
else
  echo "QUOTE_COMMIT_CREDIT_PARITY=FAIL rows=${FINAL_PARITY_BAD:-unknown}"
fi

echo
echo "===== 7. REQUIRED LAUNCH VARIANT RUNTIME EVIDENCE ====="
"${PSQL[@]}" -P pager=off -c "
with expected(variant_code) as (
  values
    ('FACE_T2I'),
    ('FACE_I2I'),
    ('FACE_MULTI_PERSON'),
    ('AUDIO_TTS'),
    ('AUDIO_MULTI_PERSON'),
    ('FUSION_TALKING_VIDEO')
)
select
  e.variant_code,
  count(r.*) filter (where r.created_at >= now()-interval '30 days') recent_reservations,
  count(r.*) filter (where r.created_at >= now()-interval '30 days' and r.status='committed') recent_commits,
  case
    when count(r.*) filter (where r.created_at >= now()-interval '30 days' and r.status='committed') > 0 then 'PASS'
    else 'FAIL:NO_RECENT_COMMIT'
  end runtime_evidence
from expected e
left join public.pricing_credit_reservations r
  on coalesce(r.quote_json->>'variant_code',r.quote_json->>'sku_code')=e.variant_code
group by e.variant_code
order by e.variant_code;
"

EVIDENCE_BAD="$("${PSQL[@]}" -Atq -c "
with expected(variant_code) as (
  values ('FACE_T2I'),('FACE_I2I'),('FACE_MULTI_PERSON'),('AUDIO_TTS'),('AUDIO_MULTI_PERSON'),('FUSION_TALKING_VIDEO')
)
select count(*)
from expected e
where not exists (
  select 1 from public.pricing_credit_reservations r
  where r.created_at >= now()-interval '30 days'
    and r.status='committed'
    and coalesce(r.quote_json->>'variant_code',r.quote_json->>'sku_code')=e.variant_code
);
")"
if [[ "${EVIDENCE_BAD:-999}" == "0" ]]; then
  echo "LAUNCH_VARIANT_RUNTIME_EVIDENCE=PASS"
else
  echo "LAUNCH_VARIANT_RUNTIME_EVIDENCE=FAIL variants_without_recent_commit=${EVIDENCE_BAD:-unknown}"
fi

echo
echo "============================================================"
echo " FINAL VERDICT"
echo "============================================================"
echo "CATALOG_BAD=${CATALOG_BAD:-unknown}"
echo "PRICEBOOK_BAD=${PRICEBOOK_BAD:-unknown}"
echo "COGS_BAD=${COGS_BAD:-unknown}"
echo "PREMIUM_NOT_20=${PREMIUM_NOT_20:-unknown}"
echo "MARGIN_BAD=${MARGIN_BAD:-unknown}"
echo "RECENT_ECON_BAD=${RECENT_ECON_BAD:-unknown}"
echo "RESERVE_PARITY_BAD=${RESERVE_PARITY_BAD:-unknown}"
echo "FINAL_PARITY_BAD=${FINAL_PARITY_BAD:-unknown}"
echo "EVIDENCE_BAD=${EVIDENCE_BAD:-unknown}"
echo "workflow_mutation=NONE"
echo "generation_mutation=NONE"
echo "db_mutation=NONE"
echo "production=UNTOUCHED"

if [[ "${CATALOG_BAD:-999}" != "0"    || "${PRICEBOOK_BAD:-999}" != "0"    || "${COGS_BAD:-999}" != "0"    || "${PREMIUM_NOT_20:-999}" != "0"    || "${MARGIN_BAD:-999}" != "0"    || "${RECENT_ECON_BAD:-999}" != "0"    || "${RESERVE_PARITY_BAD:-999}" != "0"    || "${FINAL_PARITY_BAD:-999}" != "0"    || "${EVIDENCE_BAD:-999}" != "0" ]]; then
  echo "ALL_STUDIO_SKU_COGS_E2E=FAIL"
  exit 1
fi

echo "ALL_STUDIO_SKU_COGS_E2E=PASS"
