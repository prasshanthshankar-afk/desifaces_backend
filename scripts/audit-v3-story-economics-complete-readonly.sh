#!/usr/bin/env bash
set -Eeuo pipefail

DB="${DF_DB_CONTAINER:-}"
if [[ -z "$DB" ]]; then
  for candidate in desifaces-v3-db df-v3-db desifaces-db; do
    if docker inspect "$candidate" >/dev/null 2>&1; then DB="$candidate"; break; fi
  done
fi
[[ -n "$DB" ]] || { echo "FAIL: postgres container not found"; exit 1; }
PGUSER="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_USER"{print $2; exit}')"
PGDB="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_DB"{print $2; exit}')"
PGUSER="${PGUSER:-postgres}"; PGDB="${PGDB:-postgres}"
PSQL=(docker exec -i "$DB" psql -X -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB")

for required in v3_studio_workflows v3_studio_stage_runs v3_studio_stage_attempts pricing_credit_reservations pricing_sku_costs; do
  EXISTS="$("${PSQL[@]}" -At -c "select case when to_regclass('public.${required}') is not null then 1 else 0 end;")"
  [[ "$EXISTS" == "1" ]] || { echo "FAIL: required table public.$required missing"; exit 1; }
done

IFS='|' read -r WF STORY USER CREATED UPDATED < <("${PSQL[@]}" -At -F '|' -c "
select workflow_id,story_id,owner_user_id,created_at,updated_at
from public.v3_studio_workflows
where story_id is not null and lower(coalesce(state::text,''))<>'canceled'
order by updated_at desc,created_at desc limit 1;")
[[ -n "${WF:-}" ]] || { echo "FAIL: no Story workflow"; exit 1; }

echo "============================================================"
echo " desifaces V3 — COMPLETE STORY ECONOMICS AUDIT (READ ONLY)"
echo "============================================================"
echo "workflow_id=$WF"
echo "story_id=$STORY"
echo "owner_user_id=$USER"
echo "window=$CREATED .. $UPDATED"
echo "cost_basis=CURRENT_ACTIVE_COGS_RECALCULATION"

# Exact identifier set reused from the already-proven 9-reservation pricing
# reconciliation. Do not downgrade correlation to workflow/story/stage only.
SQL_COMMON="
with ids(id) as (
 values ('$WF'::text),('$STORY'::text)
 union select stage_run_id::text from public.v3_studio_stage_runs where workflow_id='$WF'::uuid
 union select generation_job_id::text from public.v3_studio_stage_runs where workflow_id='$WF'::uuid and generation_job_id is not null
 union select generation_request_id::text from public.v3_studio_stage_runs where workflow_id='$WF'::uuid and generation_request_id is not null
 union select attempt_id::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WF'::uuid)
 union select provider_job_ref::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WF'::uuid) and provider_job_ref is not null
), expected as (
 select count(*)::int expected_billable_attempts
 from public.v3_studio_stage_attempts a
 join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
 where s.workflow_id='$WF'::uuid
   and lower(coalesce(a.state::text,''))='succeeded'
   and a.pricing_quote_id is not null
), rr as (
 select r.*
 from public.pricing_credit_reservations r
 where r.user_id='$USER'::uuid
   and r.created_at >= '$CREATED'::timestamptz - interval '10 minutes'
   and r.created_at <= '$UPDATED'::timestamptz + interval '10 minutes'
   and exists (select 1 from ids where coalesce(to_jsonb(r)::text,'') like '%'||ids.id||'%')
), committed as (
 select * from rr where lower(coalesce(status,'')) in ('committed','finalized','charged','completed','invoiced')
), lines as (
 select r.id reservation_id,
        coalesce(to_jsonb(r)->>'service_name','') service_name,
        coalesce(to_jsonb(r)->>'service_action','') service_action,
        r.created_at,
        x.line->>'sku_code' leaf_sku,
        coalesce(nullif(x.line->>'qty','')::numeric,1) qty,
        coalesce(nullif(x.line->>'line_money','')::numeric,0) revenue_usd,
        coalesce(nullif(x.line->>'line_credits','')::numeric,0) credits
 from committed r
 cross join lateral jsonb_array_elements(coalesce(r.quote_json->'lines','[]'::jsonb)) x(line)
), costed as (
 select l.*,
        c.unit_cogs,
        case when nullif(l.leaf_sku,'') is null then '<missing-sku>'
             when c.unit_cogs is null or c.unit_cogs<=0 then l.leaf_sku
             else null end missing_cost_sku,
        case when c.unit_cogs is null or c.unit_cogs<=0 then null else l.qty*c.unit_cogs end cogs_usd
 from lines l
 left join lateral (
   select sum(
     pc.variable_cost_money +
     case when pc.assumed_monthly_units>0 then pc.fixed_monthly_cost_money/pc.assumed_monthly_units else 0 end
   ) unit_cogs
   from public.pricing_sku_costs pc
   where pc.sku_code=l.leaf_sku
     and pc.cost_currency='USD'
     and pc.is_active=true
     and pc.effective_from<=now()
     and (pc.effective_to is null or pc.effective_to>now())
 ) c on true
)"

echo
echo "===== 0. RESERVATION CORRELATION ====="
"${PSQL[@]}" -P pager=off -c "$SQL_COMMON
select
 (select expected_billable_attempts from expected) expected_billable_attempts,
 (select count(*) from rr) matched_reservations,
 (select count(*) from committed) committed_reservations,
 (select count(distinct reservation_id) from lines) reservations_with_lines,
 (select coalesce(sum(coalesce((quote_json->>'final_charged_credits')::numeric,0)),0) from committed) committed_credits;"

IFS='|' read -r EXPECTED MATCHED COMMITTED WITH_LINES CREDITS < <("${PSQL[@]}" -At -F '|' -c "$SQL_COMMON
select
 (select expected_billable_attempts from expected),
 (select count(*) from rr),
 (select count(*) from committed),
 (select count(distinct reservation_id) from lines),
 (select coalesce(sum(coalesce((quote_json->>'final_charged_credits')::numeric,0)),0) from committed);")

if [[ "$EXPECTED" == "0" || "$MATCHED" != "$EXPECTED" || "$COMMITTED" != "$EXPECTED" || "$WITH_LINES" != "$EXPECTED" ]]; then
  echo "ECONOMICS_STATUS=CORRELATION_INCOMPLETE"
  echo "expected_billable_attempts=$EXPECTED"
  echo "matched_reservations=$MATCHED"
  echo "committed_reservations=$COMMITTED"
  echo "reservations_with_lines=$WITH_LINES"
  echo "gross_margin_pct=UNAVAILABLE"
  exit 3
fi

echo "STORY_RESERVATION_CORRELATION=PASS reservations=$COMMITTED credits=$CREDITS"

echo
echo "===== 1. COST COMPLETENESS ====="
"${PSQL[@]}" -P pager=off -c "$SQL_COMMON
select leaf_sku,count(*) line_items,
       sum(credits) credits,
       sum(revenue_usd) revenue_usd,
       min(unit_cogs) unit_cogs_usd,
       bool_and(unit_cogs is not null and unit_cogs>0) cost_complete
from costed group by leaf_sku order by leaf_sku;"

MISSING="$("${PSQL[@]}" -At -c "$SQL_COMMON
select coalesce(string_agg(distinct missing_cost_sku,',' order by missing_cost_sku),'')
from costed where missing_cost_sku is not null;")"
if [[ -n "$MISSING" ]]; then
  echo "ECONOMICS_STATUS=COST_INCOMPLETE"
  echo "missing_cost_skus=$MISSING"
  echo "gross_margin_pct=UNAVAILABLE"
  exit 2
fi

echo "ECONOMICS_COST_COMPLETENESS=PASS"

echo
echo "===== 2. ECONOMICS BY SERVICE ====="
"${PSQL[@]}" -P pager=off -c "$SQL_COMMON
select service_name,service_action,
       count(distinct reservation_id) operations,
       sum(credits) credits,
       round(sum(revenue_usd),4) revenue_usd,
       round(sum(cogs_usd),4) cogs_usd,
       round(sum(revenue_usd)-sum(cogs_usd),4) gross_profit_usd,
       round(case when sum(revenue_usd)>0 then 100*(sum(revenue_usd)-sum(cogs_usd))/sum(revenue_usd) end,2) gross_margin_pct
from costed group by service_name,service_action order by service_name,service_action;"

echo
echo "===== 3. OVERALL WORKFLOW ECONOMICS ====="
"${PSQL[@]}" -P pager=off -c "$SQL_COMMON
select count(distinct reservation_id) operations,
       sum(credits) total_credits,
       round(sum(revenue_usd),4) revenue_usd,
       round(sum(cogs_usd),4) cogs_usd,
       round(sum(revenue_usd)-sum(cogs_usd),4) gross_profit_usd,
       round(case when sum(revenue_usd)>0 then 100*(sum(revenue_usd)-sum(cogs_usd))/sum(revenue_usd) end,2) gross_margin_pct
from costed;"

echo "ECONOMICS_STATUS=COMPLETE"
echo "MISSING_COST_SKUS=0"
echo "CORRELATED_RESERVATIONS=$COMMITTED"
echo "CORRELATED_CREDITS=$CREDITS"
echo "READ_ONLY=PASS"
