#!/usr/bin/env bash
set -Eeuo pipefail

resolve_container() {
  local preferred="$1" service="$2"
  if docker inspect "$preferred" >/dev/null 2>&1; then
    printf '%s' "$preferred"
    return 0
  fi
  docker ps -a --filter "label=com.docker.compose.service=${service}" --format '{{.Names}}' | head -1
}

FACE_API="$(resolve_container df-svc-face svc-face || true)"
FACE_WORKER="$(resolve_container df-svc-face-worker svc-face-worker || true)"
DB="$(resolve_container desifaces-db desifaces-db || true)"
[[ -n "$DB" ]] || DB="$(resolve_container desifaces-v3-db desifaces-db || true)"

printf '============================================================\n'
printf ' desifaces PROD — FACE CONCURRENCY + MULTI-PERSON FUSION 404\n'
printf ' READ_ONLY=YES\n'
printf '============================================================\n'

printf '\n===== 1. RUNTIME CONTAINERS =====\n'
docker ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}' | grep -Ei 'svc-face|director|fusion' || true

printf '\n===== 2. FACE WORKER LIVE SOURCE CONTRACT =====\n'
if [[ -n "$FACE_WORKER" ]]; then
  docker exec "$FACE_WORKER" python - <<'PY' || true
import inspect, os
from app.workers.face_worker import WorkerProcess
from app.services.creator_orchestrator import CreatorOrchestrator
worker = inspect.getsource(WorkerProcess)
orch = inspect.getsource(CreatorOrchestrator)
print('FACE_WORKER_CLAIM_LIMIT_1=' + ('YES' if 'limit=1' in worker else 'NO'))
print('FACE_WORKER_JOB_LEVEL_GATHER=' + ('YES' if 'asyncio.gather' in worker else 'NO'))
print('FACE_VARIANT_PARALLELISM_PRESENT=' + ('YES' if 'asyncio.gather' in orch and '_face_variant_concurrency' in orch else 'NO'))
print('DF_FACE_VARIANT_CONCURRENCY=' + os.getenv('DF_FACE_VARIANT_CONCURRENCY','<default=3>'))
PY
else
  echo 'FACE_WORKER_MISSING=YES'
fi

printf '\n===== 3. FACE QUEUE — RECENT 2 HOURS =====\n'
if [[ -n "$DB" ]]; then
  PSQL=(docker exec "$DB" psql -U desifaces_admin -d desifaces -v ON_ERROR_STOP=1 -P pager=off)
  "${PSQL[@]}" -Atc "
    select concat_ws('|',id::text,status,coalesce(attempt_count,0)::text,
      to_char(created_at at time zone 'UTC','HH24:MI:SS'),
      to_char(updated_at at time zone 'UTC','HH24:MI:SS'))
    from public.studio_jobs
    where studio_type='face'
      and created_at > now() - interval '2 hours'
    order by created_at desc
    limit 20;" || true

  printf '\n===== FACE QUEUE COUNTS =====\n'
  "${PSQL[@]}" -Atc "
    select status || '=' || count(*)::text
    from public.studio_jobs
    where studio_type='face'
      and created_at > now() - interval '2 hours'
    group by status
    order by status;" || true

  printf '\n===== FACE OVERLAP CHECK =====\n'
  "${PSQL[@]}" -Atc "
    select 'currently_running=' || count(*)::text
    from public.studio_jobs
    where studio_type='face' and status='running';" || true
else
  echo 'DB_CONTAINER_MISSING=YES'
fi

printf '\n===== 4. MULTI-PERSON FUSION FACE-READ 404 — BOUNDED LOGS =====\n'
for c in $(docker ps --format '{{.Names}}' | grep -Ei 'director|fusion' || true); do
  printf -- '--- %s ---\n' "$c"
  docker logs "$c" --since 90m 2>&1 \
    | grep -Ei 'fusion_face_read_url_failed|face.*read.*url|404.*Not Found|Not Found.*face|read_url' \
    | tail -n 100 \
    | sed -E 's#([?&])(sig|se|sp|sv|sr|skoid|sktid|skt|ske|sks|skv)=[^ &"]+#\1\2=<redacted>#g' \
    || true
done

printf '\n===== 5. FACE API READ/MEDIA ROUTES =====\n'
if [[ -n "$FACE_API" ]]; then
  docker exec "$FACE_API" python - <<'PY' || true
from app.main import app
for r in app.routes:
    p = getattr(r, 'path', '')
    methods = ','.join(sorted(getattr(r, 'methods', []) or []))
    if any(k in p.lower() for k in ('read','media','asset','artifact','face')):
        print(f'{methods}|{p}')
PY
else
  echo 'FACE_API_MISSING=YES'
fi

printf '\n===== 6. CLASSIFICATION =====\n'
if [[ -n "$FACE_WORKER" ]]; then
  JOB_LIMIT="$(docker exec "$FACE_WORKER" python - <<'PY'
import inspect
from app.workers.face_worker import WorkerProcess
s=inspect.getsource(WorkerProcess)
print('1' if 'limit=1' in s and 'asyncio.gather' not in s else '0')
PY
)"
  if [[ "$JOB_LIMIT" == "1" ]]; then
    echo 'FACE_JOB_PARALLELISM=SERIAL'
  else
    echo 'FACE_JOB_PARALLELISM=REVIEW_REQUIRED'
  fi
fi

echo 'MULTI_PERSON_FUSION_404=DIAGNOSED_FROM_LOGS_ABOVE'
echo 'READ_ONLY_DIAGNOSIS=COMPLETE'
