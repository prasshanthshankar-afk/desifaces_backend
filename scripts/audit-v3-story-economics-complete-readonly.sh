#!/usr/bin/env bash
set -Eeuo pipefail

DB="${DF_DB_CONTAINER:-}"
if [[ -z "$DB" ]]; then
  for candidate in desifaces-v3-db df-v3-db; do
    if docker inspect "$candidate" >/dev/null 2>&1; then DB="$candidate"; break; fi
  done
fi
[[ -n "$DB" ]] || { echo "FAIL: postgres container not found"; exit 1; }
PGUSER="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_USER"{print $2; exit}')"
PGDB="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_DB"{print $2; exit}')"
PGUSER="${PGUSER:-postgres}"; PGDB="${PGDB:-postgres}"
PSQL=(docker exec -i "$DB" psql -X -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB")

IFS='|' read -r WF STORY USER CREATED UPDATED < <("${PSQL[@]}" -At -F '|' -c "
select workflow_id,story_id,owner_user_id,created_at,updated_at
from public.v3_studio_workflows
where story_id is not null and state<>'canceled'
order by updated_at desc,created_at desc limit 1;")
[[ -n "${WF:-}" ]] || { echo "FAIL: no Story workflow"; exit 1; }

echo "============================================================"
echo " desifaces V3 — COMPLETE STORY ECONOMICS AUDIT (READ ONLY)"
echo "============================================================"
echo "workflow_id=$WF"
echo "story_id=$STORY"
echo "window=$CREATED .. $UPDATED"

SQL_COMMON="
with ids(id) as (
 values ('$WF'::text),('$STORY'::text)
 union select stage_run_id::text from public.v3_studio_stage_runs where workflow_id='$WF'::uuid
 union select generation_job_id::text from public.v3_studio_stage_runs where workflow_id='$WF'::uuid and generation_job_id is not null
 union select generation_request_id::text from public.v3_studio_stage_runs where workflow_id='$WF'::uuid and generation_request_id is not null
 union select attempt_id::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WF'::uuid)
 union select provider_job_ref::text from public.v3_studio_stage_attempts where stage_run_id in (select stage_run_id from public.v3_studio_stage_runs where workflow_id='$WF'::uuid) and provider_job_ref is not null
), rr as (
 select r.* from public.pricing_credit_reservations r
 where r.user_id='$USER'::uuid
   and r.status='committed'
   and r.created_at >= '$CREATED'::timestamptz - interval '10 minutes'
   and r.created_at <= '$UPDATED'::timestamptz + interval '10 minutes'
   and exists (select 1 from ids where to_jsonb(r)::text like '%'||ids.id||'%')
), lines as (
 select r.id reservation_id,r.service_name,r.service_action,r.created_at,
        x.line->>'sku_code' leaf_sku,
        coalesce(nullif(x.line->>'qty','')::numeric,1) qty,
        coalesce(nullif(x.line->>'line_money','')::numeric,0) revenue_usd,
        coalesce(nullif(x.line->>'line_credits','')::numeric,0) credits
 from rr r
 cross join lateral jsonb_array_elements(coalesce(r.quote_json->'lines','[]'::jsonb)) x(line)
), costed as (
 select l.*,
        c.unit_cogs,
        case when c.unit_cogs is null then l.leaf_sku else null end missing_cost_sku,
        case when c.unit_cogs is null then null else l.qty*c.unit_cogs end cogs_usd
 from lines l
 left join lateral (
   select sum(
      pc.variable_cost_money +
      case when pc.assumed_monthly_units>0 then pc.fixed_monthly_cost_money/pc.assumed_monthly_units else 0 end
   ) unit_cogs
   from public.pricing_sku_costs pc
   where pc.sku_code=l.leaf_sku
     and pc.effective_from<=l.created_at
     and (pc.effective_to is null or pc.effective_to>l.created_at)
     and (pc.is_active=true or pc.effective_to is not null)
 ) c on true
)"

echo
echo "===== 1. COST COMPLETENESS ====="
"${PSQL[@]}" -P pager=off -c "$SQL_COMMON
select leaf_sku,count(*) operations,
       sum(credits) credits,
       sum(revenue_usd) revenue_usd,
       min(unit_cogs) unit_cogs_usd,
       bool_and(unit_cogs is not null) cost_complete
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
echo "READ_ONLY=PASS"
