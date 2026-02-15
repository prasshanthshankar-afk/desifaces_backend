#!/usr/bin/env bash
set -euo pipefail

COMPOSE_ENV="${COMPOSE_ENV:-./infra/.env}"
RUN_DIR="/tmp/df_commerce_worker_db_check_$(date -u +%Y%m%d_%H%M%S)"
mkdir -p "$RUN_DIR"

echo "RUN_DIR=$RUN_DIR"

docker compose --env-file "$COMPOSE_ENV" exec -T svc-commerce-worker python - <<'PY' \
  >"$RUN_DIR/out.txt" 2>&1 || true
import asyncio
from app.db import get_pool

async def main():
    pool = await get_pool()
    async with pool.acquire() as con:
        db = await con.fetchrow("select current_database() as db, inet_server_addr() as addr, inet_server_port() as port, now() as now;")
        due = await con.fetchval("""
          select count(*)
          from public.studio_jobs
          where studio_type='commerce'
            and status='queued'
            and (next_run_at is null or next_run_at <= now())
        """)
        anyq = await con.fetchval("""
          select count(*)
          from public.studio_jobs
          where studio_type='commerce' and status='queued'
        """)
        print("db=", dict(db))
        print("queued_total=", int(anyq))
        print("queued_due=", int(due))

asyncio.run(main())
PY

echo "Saved: $RUN_DIR/out.txt"
sed -n '1,50p' "$RUN_DIR/out.txt" || true