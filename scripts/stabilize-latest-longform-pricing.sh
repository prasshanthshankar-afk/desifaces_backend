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
[ -n "$EXT_API" ] || { echo "FAIL: Fusion Extension API container not found" >&2; exit 2; }

PSQL=(docker exec "$DB" psql -U desifaces_admin -d desifaces -v ON_ERROR_STOP=1 -P pager=off)
LATEST="$(${PSQL[@]} -Atc "select id::text from public.longform_jobs order by created_at desc limit 1")"
[ -n "$LATEST" ] || { echo "NO_LONGFORM_JOBS"; exit 0; }

printf '============================================================\n'
printf ' desifaces V3 — LONGFORM PRICING STABILIZATION\n'
printf '============================================================\n'
printf 'job_id=%s\n' "$LATEST"

printf '\n===== BEFORE =====\n'
${PSQL[@]} -x -c "
select j.id,j.user_id,j.status,j.error_code,left(coalesce(j.error_message,''),500) as error_message,
       j.total_segments,j.completed_segments,
       (j.final_storage_path is not null) as has_final_storage,
       (j.final_video_url is not null and j.final_video_url <> '') as has_final_video,
       r.id as reservation_id,r.status as reservation_status,r.reserved_credits,r.sku_code,r.service_name,r.service_action,
       r.quote_json #>> '{meta,requested_duration_sec}' as pricing_duration_sec,
       r.quote_json #>> '{meta,segment_count}' as pricing_segment_count,
       r.quote_json #>> '{meta,segment_durations_sec}' as pricing_segment_durations_sec
from public.longform_jobs j
left join lateral (
  select * from public.pricing_credit_reservations r0
  where r0.job_ref=j.id::text
  order by r0.created_at desc limit 1
) r on true
where j.id='${LATEST}'::uuid;"

printf '\n===== SAFE RELEASE GATE =====\n'
docker exec -e DF_STABILIZE_JOB_ID="$LATEST" "$EXT_API" python - <<'PY'
import asyncio, os
from app.db import get_db_pool
from app.services.longform_orchestrator import release_longform_pricing_for_job

TERMINAL_FAILED = {'failed','error','canceled','cancelled','blocked'}

async def main():
    job_id = os.environ['DF_STABILIZE_JOB_ID']
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id::text,user_id::text,status,error_code,error_message,
                   final_storage_path,final_video_url,tags
            from public.longform_jobs where id=$1::uuid
            """, job_id,
        )
        if not row:
            print('ACTION=NONE reason=parent_not_found')
            return
        reservation = await conn.fetchrow(
            """
            select id::text,status,reserved_credits
            from public.pricing_credit_reservations
            where job_ref=$1
            order by created_at desc limit 1
            """, job_id,
        )
        parent_status = str(row['status'] or '').lower()
        reservation_status = str((reservation['status'] if reservation else '') or '').lower()
        has_final = bool(row['final_storage_path'] or row['final_video_url'])
        print(f'parent_status={parent_status}')
        print(f'reservation_status={reservation_status or "none"}')
        print(f'has_final={str(has_final).lower()}')
        if parent_status in TERMINAL_FAILED and reservation_status == 'reserved' and not has_final:
            pricing = await release_longform_pricing_for_job(
                conn,
                job_id=job_id,
                user_id=str(row['user_id']),
                reason='stabilize_terminal_failed_longform',
                tags=dict(row['tags'] or {}),
            )
            print('ACTION=RELEASED_FAILED_PARENT_RESERVATION')
            print(f'new_pricing_state={pricing.get("state")}')
        elif parent_status in TERMINAL_FAILED and reservation_status == 'released':
            print('ACTION=NONE reason=already_released')
        elif parent_status in {'succeeded','completed'}:
            print('ACTION=NONE reason=parent_completed')
        else:
            print('ACTION=NONE reason=parent_not_terminal_failed')

asyncio.run(main())
PY

printf '\n===== AFTER =====\n'
${PSQL[@]} -x -c "
select j.status as parent_status,j.error_code,left(coalesce(j.error_message,''),500) as error_message,
       r.status as reservation_status,r.reserved_credits,
       to_jsonb(r)->>'final_charged_credits' as final_charged_credits,
       r.updated_at
from public.longform_jobs j
left join lateral (
  select * from public.pricing_credit_reservations r0
  where r0.job_ref=j.id::text
  order by r0.created_at desc limit 1
) r on true
where j.id='${LATEST}'::uuid;"

printf '\n===== LATEST SEGMENTS =====\n'
${PSQL[@]} -Atc "
select concat_ws('|',segment_index,status,duration_sec,coalesce(fusion_job_id::text,''),coalesce(error_code,''),left(coalesce(error_message,''),180))
from public.longform_segments where job_id='${LATEST}'::uuid order by segment_index;"

printf '\n===== ACCOUNT BALANCE =====\n'
${PSQL[@]} -x -c "
select to_jsonb(v)->'lots_json' as lots_json,
       to_jsonb(v)->'legacy_account_json' as legacy_account_json
from public.v_pricing_account_overview v
where v.user_id=(select user_id from public.longform_jobs where id='${LATEST}'::uuid)
limit 1;" || true

printf '\n===== BOUNDED ERROR LOGS =====\n'
for c in "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER"; do
  [ -n "$c" ] || continue
  printf -- '--- %s ---\n' "$c"
  docker logs "$c" --since 90m 2>&1 \
    | grep -F "$LATEST" \
    | tail -n 50 \
    | sed -E 's#https?://[^ ]+#<url-redacted>#g' || true
done

printf '\nNO_SERVICE_RESTARTS=YES\n'
printf 'NO_DB_SCHEMA_CHANGES=YES\n'
printf '============================================================\n'
