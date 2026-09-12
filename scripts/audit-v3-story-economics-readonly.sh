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

IFS='|' read -r WORKFLOW_ID STORY_ID OWNER_USER_ID WF_CREATED WF_UPDATED < <(
  "${PSQL[@]}" -At -F '|' -c "
    select workflow_id,story_id,owner_user_id,created_at,updated_at
      from public.v3_studio_workflows
     where story_id is not null and state <> 'canceled'
     order by updated_at desc,created_at desc limit 1;"
)
[[ -n "${WORKFLOW_ID:-}" ]] || { echo "FAIL: no recent Story workflow"; exit 1; }

echo "============================================================"
echo " desifaces V3 — STORY ECONOMICS AUDIT (READ ONLY)"
echo "============================================================"
echo "workflow_id=$WORKFLOW_ID"
echo "story_id=$STORY_ID"
echo "owner_user_id=$OWNER_USER_ID"
echo "window=$WF_CREATED .. $WF_UPDATED"

echo
echo "===== ECONOMICS BY SERVICE ====="
"${PSQL[@]}" -P pager=off -c "
with ids(id) as (
  values ('$WORKFLOW_ID'::text),('$STORY_ID'::text)
  union select stage_run_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid
  union select generation_job_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and generation_job_id is not null
  union select generation_request_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and generation_request_id is not null
  union select attempt_id::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid)
  union select provider_job_ref::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid) and provider_job_ref is not null
), rr as (
  select r.*
    from public.pricing_credit_reservations r
   where r.user_id='$OWNER_USER_ID'::uuid
     and r.created_at >= '$WF_CREATED'::timestamptz - interval '10 minutes'
     and r.created_at <= '$WF_UPDATED'::timestamptz + interval '10 minutes'
     and r.status='committed'
     and exists (select 1 from ids where to_jsonb(r)::text like '%'||ids.id||'%')
), e as (
  select service_name,service_action,sku_code,
         coalesce(nullif(quote_json#>>'{economics,revenue_money_final}','')::numeric,
                  nullif(quote_json#>>'{economics,revenue_money_est}','')::numeric,
                  nullif(quote_json->>'final_charged_money','')::numeric,
                  nullif(quote_json->>'total_money','')::numeric,0) revenue,
         coalesce(nullif(quote_json#>>'{economics,cogs_money_final}','')::numeric,
                  nullif(quote_json#>>'{economics,cogs_money_est}','')::numeric,
                  nullif(quote_json#>>'{economics,cogs_usd_total}','')::numeric,0) cogs,
         coalesce(nullif(quote_json->>'final_charged_credits','')::numeric,
                  nullif(quote_json#>>'{finalize,final_charged_credits}','')::numeric,0) credits
    from rr
)
select coalesce(service_name,'') service_name,
       coalesce(service_action,'') service_action,
       coalesce(sku_code,'') sku_code,
       count(*) operations,
       sum(credits) credits,
       round(sum(revenue),4) revenue_usd,
       round(sum(cogs),4) cogs_usd,
       round(sum(revenue)-sum(cogs),4) gross_profit_usd,
       case when sum(revenue)>0 then round(((sum(revenue)-sum(cogs))/sum(revenue))*100,2) else null end gross_margin_pct
  from e
 group by 1,2,3
 order by 1,2,3;"

echo
echo "===== OVERALL WORKFLOW ECONOMICS ====="
"${PSQL[@]}" -P pager=off -c "
with ids(id) as (
  values ('$WORKFLOW_ID'::text),('$STORY_ID'::text)
  union select stage_run_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid
  union select generation_job_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and generation_job_id is not null
  union select generation_request_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and generation_request_id is not null
  union select attempt_id::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid)
  union select provider_job_ref::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid) and provider_job_ref is not null
), rr as (
  select r.*
    from public.pricing_credit_reservations r
   where r.user_id='$OWNER_USER_ID'::uuid
     and r.created_at >= '$WF_CREATED'::timestamptz - interval '10 minutes'
     and r.created_at <= '$WF_UPDATED'::timestamptz + interval '10 minutes'
     and r.status='committed'
     and exists (select 1 from ids where to_jsonb(r)::text like '%'||ids.id||'%')
), e as (
  select coalesce(nullif(quote_json#>>'{economics,revenue_money_final}','')::numeric,
                  nullif(quote_json#>>'{economics,revenue_money_est}','')::numeric,
                  nullif(quote_json->>'final_charged_money','')::numeric,
                  nullif(quote_json->>'total_money','')::numeric,0) revenue,
         coalesce(nullif(quote_json#>>'{economics,cogs_money_final}','')::numeric,
                  nullif(quote_json#>>'{economics,cogs_money_est}','')::numeric,
                  nullif(quote_json#>>'{economics,cogs_usd_total}','')::numeric,0) cogs,
         coalesce(nullif(quote_json->>'final_charged_credits','')::numeric,
                  nullif(quote_json#>>'{finalize,final_charged_credits}','')::numeric,0) credits
    from rr
)
select count(*) operations,
       sum(credits) total_credits,
       round(sum(revenue),4) revenue_usd,
       round(sum(cogs),4) cogs_usd,
       round(sum(revenue)-sum(cogs),4) gross_profit_usd,
       case when sum(revenue)>0 then round(((sum(revenue)-sum(cogs))/sum(revenue))*100,2) else null end gross_margin_pct
  from e;"

echo
echo "READ_ONLY=PASS"
