#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
COMPOSE=(bash "$ROOT/scripts/v3-compose.sh")
STAGE_ID="${STAGE_ID:?STAGE_ID is required}"

POSTGRES_DB="$(awk -F= '$1=="POSTGRES_DB"{sub(/^[^=]*=/,""); print; exit}' infra/.env)"
POSTGRES_USER="$(awk -F= '$1=="POSTGRES_USER"{sub(/^[^=]*=/,""); print; exit}' infra/.env)"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || { echo "HOLD: refusing non-V3 DB: $POSTGRES_DB" >&2; exit 1; }

PSQL=("${COMPOSE[@]}" exec -T desifaces-db psql -X -P pager=off -U "$POSTGRES_USER" -d "$POSTGRES_DB")

echo
echo "===== V3 FUSION PERFORMANCE EVIDENCE ====="
echo "stage_id=$STAGE_ID"

"${PSQL[@]}" -v stage_id="$STAGE_ID" <<'SQL'
\echo '--- A. Stage / attempt / dispatch telemetry ---'
select
  s.workflow_id,
  s.state as stage_state,
  s.metadata_json #>> '{fusion_parent_pricing,state}' as parent_pricing_state,
  a.attempt_id,
  a.state as attempt_state,
  a.metadata_json #>> '{dispatch_performance,execution_mode}' as execution_mode,
  a.metadata_json #>> '{dispatch_performance,requested_children}' as requested_children,
  a.metadata_json #>> '{dispatch_performance,accepted_children}' as accepted_children,
  a.metadata_json #>> '{dispatch_performance,preserved_children}' as preserved_children,
  a.metadata_json #>> '{dispatch_performance,dispatch_concurrency}' as dispatch_concurrency,
  a.metadata_json #>> '{dispatch_performance,max_parallel_dispatch_observed}' as max_parallel_dispatch_observed,
  a.metadata_json #>> '{dispatch_performance,first_child_submitted_at}' as first_child_submitted_at,
  a.metadata_json #>> '{dispatch_performance,last_child_submitted_at}' as last_child_submitted_at,
  a.metadata_json #>> '{dispatch_performance,dispatch_spread_ms}' as dispatch_spread_ms,
  a.metadata_json #>> '{dispatch_performance,dispatch_elapsed_ms}' as dispatch_elapsed_ms,
  a.metadata_json #>> '{background_coordinator,phase}' as background_phase,
  a.metadata_json #>> '{background_coordinator,stitch_ms}' as stitch_ms,
  a.metadata_json #>> '{background_coordinator,stitch_started_at}' as stitch_started_at,
  a.metadata_json #>> '{background_coordinator,stitch_completed_at}' as stitch_completed_at,
  a.metadata_json #>> '{background_coordinator,finalized_at}' as finalized_at
from public.v3_studio_stage_runs s
join lateral (
  select * from public.v3_studio_stage_attempts
  where stage_run_id=s.stage_run_id
  order by attempt_no desc limit 1
) a on true
where s.stage_run_id=:'stage_id'::uuid;

