#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || { echo "FAIL: wrong host"; exit 1; }

DB=""
for candidate in desifaces-v3-db df-v3-db; do
  if docker inspect "$candidate" >/dev/null 2>&1; then DB="$candidate"; break; fi
done
[[ -n "$DB" ]] || { echo "FAIL: postgres container not found"; exit 1; }

PGUSER="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_USER"{print $2; exit}')"
PGDB="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_DB"{print $2; exit}')"
PGUSER="${PGUSER:-postgres}"
PGDB="${PGDB:-postgres}"
PSQL=(docker exec -i "$DB" psql -X -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB")

# Preserve timestamps as complete fields. Do not translate the delimiter to spaces:
# created_at/updated_at contain spaces and were previously split into invalid values.
IFS='|' read -r WORKFLOW_ID STORY_ID OWNER_USER_ID ACCOUNT_ID WF_CREATED WF_UPDATED < <(
  "${PSQL[@]}" -At -F '|' -c "
    select workflow_id,story_id,owner_user_id,account_id,created_at,updated_at
      from public.v3_studio_workflows
     where story_id is not null and state <> 'canceled'
     order by updated_at desc,created_at desc limit 1;"
)

[[ -n "${WORKFLOW_ID:-}" ]] || { echo "FAIL: no recent Story workflow"; exit 1; }
[[ -n "${WF_CREATED:-}" && -n "${WF_UPDATED:-}" ]] || { echo "FAIL: workflow timestamps missing"; exit 1; }

echo "============================================================"
echo " desifaces V3 — STORY PRICING RECONCILIATION (READ ONLY)"
echo "============================================================"
echo "workflow_id=$WORKFLOW_ID"
echo "story_id=$STORY_ID"
echo "owner_user_id=$OWNER_USER_ID"
echo "window=$WF_CREATED .. $WF_UPDATED"

echo
echo "===== 1. STAGE / ATTEMPT IDENTIFIERS ====="
"${PSQL[@]}" -P pager=off -c "
select s.stage_run_id,s.stage_type,s.state,s.generation_job_id,s.generation_request_id,
       a.attempt_id,a.attempt_no,a.attempt_kind,a.provider_job_ref,a.pricing_quote_id,a.state as attempt_state
  from public.v3_studio_stage_runs s
  left join public.v3_studio_stage_attempts a on a.stage_run_id=s.stage_run_id
 where s.workflow_id='$WORKFLOW_ID'::uuid
 order by s.created_at,a.attempt_no;"

echo
echo "===== 2. CORRELATED RESERVATIONS ====="
"${PSQL[@]}" -P pager=off -c "
with ids(id) as (
  values ('$WORKFLOW_ID'::text),('$STORY_ID'::text)
  union
  select stage_run_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid
  union
  select generation_job_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and generation_job_id is not null
  union
  select generation_request_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and generation_request_id is not null
  union
  select attempt_id::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid)
  union
  select provider_job_ref::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid) and provider_job_ref is not null
), rr as (
  select r.*
    from public.pricing_credit_reservations r
   where r.user_id='$OWNER_USER_ID'::uuid
     and r.created_at >= '$WF_CREATED'::timestamptz - interval '10 minutes'
     and r.created_at <= '$WF_UPDATED'::timestamptz + interval '10 minutes'
     and exists (select 1 from ids where to_jsonb(r)::text like '%'||ids.id||'%')
)
select id,status,service_name,service_action,sku_code,job_ref,reserved_credits,
       coalesce((quote_json->>'final_charged_credits')::numeric,0) as final_charged_credits,
       quote_json->>'ledger_entry_id' as ledger_entry_id,
       quote_json#>>'{finalize,final_charged_credits}' as finalize_charged_credits,
       quote_json#>>'{finalize,timestamp}' as finalize_timestamp,
       created_at,finalized_at
  from rr
 order by created_at,id;"

echo
echo "===== 3. RESERVATION TOTALS BY SERVICE ====="
"${PSQL[@]}" -P pager=off -c "
with ids(id) as (
  values ('$WORKFLOW_ID'::text),('$STORY_ID'::text)
  union select stage_run_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid
  union select generation_job_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and generation_job_id is not null
  union select generation_request_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and generation_request_id is not null
  union select attempt_id::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid)
  union select provider_job_ref::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid) and provider_job_ref is not null
), rr as (
  select r.* from public.pricing_credit_reservations r
   where r.user_id='$OWNER_USER_ID'::uuid
     and r.created_at >= '$WF_CREATED'::timestamptz - interval '10 minutes'
     and r.created_at <= '$WF_UPDATED'::timestamptz + interval '10 minutes'
     and exists (select 1 from ids where to_jsonb(r)::text like '%'||ids.id||'%')
)
select coalesce(service_name,'') service_name,coalesce(service_action,'') service_action,coalesce(sku_code,'') sku_code,
       count(*) reservations,
       count(*) filter (where status='committed') committed,
       sum(coalesce((quote_json->>'final_charged_credits')::numeric,0)) as final_charged_credits
  from rr
 group by 1,2,3
 order by 1,2,3;"

