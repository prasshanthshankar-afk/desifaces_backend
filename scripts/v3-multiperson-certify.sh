#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ID="${WORKFLOW_ID:-${1:-}}"
if [ -z "$WORKFLOW_ID" ]; then
  echo "Usage: WORKFLOW_ID=<uuid> $0" >&2
  exit 2
fi

cd "$(git rev-parse --show-toplevel)"

DB_USER="${POSTGRES_USER:-$(docker exec desifaces-v3-db printenv POSTGRES_USER)}"
DB_NAME="${POSTGRES_DB:-$(docker exec desifaces-v3-db printenv POSTGRES_DB)}"

if [ -z "$DB_USER" ] || [ -z "$DB_NAME" ]; then
  echo "ERROR: unable to resolve V3 PostgreSQL credentials from desifaces-v3-db" >&2
  exit 1
fi

echo "===== WORKFLOW ====="
docker exec -i desifaces-v3-db psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -P pager=off -c "
select workflow_id,state,current_stage,final_media_id,updated_at
from public.v3_studio_workflows
where workflow_id='${WORKFLOW_ID}'::uuid;
"

echo "===== STAGE STATES ====="
docker exec -i desifaces-v3-db psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -P pager=off -c "
select stage_type,scope_type,state,count(*)
from public.v3_studio_stage_runs
where workflow_id='${WORKFLOW_ID}'::uuid
group by stage_type,scope_type,state
order by stage_type,scope_type,state;
"

echo "===== ATTEMPTS / PROVIDER JOBS ====="
docker exec -i desifaces-v3-db psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -P pager=off -c "
select s.stage_type,a.attempt_no,a.attempt_kind,a.state,a.provider_service,
       a.provider_job_ref,a.pricing_quote_id,a.media_id,a.error_code
from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid
order by s.created_at,a.attempt_no;
"

echo "===== FACE/AUDIO OWNER-SERVICE PRICING ====="
docker exec -i desifaces-v3-db psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -P pager=off -c "
select j.studio_type,j.id,j.status,
       coalesce(j.payload_json->'pricing'->>'state',j.meta_json->'pricing'->>'state') as pricing_state,
       coalesce(j.payload_json->'pricing'->>'quote_id',j.meta_json->'pricing'->>'quote_id') as quote_id,
       coalesce(j.payload_json->'pricing'->>'reservation_id',j.meta_json->'pricing'->>'reservation_id') as reservation_id
from public.studio_jobs j
join public.v3_studio_stage_attempts a on a.provider_job_ref=j.id::text
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid
order by j.created_at;
"

echo "===== FUSION CHILD PRICING ====="
docker exec -i desifaces-v3-db psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -P pager=off -c "
with child as (
  select (x->>'fusion_job_id')::uuid as job_id
  from public.v3_studio_stage_attempts a
  join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
  cross join lateral jsonb_array_elements(coalesce(a.metadata_json->'children','[]'::jsonb)) x
  where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='fusion'
)
select j.id,j.status,
       coalesce(j.payload_json->'pricing'->>'state',j.meta_json->'pricing'->>'state') as pricing_state,
       coalesce(j.payload_json->'pricing'->>'quote_id',j.meta_json->'pricing'->>'quote_id') as quote_id,
       coalesce(j.payload_json->'pricing'->>'reservation_id',j.meta_json->'pricing'->>'reservation_id') as reservation_id
from child c join public.studio_jobs j on j.id=c.job_id
order by j.created_at;
"

echo "===== HITL REVIEW ====="
docker exec -i desifaces-v3-db psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -P pager=off -c "
select s.stage_type,r.decision,count(*)
from public.v3_studio_review_items r
join public.v3_studio_stage_runs s on s.stage_run_id=r.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid
group by s.stage_type,r.decision
order by s.stage_type,r.decision;
"

echo "===== CERTIFICATION COMPLETE ====="