\echo '--- B. Child lifecycle timing ---'
with jobs as (
  select j.*
  from public.studio_jobs j
  where j.studio_type='fusion'
    and (
      j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}' = :'stage_id'
      or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}' = :'stage_id'
      or j.payload_json #>> '{billing_context,billing_parent_job_id}' = :'stage_id'
      or j.payload_json #>> '{pricing,parent_job_id}' = :'stage_id'
    )
), runs as (
  select
    j.id as job_id,
    j.status as job_status,
    j.created_at,
    nullif(j.meta_json->>'worker_claimed_at','')::timestamptz as worker_claimed_at,
    r.provider,
    r.provider_status,
    nullif(r.meta_json->>'provider_submitted_at','')::timestamptz as provider_submitted_at,
    nullif(r.meta_json->>'provider_first_processing_at','')::timestamptz as provider_first_processing_at,
    nullif(r.meta_json->>'provider_terminal_at','')::timestamptz as provider_terminal_at
  from jobs j
  left join lateral (
    select * from public.provider_runs p
    where p.job_id=j.id
    order by p.created_at desc limit 1
  ) r on true
)
select
  job_id,
  job_status,
  provider,
  provider_status,
  round(extract(epoch from (worker_claimed_at-created_at))*1000)::bigint as queue_wait_ms,
  round(extract(epoch from (provider_submitted_at-worker_claimed_at))*1000)::bigint as worker_to_provider_submit_ms,
  round(extract(epoch from (provider_first_processing_at-provider_submitted_at))*1000)::bigint as provider_queue_ms,
  round(extract(epoch from (provider_terminal_at-provider_first_processing_at))*1000)::bigint as provider_processing_ms,
  round(extract(epoch from (provider_terminal_at-provider_submitted_at))*1000)::bigint as submission_to_terminal_ms,
  provider_submitted_at,
  provider_first_processing_at,
  provider_terminal_at
from runs
order by provider_submitted_at nulls last,job_id;

\echo '--- C. Aggregate p50 / p95 and provider overlap ---'
with jobs as (
  select j.*
  from public.studio_jobs j
  where j.studio_type='fusion'
    and (
      j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}' = :'stage_id'
      or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}' = :'stage_id'
      or j.payload_json #>> '{billing_context,billing_parent_job_id}' = :'stage_id'
      or j.payload_json #>> '{pricing,parent_job_id}' = :'stage_id'
    )
), runs as (
  select
    j.id as job_id,
    j.created_at,
    nullif(j.meta_json->>'worker_claimed_at','')::timestamptz as worker_claimed_at,
    nullif(r.meta_json->>'provider_submitted_at','')::timestamptz as submitted_at,
    nullif(r.meta_json->>'provider_first_processing_at','')::timestamptz as processing_at,
    nullif(r.meta_json->>'provider_terminal_at','')::timestamptz as terminal_at
  from jobs j
  join lateral (
    select * from public.provider_runs p
    where p.job_id=j.id
    order by p.created_at desc limit 1
  ) r on true
), metrics as (
  select *,
    extract(epoch from (worker_claimed_at-created_at))*1000 as queue_wait_ms,
    extract(epoch from (processing_at-submitted_at))*1000 as provider_queue_ms,
    extract(epoch from (terminal_at-processing_at))*1000 as provider_processing_ms,
    extract(epoch from (terminal_at-submitted_at))*1000 as submission_to_terminal_ms,
    coalesce(processing_at,submitted_at) as overlap_start
  from runs
), overlap as (
  select m1.job_id,
    (select count(*) from metrics m2
      where m2.overlap_start is not null and m2.terminal_at is not null
        and m2.overlap_start <= m1.overlap_start
        and m2.terminal_at > m1.overlap_start) as concurrent_at_start
  from metrics m1
  where m1.overlap_start is not null and m1.terminal_at is not null
)
select
  count(*) as child_runs,
  count(*) filter(where worker_claimed_at is not null) as with_worker_claim_timing,
  count(*) filter(where submitted_at is not null) as with_provider_submit_timing,
  count(*) filter(where processing_at is not null) as with_provider_processing_timing,
  count(*) filter(where terminal_at is not null) as with_terminal_timing,
  round(percentile_cont(0.50) within group(order by queue_wait_ms) filter(where queue_wait_ms is not null))::bigint as queue_wait_p50_ms,
  round(percentile_cont(0.95) within group(order by queue_wait_ms) filter(where queue_wait_ms is not null))::bigint as queue_wait_p95_ms,
  round(percentile_cont(0.50) within group(order by provider_queue_ms) filter(where provider_queue_ms is not null))::bigint as provider_queue_p50_ms,
  round(percentile_cont(0.95) within group(order by provider_queue_ms) filter(where provider_queue_ms is not null))::bigint as provider_queue_p95_ms,
  round(percentile_cont(0.50) within group(order by provider_processing_ms) filter(where provider_processing_ms is not null))::bigint as provider_processing_p50_ms,
  round(percentile_cont(0.95) within group(order by provider_processing_ms) filter(where provider_processing_ms is not null))::bigint as provider_processing_p95_ms,
  round(percentile_cont(0.50) within group(order by submission_to_terminal_ms) filter(where submission_to_terminal_ms is not null))::bigint as submit_to_terminal_p50_ms,
  round(percentile_cont(0.95) within group(order by submission_to_terminal_ms) filter(where submission_to_terminal_ms is not null))::bigint as submit_to_terminal_p95_ms,
  (select max(concurrent_at_start) from overlap) as max_provider_overlap
