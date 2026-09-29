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
echo " desifaces DEV — PARENT VIDEO COGS RECONCILIATION"
echo " READ ONLY / NO GENERATION"
echo "============================================================"
echo "provider_rates=public_current_upper_bound"
echo "sync3_usd_per_sec=0.133"
echo "veed_fabric_usd_per_sec=0.150"
echo "omnihuman_v15_usd_per_sec=0.160"
echo "kling_standard_usd_per_sec=0.0562"
echo "kling_pro_usd_per_sec=0.115"
echo "db_mutation=NONE"
echo "generation_mutation=NONE"
echo "production=UNTOUCHED"

echo
echo "===== 1. PARENT RESERVATIONS / STAGE IDS ====="
"${PSQL[@]}" -P pager=off -c "
select
  r.id reservation_id,
  r.created_at,
  r.status,
  coalesce(r.quote_json->>'variant_code',r.quote_json->>'sku_code') variant_code,
  r.quote_json->'params'->>'external_ref_type' external_ref_type,
  r.quote_json->'params'->>'external_ref_id' stage_run_id,
  r.quote_json->'params'->>'workflow_id' workflow_id,
  r.quote_json->'params'->>'actual_audio_duration_sec' priced_audio_sec,
  r.quote_json->'params'->>'minutes' priced_minutes,
  r.quote_json->>'total_credits' total_credits
from public.pricing_credit_reservations r
where r.created_at >= now()-interval '30 days'
  and r.status in ('reserved','committed')
  and r.quote_json->>'service_action'='fusion.video.generate'
order by r.created_at desc
limit 50;
"

echo
echo "===== 2. CHILD JOBS BY PARENT STAGE ====="
"${PSQL[@]}" -P pager=off -c "
with parents as (
  select distinct
    r.id reservation_id,
    r.created_at parent_created_at,
    r.quote_json,
    nullif(r.quote_json->'params'->>'external_ref_id','') stage_run_id
  from public.pricing_credit_reservations r
  where r.created_at >= now()-interval '30 days'
    and r.status='committed'
    and r.quote_json->>'service_action'='fusion.video.generate'
),
children as (
  select
    p.reservation_id,
    p.parent_created_at,
    p.stage_run_id,
    sj.id child_job_id,
    sj.status child_status,
    sj.created_at child_created_at,
    coalesce(
      sj.payload_json->'tags'->>'stage_run_id',
      sj.payload_json->'pricing_context'->>'billing_parent_job_id',
      sj.payload_json->'billing_context'->>'billing_parent_job_id',
      sj.payload_json->'pricing'->>'parent_job_id'
    ) child_parent_stage,
    nullif(sj.payload_json->'video'->>'duration_sec','')::numeric requested_duration_sec,
    sj.payload_json->>'provider' requested_provider,
    coalesce((sj.payload_json->'pricing'->>'suppressed')::boolean,false) pricing_suppressed
  from parents p
  join public.studio_jobs sj
    on (
      sj.payload_json->'tags'->>'stage_run_id'=p.stage_run_id
      or sj.payload_json->'pricing_context'->>'billing_parent_job_id'=p.stage_run_id
      or sj.payload_json->'billing_context'->>'billing_parent_job_id'=p.stage_run_id
      or sj.payload_json->'pricing'->>'parent_job_id'=p.stage_run_id
    )
   and sj.studio_type='fusion'
)
select
  c.parent_created_at,
  c.reservation_id,
  c.stage_run_id,
  c.child_job_id,
  c.child_status,
  c.requested_duration_sec,
  c.requested_provider,
  c.pricing_suppressed,
  pr.provider actual_provider,
  pr.provider_status,
  coalesce(
    pr.response_json->'provider_meta'->>'model_id',
    pr.response_json->'provider_meta'->>'provider_model_name',
    pr.meta_json->>'model_id',
    pr.request_json->>'model_id',
    ''
  ) model_id,
  pr.created_at provider_run_created_at
from children c
left join public.provider_runs pr on pr.job_id=c.child_job_id
order by c.parent_created_at desc,c.child_job_id,pr.created_at;
"

