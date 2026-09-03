#!/usr/bin/env bash
set -Eeuo pipefail

resolve_container(){
  local preferred="$1" service="$2"
  if docker inspect "$preferred" >/dev/null 2>&1; then printf '%s' "$preferred"; return 0; fi
  docker ps -a --filter "label=com.docker.compose.service=${service}" --format '{{.Names}}' | head -1
}

EXT_API="$(resolve_container "${EXT_API_CONTAINER:-df-v3-svc-fusion-extension}" svc-fusion-extension)"
EXT_WORKER="$(resolve_container "${EXT_WORKER_CONTAINER:-df-v3-svc-fusion-extension-worker}" svc-fusion-extension-worker)"
STITCH_WORKER="$(resolve_container "${STITCH_WORKER_CONTAINER:-df-v3-svc-fusion-extension-stitch-worker}" svc-fusion-extension-stitch-worker)"

[ -n "$EXT_API" ] || { echo "FAIL: Fusion Extension API container not found" >&2; exit 2; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE="/tmp/v3-longform-stabilization-${STAMP}.log"

printf '============================================================\n'
printf ' desifaces V3 — LONGFORM PRICING STABILIZATION\n'
printf '============================================================\n'
printf 'db_access=svc-fusion-extension:DATABASE_URL\n'

# Use the same asyncpg DATABASE_URL connection as the running Fusion Extension API.
# This avoids host-local PostgreSQL sockets and any hardcoded DB role assumptions.
docker exec -i "$EXT_API" python - >"$EVIDENCE" <<'PY'
import asyncio, json
from decimal import Decimal
from datetime import date, datetime
from uuid import UUID

from app.db import get_db_pool
from app.services.longform_orchestrator import release_longform_pricing_for_job

TERMINAL_FAILED = {'failed','error','canceled','cancelled','blocked'}


def safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, UUID)):
        return str(value)
    if isinstance(value, dict):
        return {str(k): safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(v) for v in value]
    try:
        return safe(dict(value))
    except Exception:
        return str(value)


def line(label, value):
    print(f"{label}={'' if value is None else value}")


async def latest_reservation(conn, job_id):
    return await conn.fetchrow(
        """
        select id::text,status,reserved_credits,sku_code,service_name,service_action,
               quote_json,created_at,updated_at
        from public.pricing_credit_reservations
        where job_ref=$1
        order by created_at desc limit 1
        """,
        job_id,
    )