echo
echo "===== 4. LEDGER EVENTS VIA RESERVATION LEDGER IDS + IDENTIFIERS ====="
"${PSQL[@]}" -P pager=off -c "
with ids(id) as (
  values ('$WORKFLOW_ID'::text),('$STORY_ID'::text)
  union select stage_run_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid
  union select generation_job_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and generation_job_id is not null
  union select generation_request_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and generation_request_id is not null
  union select attempt_id::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid)
  union select provider_job_ref::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid) and provider_job_ref is not null
), rr as (
  select r.* from public.pricing_credit_reservations r
   where r.user_id='$OWNER_USER_ID'::uuid
     and r.created_at >= '$WF_CREATED'::timestamptz - interval '10 minutes'
     and r.created_at <= '$WF_UPDATED'::timestamptz + interval '10 minutes'
     and exists (select 1 from ids where to_jsonb(r)::text like '%'||ids.id||'%')
), ledger_ids as (
  select nullif(quote_json->>'ledger_entry_id','') as id from rr
  union select nullif(quote_json#>>'{finalize,ledger_entry_id}','') from rr
), relevant as (
  select le.*
    from public.pricing_credit_ledger_events le
   where le.user_id='$OWNER_USER_ID'::uuid
     and le.created_at >= '$WF_CREATED'::timestamptz - interval '10 minutes'
     and le.created_at <= '$WF_UPDATED'::timestamptz + interval '10 minutes'
     and (
       le.id::text in (select id from ledger_ids where id is not null)
       or exists (select 1 from ids where to_jsonb(le)::text like '%'||ids.id||'%')
     )
)
select id,event_type,credits_delta,sku_code,service_name,service_action,studio_job_id,idempotency_key,created_at
  from relevant order by created_at,id;"

echo
echo "===== 5. FINAL RECONCILIATION ====="
"${PSQL[@]}" -P pager=off -c "
with ids(id) as (
  values ('$WORKFLOW_ID'::text),('$STORY_ID'::text)
  union select stage_run_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid
  union select generation_job_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and generation_job_id is not null
  union select generation_request_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and generation_request_id is not null
  union select attempt_id::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid)
  union select provider_job_ref::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid) and provider_job_ref is not null
), rr as (
  select r.* from public.pricing_credit_reservations r
   where r.user_id='$OWNER_USER_ID'::uuid
     and r.created_at >= '$WF_CREATED'::timestamptz - interval '10 minutes'
     and r.created_at <= '$WF_UPDATED'::timestamptz + interval '10 minutes'
     and exists (select 1 from ids where to_jsonb(r)::text like '%'||ids.id||'%')
), ledger_ids as (
  select nullif(quote_json->>'ledger_entry_id','') as id from rr
  union select nullif(quote_json#>>'{finalize,ledger_entry_id}','') from rr
), relevant as (
  select le.* from public.pricing_credit_ledger_events le
   where le.user_id='$OWNER_USER_ID'::uuid
     and le.created_at >= '$WF_CREATED'::timestamptz - interval '10 minutes'
     and le.created_at <= '$WF_UPDATED'::timestamptz + interval '10 minutes'
     and (le.id::text in (select id from ledger_ids where id is not null)
          or exists (select 1 from ids where to_jsonb(le)::text like '%'||ids.id||'%'))
), dupes as (
  select idempotency_key,count(*) n
    from relevant
   where event_type='consume' and credits_delta<0 and coalesce(idempotency_key,'')<>''
   group by idempotency_key having count(*)>1
)
select
  (select count(*) from rr) as matched_reservations,
  (select count(*) from rr where status='committed') as committed_reservations,
  (select coalesce(sum(coalesce((quote_json->>'final_charged_credits')::numeric,0)),0) from rr where status='committed') as reservation_final_credits,
  (select coalesce(sum(abs(credits_delta)),0) from relevant where event_type='consume' and credits_delta<0) as ledger_consumed_credits,
  (select coalesce(sum(credits_delta),0) from relevant where credits_delta>0 and (event_type ilike '%refund%' or event_type ilike '%reversal%')) as ledger_refunded_credits,
  (select count(*) from dupes) as duplicate_idempotency_groups;"

echo
echo "READ_ONLY=PASS"
echo "NEXT=USE_RECONCILIATION_TOTALS_TO_CERTIFY_WORKFLOW_PRICING"