echo
echo "===== 3. PARENT COGS USING CURRENT PUBLIC PROVIDER UPPER-BOUND RATES ====="
"${PSQL[@]}" -P pager=off -c "
with parents as (
  select distinct
    r.id reservation_id,
    r.created_at,
    r.quote_json,
    nullif(r.quote_json->'params'->>'external_ref_id','') stage_run_id,
    nullif(r.quote_json->'params'->>'actual_audio_duration_sec','')::numeric priced_audio_sec,
    nullif(r.quote_json->>'total_credits','')::numeric total_credits
  from public.pricing_credit_reservations r
  where r.created_at >= now()-interval '30 days'
    and r.status='committed'
    and r.quote_json->>'service_action'='fusion.video.generate'
),
children as (
  select
    p.*,
    sj.id child_job_id,
    nullif(sj.payload_json->'video'->>'duration_sec','')::numeric requested_duration_sec
  from parents p
  join public.studio_jobs sj
    on sj.studio_type='fusion'
   and (
      sj.payload_json->'tags'->>'stage_run_id'=p.stage_run_id
      or sj.payload_json->'pricing_context'->>'billing_parent_job_id'=p.stage_run_id
      or sj.payload_json->'billing_context'->>'billing_parent_job_id'=p.stage_run_id
      or sj.payload_json->'pricing'->>'parent_job_id'=p.stage_run_id
   )
),
runs as (
  select
    c.*,
    pr.provider,
    pr.provider_status,
    case
      when pr.provider='sync3' then 0.133::numeric
      when pr.provider='veed_fabric' then 0.150::numeric
      when pr.provider='omnihuman_v15' then 0.160::numeric
      when pr.provider='kling' then 0.115::numeric
      else null::numeric
    end as usd_per_sec
  from children c
  join public.provider_runs pr on pr.job_id=c.child_job_id
),
agg as (
  select
    reservation_id,
    max(created_at) created_at,
    max(stage_run_id) stage_run_id,
    max(priced_audio_sec) priced_audio_sec,
    max(total_credits) total_credits,
    count(*) provider_attempts,
    count(*) filter (where provider_status in ('succeeded','completed','success')) succeeded_provider_attempts,
    string_agg(distinct provider,',' order by provider) providers,
    sum(
      coalesce(requested_duration_sec,0) * coalesce(usd_per_sec,0)
    ) provider_cogs_usd,
    count(*) filter (where requested_duration_sec is null or usd_per_sec is null) incomplete_cost_attempts
  from runs
  group by reservation_id
),
credit as (
  select min(value) usd_per_credit
  from (
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
  ) x
)
select
  a.created_at,
  a.reservation_id,
  a.stage_run_id,
  a.priced_audio_sec,
  a.providers,
  a.provider_attempts,
  a.succeeded_provider_attempts,
  round(a.provider_cogs_usd,4) provider_cogs_usd,
  a.total_credits,
  round(a.total_credits*c.usd_per_credit,4) minimum_realized_revenue_usd,
  round(a.total_credits*c.usd_per_credit-a.provider_cogs_usd,4) minimum_realized_margin_usd,
  a.incomplete_cost_attempts,
  case
    when a.incomplete_cost_attempts>0 then 'INCOMPLETE'
    when a.total_credits*c.usd_per_credit-a.provider_cogs_usd<0 then 'LOSS'
    else 'NONNEGATIVE'
  end economics
from agg a cross join credit c
order by a.created_at desc;
"

echo
echo "===== 4. PROVIDER / DURATION COVERAGE SUMMARY ====="
"${PSQL[@]}" -P pager=off -c "
with children as (
  select
    sj.id child_job_id,
    nullif(sj.payload_json->'video'->>'duration_sec','')::numeric requested_duration_sec,
    coalesce(
      sj.payload_json->'tags'->>'stage_run_id',
      sj.payload_json->'pricing_context'->>'billing_parent_job_id',
      sj.payload_json->'billing_context'->>'billing_parent_job_id',
      sj.payload_json->'pricing'->>'parent_job_id'
    ) parent_stage
  from public.studio_jobs sj
  where sj.studio_type='fusion'
    and sj.created_at>=now()-interval '30 days'
    and coalesce((sj.payload_json->'pricing'->>'suppressed')::boolean,false)=true
)
select
  pr.provider,
  count(*) attempts,
  count(*) filter (where pr.provider_status in ('succeeded','completed','success')) succeeded,
  count(*) filter (where c.requested_duration_sec is null) missing_duration,
  round(sum(coalesce(c.requested_duration_sec,0)),2) requested_seconds,
  max(pr.updated_at) last_seen
from children c
join public.provider_runs pr on pr.job_id=c.child_job_id
where c.parent_stage is not null
group by pr.provider
order by attempts desc,pr.provider;
"

echo
echo "============================================================"
echo "PARENT_VIDEO_COGS_RECONCILIATION=COMPLETE"
echo "db_mutation=NONE"
echo "generation_mutation=NONE"
echo "production=UNTOUCHED"
echo "============================================================"
