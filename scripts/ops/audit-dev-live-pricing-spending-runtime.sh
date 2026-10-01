#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

PRICING="${PRICING:-df-svc-pricing}"
DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
USER_ID="9749d12a-221b-4c2c-8eda-ec912985d693"

docker inspect "$PRICING" >/dev/null 2>&1 || { echo "FAIL: missing $PRICING"; exit 2; }

echo "============================================================"
echo " desifaces DEV — LIVE PRICING RUNTIME SPENDING PROBE"
echo " READ ONLY"
echo "============================================================"

echo
echo "===== 1. RUNTIME SOURCE CONTRACT ====="
docker exec -i "$PRICING" python - <<'PY'
import inspect
from app.services import customer_spending_service as s

src=inspect.getsource(s._usage_totals)
print("HAS_CONSUME_NEGATIVE_GATE=", 'event == "consume"' in src and 'delta < 0' in src)
print("HAS_LEDGER_SOURCE=", "pricing_credit_ledger_events" in inspect.getsource(s._ledger_rows))
print("SPENDING_SERVICE_FILE=", inspect.getsourcefile(s))
PY

echo
echo "===== 2. DIRECT RUNTIME SUMMARY ====="
docker exec -i "$PRICING" python - "$USER_ID" <<'PY'
import asyncio, json, os, sys
from uuid import UUID
import asyncpg
from app.services.customer_spending_service import spending_summary

async def main():
    conn=await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        out=await spending_summary(conn,user_id=UUID(sys.argv[1]),period="month")
        print(json.dumps(out,default=str,indent=2))
        print("RUNTIME_MONTH_CREDITS_USED="+str(out.get("credits",{}).get("consumed")))
        print("RUNTIME_MONTH_RESERVED="+str(out.get("credits",{}).get("reserved")))
        print("RUNTIME_MONTH_AVAILABLE="+str(out.get("credits",{}).get("available")))
        print("RUNTIME_MONTH_MONEY_PAID="+str(out.get("money",{}).get("paid")))
    finally:
        await conn.close()

asyncio.run(main())
PY

echo
echo "===== 3. LIVE CONTAINER IMAGE ====="
docker inspect "$PRICING" --format 'image_ref={{.Config.Image}} image_id={{.Image}} created={{.Created}}'

echo
echo "production=UNTOUCHED"
echo "db_mutation=NONE"
echo "LIVE_PRICING_RUNTIME_PROBE=COMPLETE"
