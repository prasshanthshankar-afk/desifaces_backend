#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT="${COMPOSE_PROJECT:-desifaces-v3}"
DB_CONTAINER="${DB_CONTAINER:-}"

find_by_service(){
  local service="$1"
  docker ps -a \
    --filter "label=com.docker.compose.project=${PROJECT}" \
    --filter "label=com.docker.compose.service=${service}" \
    --format '{{.Names}}' | head -1
}

if [ -z "$DB_CONTAINER" ]; then
  for c in desifaces-v3-db df-v3-db; do
    if docker inspect "$c" >/dev/null 2>&1; then DB_CONTAINER="$c"; break; fi
  done
fi
if [ -z "$DB_CONTAINER" ]; then
  DB_CONTAINER="$(find_by_service desifaces-db)"
fi
[ -n "$DB_CONTAINER" ] || { echo "FAIL: V3 database container not found" >&2; exit 2; }

DB_USER="$(docker exec "$DB_CONTAINER" sh -lc 'printf %s "${POSTGRES_USER:-desifaces_v3_admin}"')"
DB_NAME="$(docker exec "$DB_CONTAINER" sh -lc 'printf %s "${POSTGRES_DB:-desifaces_v3}"')"

sql(){
  docker exec "$DB_CONTAINER" psql -X -q -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -At -F '|' -c "$1"
}

redact(){
  sed -E \
    -e 's/(sig=)[^& ]+/\1<redacted>/g' \
    -e 's/(se=)[^& ]+/\1<redacted>/g' \
    -e 's/(sp=)[^& ]+/\1<redacted>/g' \
    -e 's/(sv=)[^& ]+/\1<redacted>/g' \
    -e 's/(token=)[^& ]+/\1<redacted>/g'
}

LATEST="$(sql "select id::text,studio_type,status,to_char(created_at at time zone 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') from public.studio_jobs where studio_type in ('face','fusion') order by created_at desc limit 1;")"
[ -n "$LATEST" ] || { echo "No recent Face/Fusion jobs found"; exit 0; }
IFS='|' read -r JOB STUDIO STATUS CREATED <<< "$LATEST"

printf '%s\n' "============================================================"
printf '%s\n' " desifaces V3 — LATEST AI JOB EVIDENCE"
printf '%s\n' "============================================================"
printf 'job_id=%s\nstudio=%s\nstatus=%s\ncreated_utc=%s\n' "$JOB" "$STUDIO" "$STATUS" "$CREATED"

echo
echo "===== RECENT FACE/FUSION JOBS ====="
sql "select id::text,studio_type,status,to_char(created_at at time zone 'UTC','HH24:MI:SS'),to_char(updated_at at time zone 'UTC','HH24:MI:SS'),coalesce(error_code,''),left(coalesce(error_message,''),120) from public.studio_jobs where studio_type in ('face','fusion') order by created_at desc limit 6;" | sed 's/^/  /'

echo
echo "===== LATEST JOB CONTRACT ====="
sql "select 'provider='||coalesce(payload_json->>'provider',''), 'quality_tier='||coalesce(payload_json->>'quality_tier',''), 'provider_resolution='||coalesce(payload_json->'provider_options'->>'resolution',''), 'turbo_mode='||coalesce(payload_json->'provider_options'->>'turbo_mode',''), 'video_resolution='||coalesce(payload_json->'video'->>'resolution',''), 'duration_sec='||coalesce(payload_json->'video'->>'duration_sec',''), 'variants='||coalesce(payload_json->>'num_variants',''), 'pricing_state='||coalesce(payload_json->'pricing'->>'state',''), 'pricing_tier='||coalesce(payload_json->'pricing'->>'tier_code',''), 'reservation_id='||coalesce(payload_json->'pricing'->>'reservation_id',''), 'provider_status='||coalesce(meta_json->'light_status'->>'provider_status',''), 'progress_pct='||coalesce(meta_json->'light_status'->>'progress_pct','') from public.studio_jobs where id='${JOB}'::uuid;" | tr '|' '\n' | sed 's/^/  /'

echo
echo "===== JOB STEPS ====="
if sql "select 1 from information_schema.tables where table_schema='public' and table_name='studio_job_steps';" | grep -q 1; then
  sql "select step_code,status,attempt,coalesce(error_code,''),left(coalesce(error_message,''),140),to_char(updated_at at time zone 'UTC','HH24:MI:SS') from public.studio_job_steps where job_id='${JOB}'::uuid order by created_at;" | sed 's/^/  /'
else
  echo "  studio_job_steps table unavailable"
fi

echo
echo "===== PROVIDER RUN ====="
if sql "select 1 from information_schema.tables where table_schema='public' and table_name='provider_runs';" | grep -q 1; then
  sql "select provider,provider_status,coalesce(provider_job_id,''),to_char(created_at at time zone 'UTC','HH24:MI:SS'),to_char(updated_at at time zone 'UTC','HH24:MI:SS') from public.provider_runs where job_id='${JOB}'::uuid order by created_at desc limit 3;" | sed 's/^/  /'
else
  echo "  provider_runs table unavailable"
fi

echo
echo "===== ARTIFACTS (URLS OMITTED) ====="
if sql "select 1 from information_schema.tables where table_schema='public' and table_name='artifacts';" | grep -q 1; then
  sql "select kind,coalesce(content_type,''),coalesce(bytes::text,''),to_char(created_at at time zone 'UTC','HH24:MI:SS') from public.artifacts where job_id='${JOB}'::uuid order by created_at;" | sed 's/^/  /'
else
  echo "  artifacts table unavailable"
fi

API="$(find_by_service "svc-${STUDIO}")"
WORKER="$(find_by_service "svc-${STUDIO}-worker")"
PRICING="$(find_by_service svc-pricing)"

echo
echo "===== MATCHING WORKER LOGS (LAST 120) ====="
if [ -n "$WORKER" ]; then
  echo "worker=$WORKER"
  docker logs --since "$CREATED" "$WORKER" 2>&1 | grep -F "$JOB" | tail -n 120 | redact || true
else
  echo "worker not found"
fi

echo
echo "===== MATCHING API LOGS (LAST 60) ====="
if [ -n "$API" ]; then
  echo "api=$API"
  docker logs --since "$CREATED" "$API" 2>&1 | grep -F "$JOB" | tail -n 60 | redact || true
else
  echo "api not found"
fi

echo
echo "===== MATCHING PRICING LOGS (LAST 40) ====="
if [ -n "$PRICING" ]; then
  echo "pricing=$PRICING"
  docker logs --since "$CREATED" "$PRICING" 2>&1 | grep -F "$JOB" | tail -n 40 | redact || true
else
  echo "pricing container not found"
fi

echo
echo "============================================================"
echo " READ-ONLY COLLECTION COMPLETE"
echo "============================================================"