async def main():
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id::text,user_id::text,status,error_code,error_message,
                   total_segments,completed_segments,final_storage_path,final_video_url,tags,
                   created_at,updated_at
            from public.longform_jobs
            order by created_at desc
            limit 1
            """
        )
        if not row:
            print('NO_LONGFORM_JOBS')
            return

        job_id = str(row['id'])
        reservation = await latest_reservation(conn, job_id)
        quote = safe(reservation['quote_json']) if reservation else {}
        if isinstance(quote, str):
            try: quote = json.loads(quote)
            except Exception: quote = {}
        meta = quote.get('meta') if isinstance(quote, dict) else {}
        meta = meta if isinstance(meta, dict) else {}

        print('===== BEFORE =====')
        line('job_id', job_id)
        line('user_id', row['user_id'])
        line('parent_status', row['status'])
        line('parent_error_code', row['error_code'])
        line('parent_error_message', str(row['error_message'] or '')[:700].replace('\n',' '))
        line('total_segments', row['total_segments'])
        line('completed_segments', row['completed_segments'])
        line('has_final_storage', bool(row['final_storage_path']))
        line('has_final_video', bool(row['final_video_url']))
        line('reservation_id', reservation['id'] if reservation else None)
        line('reservation_status', reservation['status'] if reservation else None)
        line('reserved_credits', reservation['reserved_credits'] if reservation else None)
        line('reservation_sku', reservation['sku_code'] if reservation else None)
        line('reservation_service', reservation['service_name'] if reservation else None)
        line('reservation_action', reservation['service_action'] if reservation else None)
        line('pricing_duration_sec', meta.get('requested_duration_sec') or meta.get('duration_sec'))
        line('pricing_segment_count', meta.get('segment_count'))
        line('pricing_segment_durations_sec', json.dumps(safe(meta.get('segment_durations_sec')), separators=(',',':')) if meta.get('segment_durations_sec') is not None else None)

        parent_status = str(row['status'] or '').lower()
        reservation_status = str((reservation['status'] if reservation else '') or '').lower()
        has_final = bool(row['final_storage_path'] or row['final_video_url'])

        print('===== SAFE RELEASE GATE =====')
        if parent_status in TERMINAL_FAILED and reservation_status == 'reserved' and not has_final:
            pricing = await release_longform_pricing_for_job(
                conn,
                job_id=job_id,
                user_id=str(row['user_id']),
                reason='stabilize_terminal_failed_longform',
                tags=dict(row['tags'] or {}),
            )
            print('ACTION=RELEASED_FAILED_PARENT_RESERVATION')
            line('new_pricing_state', pricing.get('state'))
        elif parent_status in TERMINAL_FAILED and reservation_status == 'released':
            print('ACTION=NONE reason=already_released')
        elif parent_status in {'succeeded','completed'}:
            print('ACTION=NONE reason=parent_completed')
        else:
            print('ACTION=NONE reason=parent_not_terminal_failed')

        reservation2 = await latest_reservation(conn, job_id)
        print('===== AFTER =====')
        line('parent_status_after', row['status'])
        line('reservation_status_after', reservation2['status'] if reservation2 else None)
        line('reserved_credits_after', reservation2['reserved_credits'] if reservation2 else None)

        print('===== SEGMENTS =====')
        segments = await conn.fetch(
            """
            select segment_index,status,duration_sec,fusion_job_id::text,error_code,error_message
            from public.longform_segments
            where job_id=$1::uuid
            order by segment_index
            """,
            job_id,
        )
        for s in segments:
            print('|'.join([
                str(s['segment_index']), str(s['status'] or ''), str(s['duration_sec'] or ''),
                str(s['fusion_job_id'] or ''), str(s['error_code'] or ''),
                str(s['error_message'] or '')[:240].replace('\n',' '),
            ]))

        print('===== RESERVATION LEDGER =====')
        if reservation2:
            events = await conn.fetch(
                """
                select event_type,credits_delta,sku_code,service_name,service_action,created_at
                from public.pricing_credit_ledger_events
                where reservation_id=$1::uuid
                order by created_at desc
                limit 20
                """,
                reservation2['id'],
            )
            for e in events:
                print(json.dumps(safe(dict(e)), sort_keys=True, separators=(',',':')))

        print('===== ACCOUNT BALANCE =====')
        overview = await conn.fetchrow(
            """
            select plan_json,lots_json,legacy_account_json
            from public.v_pricing_account_overview
            where user_id=$1::uuid
            limit 1
            """,
            str(row['user_id']),
        )
        if overview:
            print(json.dumps(safe(dict(overview)), sort_keys=True, separators=(',',':')))

asyncio.run(main())
PY

cat "$EVIDENCE"
LATEST="$(sed -n 's/^job_id=//p' "$EVIDENCE" | head -1)"

printf '\n===== BOUNDED ERROR LOGS =====\n'
if [ -n "$LATEST" ]; then
  for c in "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER"; do
    [ -n "$c" ] || continue
    printf -- '--- %s ---\n' "$c"
    docker logs "$c" --since 120m 2>&1 \
      | grep -F "$LATEST" \
      | tail -n 60 \
      | sed -E 's#https?://[^ ]+#<url-redacted>#g' || true
  done
fi

printf '\nNO_SERVICE_RESTARTS=YES\n'
printf 'NO_DB_SCHEMA_CHANGES=YES\n'
printf 'DB_CONNECTION_SOURCE=FUSION_EXTENSION_RUNTIME\n'
printf 'evidence=%s\n' "$EVIDENCE"
printf '============================================================\n'
