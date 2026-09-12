#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
COMPOSE=(bash "$ROOT/scripts/v3-compose.sh")

hold() { echo "V3 PARALLEL SAFE DEPLOY: HOLD: $*" >&2; exit 1; }

POSTGRES_DB="$(awk -F= '$1=="POSTGRES_DB"{sub(/^[^=]*=/,""); print; exit}' infra/.env)"
POSTGRES_USER="$(awk -F= '$1=="POSTGRES_USER"{sub(/^[^=]*=/,""); print; exit}' infra/.env)"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || hold "refusing non-V3 DB: $POSTGRES_DB"

echo
echo "===== ACTIVE V3 GENERATION GATE ====="
ACTIVE="$("${COMPOSE[@]}" exec -T desifaces-db psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
select count(*)
from public.studio_jobs
where studio_type in ('face','audio','fusion')
  and status in ('queued','running','processing','submitted','pending','finalizing');
")"
echo "active_generation_jobs=$ACTIVE"

if [[ "${ACTIVE:-0}" != "0" ]]; then
  "${COMPOSE[@]}" exec -T desifaces-db psql -P pager=off -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
    select studio_type,status,count(*)
    from public.studio_jobs
    where studio_type in ('face','audio','fusion')
      and status in ('queued','running','processing','submitted','pending','finalizing')
    group by studio_type,status
    order by studio_type,status;
  "
  hold "active V3 generation exists; refusing worker restart"
fi

echo "ACTIVE_GENERATION_GATE = PASS"

exec bash "$ROOT/scripts/v3-parallel-runtime-deploy-certify.sh"
