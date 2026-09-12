#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || { echo "FAIL: wrong host"; exit 1; }

DB=""
for candidate in desifaces-v3-db df-v3-db; do
  if docker inspect "$candidate" >/dev/null 2>&1; then DB="$candidate"; break; fi
done
if [[ -z "$DB" ]]; then
  while read -r name image; do
    case "${name} ${image}" in
      *postgres*|*Postgres*|*desifaces*db*) DB="$name"; break ;;
    esac
  done < <(docker ps --format '{{.Names}} {{.Image}}')
fi
[[ -n "$DB" ]] || { echo "FAIL: postgres container not found"; exit 1; }

PGUSER="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_USER"{print $2; exit}')"
PGDB="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_DB"{print $2; exit}')"
PGUSER="${PGUSER:-postgres}"
PGDB="${PGDB:-postgres}"
PSQL=(docker exec -i "$DB" psql -X -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB")

read -r WORKFLOW_ID STORY_ID OWNER_USER_ID ACCOUNT_ID WF_STATE CURRENT_STAGE WF_CREATED WF_UPDATED < <(
  "${PSQL[@]}" -At -F '|' -c "
    select workflow_id,story_id,owner_user_id,account_id,state,current_stage,
           created_at,updated_at
      from public.v3_studio_workflows
     where story_id is not null
       and state <> 'canceled'
     order by updated_at desc,created_at desc
     limit 1;" | tr '|' ' '
)

[[ -n "${WORKFLOW_ID:-}" ]] || { echo "FAIL: no recent Story workflow found"; exit 1; }

echo "============================================================"
echo " desifaces V3 — STORY PRICING + ARTIFACT READ-ONLY AUDIT"
echo "============================================================"
echo "mode=READ_ONLY"
echo "db_container=$DB"
echo "workflow_id=$WORKFLOW_ID"
echo "story_id=$STORY_ID"
echo "owner_user_id=$OWNER_USER_ID"
echo "account_id=$ACCOUNT_ID"
echo "workflow_state=$WF_STATE"
echo "current_stage=$CURRENT_STAGE"
echo "created_at=$WF_CREATED"
echo "updated_at=$WF_UPDATED"

echo
echo "===== 1. STAGES / CHARACTER STATE ====="
"${PSQL[@]}" -P pager=off -c "
select s.stage_run_id,
       s.stage_type,
       s.state,
       coalesce(p.display_name,'') as participant,
       s.participant_id,
       s.generation_job_id,
       s.generation_request_id,
       s.updated_at
  from public.v3_studio_stage_runs s
  left join public.v3_participants p on p.participant_id=s.participant_id
 where s.workflow_id='$WORKFLOW_ID'::uuid
 order by s.created_at,s.stage_run_id;"

echo
echo "===== 2. FACE OUTPUTS / ARTIFACTS ====="
"${PSQL[@]}" -P pager=off -c "
select s.stage_run_id,
       coalesce(p.display_name,'') as participant,
       s.state as face_state,
       o.media_id,
       o.is_active,
       jsonb_pretty(to_jsonb(o)) as output_row
  from public.v3_studio_stage_runs s
  left join public.v3_participants p on p.participant_id=s.participant_id
  left join public.v3_studio_stage_outputs o
    on o.stage_run_id=s.stage_run_id
 where s.workflow_id='$WORKFLOW_ID'::uuid
   and s.stage_type='face'
 order by s.created_at,o.created_at;"

echo
echo "===== 3. FACE REVIEWS ====="
"${PSQL[@]}" -P pager=off -c "
select s.stage_run_id,
       coalesce(p.display_name,'') as participant,
       s.state as face_state,
       r.review_item_id,
       r.media_id,
       r.decision,
       r.created_at,
       r.updated_at
  from public.v3_studio_stage_runs s
  left join public.v3_participants p on p.participant_id=s.participant_id
  left join public.v3_studio_review_items r on r.stage_run_id=s.stage_run_id
 where s.workflow_id='$WORKFLOW_ID'::uuid
   and s.stage_type='face'
 order by s.created_at,r.created_at;"

echo
echo "===== 4. PARTICIPANT PRIMARY FACE LOCK ====="
"${PSQL[@]}" -P pager=off -c "
select p.participant_id,p.display_name,p.primary_face_media_id,p.updated_at
  from public.v3_participants p
 where p.story_id='$STORY_ID'::uuid
 order by p.created_at,p.participant_id;" 2>/dev/null || \
"${PSQL[@]}" -P pager=off -c "
select distinct p.participant_id,p.display_name,p.primary_face_media_id,p.updated_at
  from public.v3_participants p
  join public.v3_studio_stage_runs s on s.participant_id=p.participant_id
 where s.workflow_id='$WORKFLOW_ID'::uuid
 order by p.display_name,p.participant_id;"

echo
echo "===== 5. ARTIFACT VISIBILITY GATE ====="
"${PSQL[@]}" -At -c "
with face as (
  select s.stage_run_id,s.state,
         count(o.*) filter (where o.is_active=true) as active_outputs,
         bool_or(
           coalesce(to_jsonb(o)->>'read_url','') <> '' or
           coalesce(to_jsonb(o)->>'image_url','') <> '' or
           coalesce(to_jsonb(o)->>'preview_url','') <> '' or
           coalesce(to_jsonb(o)->>'url','') <> ''
         ) filter (where o.is_active=true) as output_has_direct_url
    from public.v3_studio_stage_runs s
    left join public.v3_studio_stage_outputs o on o.stage_run_id=s.stage_run_id
   where s.workflow_id='$WORKFLOW_ID'::uuid and s.stage_type='face'
   group by s.stage_run_id,s.state
)
select 'face_stage='||stage_run_id||
       ' state='||state||
       ' active_outputs='||active_outputs||
       ' direct_url='||coalesce(output_has_direct_url,false)
  from face
 order by stage_run_id;"

MISSING_OUTPUTS="$("${PSQL[@]}" -At -c "
select count(*)
  from public.v3_studio_stage_runs s
 where s.workflow_id='$WORKFLOW_ID'::uuid
   and s.stage_type='face'
   and s.state in ('awaiting_review','approved')
   and not exists (
     select 1 from public.v3_studio_stage_outputs o
      where o.stage_run_id=s.stage_run_id and o.is_active=true
   );")"

if [[ "$MISSING_OUTPUTS" == "0" ]]; then
  echo "FACE_DURABLE_OUTPUT_GATE=PASS"
else
  echo "FACE_DURABLE_OUTPUT_GATE=FAIL missing=$MISSING_OUTPUTS"
fi

echo
echo "===== 6. PRICING LEDGER EVENTS CORRELATED TO THIS WORKFLOW ====="
"${PSQL[@]}" -P pager=off -c "
with stage_jobs as (
  select generation_job_id::text as job_id
    from public.v3_studio_stage_runs
   where workflow_id='$WORKFLOW_ID'::uuid
     and generation_job_id is not null
), relevant as (
  select le.*
    from public.pricing_credit_ledger_events le
   where le.user_id='$OWNER_USER_ID'::uuid
     and le.created_at >= '$WF_CREATED'::timestamptz - interval '5 minutes'
     and (
       coalesce(to_jsonb(le)->>'studio_job_id','') in (select job_id from stage_jobs)
       or coalesce(le.metadata_json::text,'') like '%'||'$WORKFLOW_ID'||'%'
       or coalesce(le.metadata_json::text,'') like '%'||'$STORY_ID'||'%'
     )
)
select to_jsonb(relevant)-'metadata_json' as ledger,
       relevant.metadata_json
  from relevant
 order by relevant.created_at;"

echo
echo "===== 7. PRICING TOTALS / DUPLICATE-CONSUME GATE ====="
"${PSQL[@]}" -P pager=off -c "
with stage_jobs as (
  select generation_job_id::text as job_id
    from public.v3_studio_stage_runs
   where workflow_id='$WORKFLOW_ID'::uuid and generation_job_id is not null
), relevant as (
  select le.*,
         coalesce(to_jsonb(le)->>'studio_job_id','') as job_key,
         coalesce(to_jsonb(le)->>'idempotency_key','') as idem_key
    from public.pricing_credit_ledger_events le
   where le.user_id='$OWNER_USER_ID'::uuid
     and le.created_at >= '$WF_CREATED'::timestamptz - interval '5 minutes'
     and (
       coalesce(to_jsonb(le)->>'studio_job_id','') in (select job_id from stage_jobs)
       or coalesce(le.metadata_json::text,'') like '%'||'$WORKFLOW_ID'||'%'
       or coalesce(le.metadata_json::text,'') like '%'||'$STORY_ID'||'%'
     )
)
select
  coalesce(sum(abs(credits_delta)) filter (where event_type='consume' and credits_delta<0),0) as credits_consumed,
  coalesce(sum(credits_delta) filter (where credits_delta>0 and (event_type ilike '%refund%' or event_type ilike '%reversal%')),0) as credits_refunded,
  count(*) filter (where event_type='consume' and credits_delta<0) as consume_events
from relevant;

with stage_jobs as (
  select generation_job_id::text as job_id
    from public.v3_studio_stage_runs
   where workflow_id='$WORKFLOW_ID'::uuid and generation_job_id is not null
), relevant as (
  select le.*,
         coalesce(to_jsonb(le)->>'studio_job_id','') as job_key,
         coalesce(to_jsonb(le)->>'idempotency_key','') as idem_key
    from public.pricing_credit_ledger_events le
   where le.user_id='$OWNER_USER_ID'::uuid
     and le.created_at >= '$WF_CREATED'::timestamptz - interval '5 minutes'
     and (
       coalesce(to_jsonb(le)->>'studio_job_id','') in (select job_id from stage_jobs)
       or coalesce(le.metadata_json::text,'') like '%'||'$WORKFLOW_ID'||'%'
       or coalesce(le.metadata_json::text,'') like '%'||'$STORY_ID'||'%'
     )
)
select coalesce(nullif(idem_key,''),nullif(job_key,''),sku_code||':'||created_at::text) as charge_key,
       sku_code,
       count(*) as consume_count,
       sum(abs(credits_delta)) as consumed_credits
  from relevant
 where event_type='consume' and credits_delta<0
 group by 1,2
having count(*) > 1
 order by consume_count desc;"

DUPES="$("${PSQL[@]}" -At -c "
with stage_jobs as (
  select generation_job_id::text as job_id
    from public.v3_studio_stage_runs
   where workflow_id='$WORKFLOW_ID'::uuid and generation_job_id is not null
), relevant as (
  select le.*,
         coalesce(to_jsonb(le)->>'studio_job_id','') as job_key,
         coalesce(to_jsonb(le)->>'idempotency_key','') as idem_key
    from public.pricing_credit_ledger_events le
   where le.user_id='$OWNER_USER_ID'::uuid
     and le.created_at >= '$WF_CREATED'::timestamptz - interval '5 minutes'
     and (
       coalesce(to_jsonb(le)->>'studio_job_id','') in (select job_id from stage_jobs)
       or coalesce(le.metadata_json::text,'') like '%'||'$WORKFLOW_ID'||'%'
       or coalesce(le.metadata_json::text,'') like '%'||'$STORY_ID'||'%'
     )
), grouped as (
  select coalesce(nullif(idem_key,''),nullif(job_key,''),sku_code||':'||created_at::text) charge_key,
         sku_code,count(*) n
    from relevant
   where event_type='consume' and credits_delta<0
   group by 1,2
  having count(*) > 1
)
select count(*) from grouped;")"

if [[ "$DUPES" == "0" ]]; then
  echo "PRICING_DUPLICATE_CONSUME_GATE=PASS"
else
  echo "PRICING_DUPLICATE_CONSUME_GATE=FAIL groups=$DUPES"
fi

echo
echo "===== 8. RESERVATIONS CREATED DURING THIS WORKFLOW ====="
"${PSQL[@]}" -P pager=off -c "
select jsonb_pretty(to_jsonb(r))
  from public.pricing_credit_reservations r
 where r.user_id='$OWNER_USER_ID'::uuid
   and r.created_at >= '$WF_CREATED'::timestamptz - interval '5 minutes'
   and (
     coalesce(to_jsonb(r)->>'external_ref_id','') in (
       select generation_job_id::text from public.v3_studio_stage_runs
        where workflow_id='$WORKFLOW_ID'::uuid and generation_job_id is not null
     )
     or coalesce(to_jsonb(r)->>'quote_json','') like '%'||'$WORKFLOW_ID'||'%'
     or coalesce(to_jsonb(r)->>'quote_json','') like '%'||'$STORY_ID'||'%'
   )
 order by r.created_at;" 2>/dev/null || echo "RESERVATION_DETAIL_QUERY=UNAVAILABLE_ON_THIS_SCHEMA"

echo
echo "============================================================"
echo " AUDIT VERDICT INPUTS COLLECTED"
echo "============================================================"
echo "READ_ONLY=PASS"
echo "FACE_DURABLE_OUTPUT_MISSING=$MISSING_OUTPUTS"
echo "PRICING_DUPLICATE_GROUPS=$DUPES"
echo "NEXT=RECONCILE_EXPECTED_VS_SETTLED_AND_FIX_WEB_ARTIFACT_PRESENTATION"