from metrics;

\echo '--- D. Completion curve from first provider submission ---'
with jobs as (
  select j.id
  from public.studio_jobs j
  where j.studio_type='fusion'
    and (
      j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}' = :'stage_id'
      or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}' = :'stage_id'
      or j.payload_json #>> '{billing_context,billing_parent_job_id}' = :'stage_id'
      or j.payload_json #>> '{pricing,parent_job_id}' = :'stage_id'
    )
), runs as (
  select
    nullif(r.meta_json->>'provider_submitted_at','')::timestamptz as submitted_at,
    nullif(r.meta_json->>'provider_terminal_at','')::timestamptz as terminal_at
  from jobs j join public.provider_runs r on r.job_id=j.id
), base as (
  select min(submitted_at) as t0 from runs
), curve as (
  select
    terminal_at,
    row_number() over(order by terminal_at) as n,
    count(*) over() as total,
    extract(epoch from (terminal_at-(select t0 from base))) as elapsed_sec
  from runs where terminal_at is not null
)
select
  min(elapsed_sec) filter(where n>=1) as first_complete_sec,
  min(elapsed_sec) filter(where n>=ceil(total*0.25)) as p25_complete_sec,
  min(elapsed_sec) filter(where n>=ceil(total*0.50)) as p50_complete_sec,
  min(elapsed_sec) filter(where n>=ceil(total*0.90)) as p90_complete_sec,
  max(elapsed_sec) as all_complete_sec
from curve;

\echo '--- E. Pricing / no-duplicate invariants ---'
with stage as (
  select workflow_id,metadata_json
  from public.v3_studio_stage_runs where stage_run_id=:'stage_id'::uuid
), jobs as (
  select j.*
  from public.studio_jobs j
  where j.studio_type='fusion'
    and (
      j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}' = :'stage_id'
      or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}' = :'stage_id'
      or j.payload_json #>> '{billing_context,billing_parent_job_id}' = :'stage_id'
      or j.payload_json #>> '{pricing,parent_job_id}' = :'stage_id'
    )
), owner as (
  select w.owner_user_id from stage s join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
)
select
  (select count(*) from jobs) as child_jobs,
  (select count(*) from jobs where payload_json #>> '{pricing,state}'='suppressed'
    or payload_json #>> '{provider_options,pricing,state}'='suppressed'
    or payload_json #>> '{tags,pricing,state}'='suppressed') as suppressed_child_jobs,
  (select metadata_json #>> '{fusion_parent_pricing,state}' from stage) as parent_pricing_state,
  (select count(*) from public.pricing_credit_ledger_events l,owner o
    where l.user_id=o.owner_user_id
      and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:' || :'stage_id' || ':commit:%') as parent_consume_events,
  (select coalesce(sum(l.credits_delta),0) from public.pricing_credit_ledger_events l,owner o
    where l.user_id=o.owner_user_id
      and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:' || :'stage_id' || ':commit:%') as parent_credit_delta;
SQL

echo
echo "PERFORMANCE_REPORT = COMPLETE"
echo "Interpret max_provider_overlap using provider_first_processing_at when available; otherwise it conservatively uses submission-to-terminal overlap."
