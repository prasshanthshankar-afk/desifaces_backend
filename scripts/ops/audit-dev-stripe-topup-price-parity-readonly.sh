#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

PRICING="${PRICING:-df-svc-pricing}"
DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"

docker inspect "$PRICING" >/dev/null 2>&1 || { echo "FAIL: missing $PRICING"; exit 2; }
docker inspect "$DB_CONTAINER" >/dev/null 2>&1 || { echo "FAIL: missing $DB_CONTAINER"; exit 2; }

echo "============================================================"
echo " desifaces DEV — STRIPE TOP-UP PRICE PARITY"
echo " READ ONLY / NO CHECKOUT / NO MUTATION"
echo "============================================================"
echo "production=UNTOUCHED"
echo "db_mutation=NONE"
echo "stripe_mutation=NONE"
echo "checkout_created=NONE"

DB_URL="$(docker exec "$PRICING" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DB_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DB_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

echo
echo "===== 1. CURRENT DEV CATALOG + MAPPINGS ====="
"${PSQL[@]}" -P pager=off -c "
select
  code,
  credits,
  currency,
  country_code,
  price_money,
  round(price_money * 100)::bigint as expected_minor,
  metadata_json->>'stripe_price_id' as stripe_price_id,
  metadata_json->>'stripe_env' as stripe_env,
  metadata_json->>'stripe_lookup_key' as stripe_lookup_key,
  metadata_json->>'stripe_account_id' as stripe_account_id,
  is_active
from public.pricing_credit_packs
where code in ('PACK_USD_1000','PACK_USD_5000','PACK_USD_15000')
order by credits;
"

echo
echo "===== 2. LIVE STRIPE OBJECT PARITY THROUGH CURRENT PRICING SECRET ====="
docker exec -i "$PRICING" python - <<'PY'
import asyncio, json, os
from decimal import Decimal, ROUND_HALF_UP
import asyncpg, httpx

def as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed=json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value or {})
    except Exception:
        return {}

CODES=("PACK_USD_1000","PACK_USD_5000","PACK_USD_15000")

async def main():
    key=(os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not key:
        raise SystemExit("FAIL: STRIPE_SECRET_KEY missing in pricing runtime")
    mode="live" if key.startswith("sk_live_") else "test" if key.startswith("sk_test_") else "unknown"
    print("STRIPE_SECRET_MODE="+mode)
    if mode=="unknown":
        raise SystemExit("FAIL: unrecognized Stripe secret mode")

    conn=await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows=await conn.fetch("""
          select code,credits,currency,country_code,price_money,metadata_json
          from public.pricing_credit_packs
          where code=any($1::text[])
          order by credits
        """, list(CODES))
    finally:
        await conn.close()

    failures=[]
    async with httpx.AsyncClient(timeout=20.0,auth=(key,"")) as client:
        for row in rows:
            md=as_dict(row["metadata_json"])
            price_id=str(md.get("stripe_price_id") or "").strip()
            expected_minor=int((Decimal(str(row["price_money"])) * Decimal("100")).quantize(Decimal("1"),rounding=ROUND_HALF_UP))
            expected_currency=str(row["currency"] or "").lower()
            rec={
              "code":row["code"],
              "credits":int(row["credits"]),
              "db_price_money":str(row["price_money"]),
              "expected_minor":expected_minor,
              "db_stripe_price_id":price_id or None,
              "db_stripe_env":md.get("stripe_env"),
              "runtime_secret_mode":mode,
            }
            if not price_id:
                rec["gate"]="FAIL_MISSING_PRICE_ID"
                failures.append(f"{row['code']}:missing_price_id")
                print(json.dumps(rec,sort_keys=True))
                continue

            resp=await client.get(f"https://api.stripe.com/v1/prices/{price_id}")
            rec["stripe_http"]=resp.status_code
            if resp.status_code != 200:
                rec["gate"]="FAIL_STRIPE_LOOKUP"
                rec["stripe_error"]=(resp.json().get("error",{}).get("message") if "application/json" in resp.headers.get("content-type","") else resp.text[:200])
                failures.append(f"{row['code']}:stripe_lookup_{resp.status_code}")
                print(json.dumps(rec,sort_keys=True))
                continue

            p=resp.json()
            recurring=p.get("recurring")
            checks={
              "active":p.get("active") is True,
              "one_time":recurring is None,
              "currency":str(p.get("currency") or "").lower()==expected_currency,
              "amount":int(p.get("unit_amount") or -1)==expected_minor,
              "mode":bool(p.get("livemode"))==(mode=="live"),
            }
            rec.update({
              "stripe_unit_amount":p.get("unit_amount"),
              "stripe_amount_display":float(Decimal(str(p.get("unit_amount") or 0))/Decimal("100")),
              "stripe_currency":p.get("currency"),
              "stripe_active":p.get("active"),
              "stripe_livemode":p.get("livemode"),
              "stripe_type":"recurring" if recurring else "one_time",
              "checks":checks,
              "gate":"PASS" if all(checks.values()) else "FAIL",
            })
            if not all(checks.values()):
                failures.append(f"{row['code']}:"+",".join(k for k,v in checks.items() if not v))
            print(json.dumps(rec,sort_keys=True))

    print("STRIPE_TOPUP_PARITY_BAD="+str(len(failures)))
    if failures:
        print("STRIPE_TOPUP_PARITY_FAILURES="+";".join(failures))
        print("STRIPE_TOPUP_PRICE_PARITY=FAIL")
        raise SystemExit(1)
    print("STRIPE_TOPUP_PRICE_PARITY=PASS")

asyncio.run(main())
PY

echo
echo "production=UNTOUCHED"
echo "db_mutation=NONE"
echo "stripe_mutation=NONE"
echo "checkout_created=NONE"
