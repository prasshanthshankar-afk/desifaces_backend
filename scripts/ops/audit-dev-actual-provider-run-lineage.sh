#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
PRICING_CONTAINER="${PRICING_CONTAINER:-df-svc-pricing}"

docker inspect "$DB_CONTAINER" >/dev/null 2>&1 || { echo "FAIL: missing $DB_CONTAINER"; exit 2; }
docker inspect "$PRICING_CONTAINER" >/dev/null 2>&1 || { echo "FAIL: missing $PRICING_CONTAINER"; exit 2; }

DATABASE_URL="$(docker exec "$PRICING_CONTAINER" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DATABASE_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DATABASE_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

echo "============================================================"
echo " desifaces DEV — ACTUAL PROVIDER RUN LINEAGE"
echo " READ ONLY"
echo "============================================================"
echo "db_mutation=NONE"
echo "generation_mutation=NONE"
echo "production=UNTOUCHED"

echo
echo "===== SCHEMA ====="
for t in studio_jobs provider_runs pricing_credit_reservations; do
  ok="$("${PSQL[@]}" -Atq -c "select case when to_regclass('public.$t') is not null then 1 else 0 end;")"
  [[ "$ok" == "1" ]] || { echo "FAIL: public.$t missing"; exit 1; }
done
echo "PROVIDER_LINEAGE_SCHEMA=PASS"

echo
echo "===== FACE — ACTUAL PROVIDER BY MODE / PRICING VARIANT (90 DAYS) ====="
"${PSQL[@]}" -P pager=off -c "
select
  coalesce(nullif(sj.payload_json->>'mode',''),
           nullif(pr.request_json->>'mode',''),
           'unknown') as mode,
  coalesce(
    nullif(sj.payload_json->'pricing'->>'variant_code',''),
    nullif(sj.payload_json->'pricing'->>'sku_code',''),
    'unknown'
  ) as pricing_variant,
  pr.provider,
  count(*) as provider_runs,
  count(*) filter (where pr.provider_status in ('succeeded','completed','success')) as succeeded_runs,
  max(pr.updated_at) as last_seen
from public.provider_runs pr
join public.studio_jobs sj on sj.id=pr.job_id
where sj.studio_type='face'
  and pr.created_at >= now()-interval '90 days'
group by 1,2,3
order by 1,2,3;
"

echo
echo "===== FACE — RECENT RUN SAMPLES ====="
"${PSQL[@]}" -P pager=off -c "
select
  sj.id job_id,
  sj.created_at,
  sj.status,
  coalesce(sj.payload_json->>'mode',pr.request_json->>'mode') mode,
  sj.payload_json->'pricing'->>'variant_code' pricing_variant,
  sj.payload_json->'pricing'->>'sku_code' pricing_code,
  pr.provider,
  pr.provider_status,
  pr.request_json->>'width' width,
  pr.request_json->>'height' height,
  pr.response_json->>'provider' response_provider
from public.provider_runs pr
join public.studio_jobs sj on sj.id=pr.job_id
where sj.studio_type='face'
  and pr.created_at>=now()-interval '30 days'
order by pr.created_at desc
limit 40;
"

echo
echo "===== FUSION — ACTUAL PROVIDER BY PRICING VARIANT (90 DAYS) ====="
"${PSQL[@]}" -P pager=off -c "
select
  coalesce(
    nullif(sj.payload_json->'pricing'->>'variant_code',''),
    nullif(sj.payload_json->'pricing'->>'sku_code',''),
    case when coalesce((sj.payload_json->'pricing'->>'suppressed')::boolean,false) then 'INTERNAL_CHILD_SUPPRESSED' end,
    'unknown'
  ) as pricing_variant,
  pr.provider,
  count(*) as provider_runs,
  count(*) filter (where pr.provider_status in ('succeeded','completed','success')) as succeeded_runs,
  max(pr.updated_at) as last_seen
from public.provider_runs pr
join public.studio_jobs sj on sj.id=pr.job_id
where sj.studio_type='fusion'
  and pr.created_at>=now()-interval '90 days'
group by 1,2
order by 1,2;
"

echo
echo "===== FUSION — CONVERSATION / PROVIDER CONTEXT ====="
"${PSQL[@]}" -P pager=off -c "
select
  pr.provider,
  coalesce(
    sj.payload_json->'provider_options'->>'conversation_mode',
    sj.payload_json->'pricing_context'->>'conversation_mode',
    sj.payload_json->'pricing'->'meta'->>'conversation_mode',
    sj.payload_json->'tags'->>'conversation_mode',
    'unknown'
  ) conversation_mode,
  coalesce(
    sj.payload_json->'pricing'->>'variant_code',
    sj.payload_json->'pricing'->>'sku_code',
    'unknown'
  ) pricing_variant,
  count(*) runs,
  max(pr.updated_at) last_seen
from public.provider_runs pr
join public.studio_jobs sj on sj.id=pr.job_id
where sj.studio_type='fusion'
  and pr.created_at>=now()-interval '90 days'
group by 1,2,3
order by 2,3,1;
"

echo
echo "===== CHARGED RESERVATION -> PROVIDER RUN JOIN ====="
"${PSQL[@]}" -P pager=off -c "
with r as (
  select
    id reservation_id,
    created_at,
    status,
    quote_json,
    coalesce(
      nullif(quote_json->'params'->>'external_ref_id',''),
      nullif(quote_json->'params'->>'service_job_id',''),
      nullif(quote_json->'params'->>'studio_job_id','')
    ) job_ref
  from public.pricing_credit_reservations
  where created_at>=now()-interval '30 days'
    and status='committed'
    and lower(coalesce(quote_json->>'service_name','')) in ('svc-face','svc-fusion')
)
select
  r.created_at,
  r.quote_json->>'service_name' service_name,
  r.quote_json->>'service_action' service_action,
  coalesce(r.quote_json->>'variant_code',r.quote_json->>'sku_code') pricing_variant,
  r.quote_json->>'total_credits' total_credits,
  r.job_ref,
  sj.studio_type,
  pr.provider,
  pr.provider_status,
  pr.response_json->>'provider' response_provider
from r
left join public.studio_jobs sj
  on r.job_ref ~* '^[0-9a-f-]{36}$'
 and sj.id::text=r.job_ref
left join public.provider_runs pr on pr.job_id=sj.id
order by r.created_at desc,pr.created_at desc
limit 100;
"

echo
echo "===== PROVIDER DISTRIBUTION — ALL RECENT STUDIO JOBS ====="
"${PSQL[@]}" -P pager=off -c "
select
  sj.studio_type,
  pr.provider,
  count(*) provider_runs,
  count(*) filter (where pr.provider_status in ('succeeded','completed','success')) succeeded_runs,
  max(pr.updated_at) last_seen
from public.provider_runs pr
join public.studio_jobs sj on sj.id=pr.job_id
where pr.created_at>=now()-interval '30 days'
  and sj.studio_type in ('face','fusion')
group by 1,2
order by 1,provider_runs desc,2;
"

echo
echo "============================================================"
echo "ACTUAL_PROVIDER_RUN_LINEAGE=COMPLETE"
echo "db_mutation=NONE"
echo "generation_mutation=NONE"
echo "production=UNTOUCHED"
echo "============================================================"
