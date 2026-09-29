#!/usr/bin/env bash
set -Eeuo pipefail

SAMPLES="${SAMPLES:-60}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-5}"

fail(){ printf '\nFAIL: %s\n' "$*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-dev" ]] || fail "DEV host desifaces-dev required"
docker inspect df-svc-fusion >/dev/null 2>&1 || fail "df-svc-fusion missing"
docker inspect df-svc-fusion-worker >/dev/null 2>&1 || fail "df-svc-fusion-worker missing"
docker inspect desifaces-db >/dev/null 2>&1 || fail "desifaces-db missing"

echo "============================================================"
echo " desifaces DEV — SYNC3 PARALLEL RENDER WATCH"
echo "============================================================"
echo "samples=$SAMPLES interval_seconds=$INTERVAL_SECONDS"
echo "production=UNTOUCHED"

max_active=0

for N in $(seq 1 "$SAMPLES"); do
  echo
  echo "===== $(date -u +%H:%M:%S) UTC | sample=$N/$SAMPLES ====="

  echo "--- CORE FUSION ---"
  docker exec desifaces-db bash -lc '
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "
      select coalesce(string_agg(status || '''=''' || n::text, ''' ''' order by status), '''no_recent_fusion_jobs''')
      from (
        select status, count(*) as n
        from public.studio_jobs
        where studio_type='''fusion'''
          and created_at > now() - interval '''20 minutes'''
        group by status
      ) q;
    "
  ' 2>/dev/null || echo "core_fusion_query_failed"

  echo "--- SYNC PROVIDER ACTIVITY ---"
  ACTIVE="$(
    docker exec df-svc-fusion-worker python -c '
import asyncio, os, httpx
async def main():
    base=(os.getenv("SYNC_API_BASE_URL") or "https://api.sync.so").rstrip("/")
    key=os.getenv("SYNC_API_KEY") or ""
    async with httpx.AsyncClient(timeout=20) as client:
        r=await client.get(f"{base}/v2/generations?status=PROCESSING",headers={"x-api-key":key})
    try:
        p=r.json()
    except Exception:
        print("unknown")
        return
    count=None
    if isinstance(p,list):
        count=len(p)
    elif isinstance(p,dict):
        for k in ("activeGenerations","active_generations","count","total"):
            v=p.get(k)
            if isinstance(v,(int,float)):
                count=int(v)
                break
        if count is None:
            for k in ("data","items","results","generations"):
                v=p.get(k)
                if isinstance(v,list):
                    count=len(v)
                    break
    print(count if count is not None else "unknown")
asyncio.run(main())
' 2>/dev/null || true
  )"

  if [[ "$ACTIVE" =~ ^[0-9]+$ ]]; then
    echo "active_processing=$ACTIVE"
    (( ACTIVE > max_active )) && max_active="$ACTIVE"
  else
    echo "active_processing=${ACTIVE:-unknown}"
  fi

  echo "--- RECENT FUSION WORKER SIGNALS ---"
  docker logs --since 8s df-svc-fusion-worker 2>&1 |
    grep -Ei 'sync3|provider|concurrency|429|queued|processing|running|succeeded|failed|error' |
    tail -n 20 || true

  sleep "$INTERVAL_SECONDS"
done

echo
echo "============================================================"
echo " SYNC3 PARALLEL WATCH COMPLETE"
echo " max_active_processing=$max_active"
if (( max_active >= 3 )); then
  echo " SYNC3_PARALLEL_PROVIDER_EXECUTION=PASS"
else
  echo " SYNC3_PARALLEL_PROVIDER_EXECUTION=NOT_YET_PROVEN"
fi
echo " production=UNTOUCHED"
echo "============================================================"
