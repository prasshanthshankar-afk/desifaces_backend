#!/usr/bin/env bash
set -Eeuo pipefail

resolve_container(){
  local preferred="$1" service="$2"
  if docker inspect "$preferred" >/dev/null 2>&1; then printf '%s' "$preferred"; return 0; fi
  docker ps -a --filter "label=com.docker.compose.service=${service}" --format '{{.Names}}' | head -1
}

DB="$(resolve_container "${DB_CONTAINER:-desifaces-v3-db}" desifaces-db)"
EXT_API="$(resolve_container "${EXT_API_CONTAINER:-df-v3-svc-fusion-extension}" svc-fusion-extension)"
EXT_WORKER="$(resolve_container "${EXT_WORKER_CONTAINER:-df-v3-svc-fusion-extension-worker}" svc-fusion-extension-worker)"
STITCH_WORKER="$(resolve_container "${STITCH_WORKER_CONTAINER:-df-v3-svc-fusion-extension-stitch-worker}" svc-fusion-extension-stitch-worker)"

[ -n "$DB" ] || { echo "FAIL: database container not found" >&2; exit 2; }

PSQL=(docker exec "$DB" psql -U desifaces_admin -d desifaces -v ON_ERROR_STOP=1 -P pager=off)
LATEST="$(${PSQL[@]} -Atc "select id::text from public.longform_jobs order by created_at desc limit 1")"
[ -n "$LATEST" ] || { echo "NO_LONGFORM_JOBS"; exit 0; }
USER_ID="$(${PSQL[@]} -Atc "select user_id::text from public.longform_jobs where id='${LATEST}'::uuid")"

printf '============================================================\n'
printf ' desifaces V3 — LATEST LONGFORM + PRICING STATE\n'
printf '============================================================\n'
printf 'job_id=%s\nuser_id=%s\n' "$LATEST" "$USER_ID"

printf '\n===== RECENT LONGFORM PARENTS =====\n'
${PSQL[@]} -Atc "
select concat_ws('|', id::text,status,coalesce(error_code,''),left(coalesce(error_message,''),180),total_segments,completed_segments,to_char(created_at at time zone 'UTC','HH24:MI:SS'),to_char(updated_at at time zone 'UTC','HH24:MI:SS'),case when final_storage_path is not null then 'final=yes' else 'final=no' end)
from public.longform_jobs
order by created_at desc
limit 5;"

printf '\n===== LATEST PARENT =====\n'
${PSQL[@]} -x -c "
select id,user_id,status,error_code,error_message,segment_seconds,max_segment_seconds,total_segments,completed_segments,
       created_at,updated_at,
       (final_storage_path is not null) as has_final_storage,
       (final_video_url is not null and final_video_url <> '') as has_final_video,
       coalesce(tags->>'requested_duration_sec',tags->>'duration_sec') as requested_duration_sec,
       tags->>'quality_tier' as quality_tier,
       tags->>'provider_hint' as provider_hint
from public.longform_jobs where id='${LATEST}'::uuid;"

printf '\n===== SEGMENTS =====\n'
${PSQL[@]} -Atc "
select concat_ws('|',segment_index,status,duration_sec,coalesce(fusion_job_id::text,''),coalesce(error_code,''),left(coalesce(error_message,''),160))
from public.longform_segments where job_id='${LATEST}'::uuid order by segment_index;"

printf '\n===== PRICING RESERVATION =====\n'
${PSQL[@]} -x -c "
select r.id,r.status,r.service_name,r.service_action,r.sku_code,r.reserved_credits,r.estimated_money,r.currency,r.settlement_mode,
       to_jsonb(r)->>'final_charged_credits' as final_charged_credits,
       to_jsonb(r)->>'final_charged_money' as final_charged_money,
       r.quote_json->>'quote_id' as quote_id,
       r.quote_json->>'variant_code' as variant_code,
       r.quote_json->>'estimated_units' as estimated_units,
       r.quote_json #>> '{meta,requested_duration_sec}' as pricing_requested_duration_sec,
       r.quote_json #>> '{meta,segment_count}' as pricing_segment_count,
       r.quote_json #>> '{meta,segment_durations_sec}' as pricing_segment_durations_sec,
       r.created_at,r.updated_at
from public.pricing_credit_reservations r
where r.job_ref='${LATEST}'
order by r.created_at desc limit 3;"

printf '\n===== LEDGER FOR PARENT =====\n'
${PSQL[@]} -Atc "
select concat_ws('|',event_type,credits_delta,coalesce(sku_code,''),coalesce(service_name,''),coalesce(service_action,''),to_char(created_at at time zone 'UTC','HH24:MI:SS'))
from public.pricing_credit_ledger_events
where reservation_id in (select id from public.pricing_credit_reservations where job_ref='${LATEST}')
order by created_at;"

printf '\n===== ACCOUNT BALANCE STATE =====\n'
${PSQL[@]} -x -c "
select to_jsonb(v)->'lots_json' as lots_json,
       to_jsonb(v)->'legacy_account_json' as legacy_account_json
from public.v_pricing_account_overview v
where v.user_id='${USER_ID}'::uuid
limit 1;" || true

printf '\n===== EXTENSION LOGS FOR PARENT (BOUNDED) =====\n'
for c in "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER"; do
  [ -n "$c" ] || continue
  printf -- '--- %s ---\n' "$c"
  docker logs "$c" --since 90m 2>&1 \
    | grep -F "$LATEST" \
    | tail -n 80 \
    | sed -E 's#https?://[^ ]+#<url-redacted>#g' || true
done

printf '\n===== CLASSIFICATION =====\n'
PARENT_STATUS="$(${PSQL[@]} -Atc "select status from public.longform_jobs where id='${LATEST}'::uuid")"
RES_STATUS="$(${PSQL[@]} -Atc "select status from public.pricing_credit_reservations where job_ref='${LATEST}' order by created_at desc limit 1")"
RES_CREDITS="$(${PSQL[@]} -Atc "select coalesce(reserved_credits,0)::text from public.pricing_credit_reservations where job_ref='${LATEST}' order by created_at desc limit 1")"
printf 'parent_status=%s\nreservation_status=%s\nreserved_credits=%s\n' "$PARENT_STATUS" "$RES_STATUS" "$RES_CREDITS"
case "${PARENT_STATUS}:${RES_STATUS}" in
  failed:reserved|error:reserved|canceled:reserved|cancelled:reserved)
    echo 'CLASSIFICATION=FAILED_PARENT_WITH_LEAKED_RESERVATION'
    ;;
  queued:reserved|running:reserved|stitching:reserved|processing:reserved)
    echo 'CLASSIFICATION=ACTIVE_PARENT_WITH_VALID_HOLD'
    ;;
  succeeded:committed|completed:committed)
    echo 'CLASSIFICATION=COMPLETED_AND_COMMITTED'
    ;;
  failed:released|error:released|canceled:released|cancelled:released)
    echo 'CLASSIFICATION=FAILED_AND_RELEASED_CORRECTLY'
    ;;
  *)
    echo 'CLASSIFICATION=REVIEW_REQUIRED'
    ;;
esac

printf '\nREAD_ONLY=YES\n'
printf '============================================================\n'
