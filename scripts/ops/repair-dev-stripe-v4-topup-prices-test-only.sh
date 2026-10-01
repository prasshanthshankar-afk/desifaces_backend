#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

PRICING="${PRICING:-df-svc-pricing}"
DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"

docker inspect "$PRICING" >/dev/null 2>&1 || { echo "FAIL: missing $PRICING"; exit 2; }
docker inspect "$DB_CONTAINER" >/dev/null 2>&1 || { echo "FAIL: missing $DB_CONTAINER"; exit 2; }

echo "============================================================"
echo " desifaces DEV — STRIPE V4 TOP-UP PRICE REPAIR"
echo " TEST MODE ONLY / USD PACKS ONLY"
echo "============================================================"
echo "production=UNTOUCHED"
echo "subscription_prices=UNTOUCHED"
echo "generation_services=UNTOUCHED"
echo "checkout_created=NONE"

docker exec -i "$PRICING" python - <<'PY'
import asyncio
import json
import os
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import asyncpg

EXPECTED = {
    "PACK_USD_1000": {
        "credits": 1000,
        "amount_minor": 4125,
        "lookup_key": "df_pack_usd_1000_v4_test",
        "name": "desifaces Starter Pack - 1000 Credits",
    },
    "PACK_USD_5000": {
        "credits": 5000,
        "amount_minor": 20625,
        "lookup_key": "df_pack_usd_5000_v4_test",
        "name": "desifaces Value Pack - 5000 Credits",
    },
    "PACK_USD_15000": {
        "credits": 15000,
        "amount_minor": 61875,
        "lookup_key": "df_pack_usd_15000_v4_test",
        "name": "desifaces Pro Pack - 15000 Credits",
    },
}

def as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value or {})
    except Exception:
        return {}

key=(os.getenv("STRIPE_SECRET_KEY") or "").strip()
if not key.startswith("sk_test_"):
    raise SystemExit("FAIL: refusing Stripe mutation because DEV runtime is not using sk_test_*")

def api(method, path, form=None, params=None, idem_key=None):
    url="https://api.stripe.com"+path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data=None
    if form is not None:
        data=urllib.parse.urlencode(form, doseq=True).encode("utf-8")
    req=urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization","Bearer "+key)
    if idem_key:
        req.add_header("Idempotency-Key", idem_key)
    if data is not None:
        req.add_header("Content-Type","application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body=exc.read().decode("utf-8",errors="replace")
        raise SystemExit(f"FAIL: Stripe API {exc.code}: {body}") from exc

def get_price(price_id):
    return api("GET", f"/v1/prices/{price_id}", params=[("expand[]","product")])

def find_price(lookup_key):
    data=api(
        "GET",
        "/v1/prices",
        params=[
            ("active","true"),
            ("limit","10"),
            ("lookup_keys[]",lookup_key),
            ("expand[]","data.product"),
        ],
    )
    rows=data.get("data") or []
    if len(rows)>1:
        raise SystemExit(f"FAIL: multiple active Stripe prices for lookup_key={lookup_key}")
    return rows[0] if rows else None

def ensure_product(name, code, old_price_id):
    if old_price_id:
        old=get_price(old_price_id)
        product=old.get("product")
        if isinstance(product,dict) and product.get("id"):
            return product["id"]
        if isinstance(product,str) and product:
            return product
    product=api(
        "POST",
        "/v1/products",
        form=[
            ("name",name),
            ("metadata[df_code]",code),
            ("metadata[df_system]","desifaces"),
            ("metadata[df_env]","test"),
            ("metadata[df_catalog_version]","v4"),
        ],
        idem_key=f"desifaces-v4-test-product-{code.lower()}",
    )
    return product["id"]

def verify_price(code, price, expected):
    recurring=price.get("recurring")
    checks={
        "active": price.get("active") is True,
        "test_mode": price.get("livemode") is False,
        "currency": str(price.get("currency") or "").lower()=="usd",
        "amount": int(price.get("unit_amount") or -1)==int(expected["amount_minor"]),
        "one_time": recurring is None,
    }
    print(json.dumps({
        "code":code,
        "price_id":price.get("id"),
        "unit_amount":price.get("unit_amount"),
        "currency":price.get("currency"),
        "active":price.get("active"),
        "livemode":price.get("livemode"),
        "lookup_key":price.get("lookup_key"),
        "checks":checks,
    },sort_keys=True))
    if not all(checks.values()):
        bad=",".join(k for k,v in checks.items() if not v)
        raise SystemExit(f"FAIL: Stripe price verification failed for {code}: {bad}")

async def main():
    conn=await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows=await conn.fetch(
            """
            select code,credits,currency,country_code,price_money,metadata_json
            from public.pricing_credit_packs
            where code=any($1::text[])
            order by credits
            """,
            list(EXPECTED),
        )
        if len(rows)!=3:
            raise SystemExit(f"FAIL: expected 3 USD packs, found {len(rows)}")

        db={}
        for row in rows:
            code=str(row["code"])
            exp=EXPECTED[code]
            amount_minor=int(
                (Decimal(str(row["price_money"])) * Decimal("100"))
                .quantize(Decimal("1"),rounding=ROUND_HALF_UP)
            )
            if str(row["currency"]).upper()!="USD":
                raise SystemExit(f"FAIL: {code} currency is not USD")
            if int(row["credits"])!=exp["credits"]:
                raise SystemExit(f"FAIL: {code} credits mismatch")
            if amount_minor!=exp["amount_minor"]:
                raise SystemExit(
                    f"FAIL: {code} catalog amount changed: db={amount_minor} expected={exp['amount_minor']}"
                )
            db[code]={
                "row":row,
                "metadata":as_dict(row["metadata_json"]),
                "amount_minor":amount_minor,
            }

        account=api("GET","/v1/account")
        account_id=str(account.get("id") or "")
        if not account_id:
            raise SystemExit("FAIL: Stripe account id unavailable")
        print("STRIPE_TEST_ACCOUNT="+account_id)

        replacements={}
        for code, exp in EXPECTED.items():
            current_md=db[code]["metadata"]
            old_price_id=str(current_md.get("stripe_price_id") or "").strip()
            existing=find_price(exp["lookup_key"])

            if existing:
                verify_price(code,existing,exp)
                price=existing
                product=existing.get("product")
                product_id=product.get("id") if isinstance(product,dict) else str(product or "")
                reused=True
            else:
                product_id=ensure_product(exp["name"],code,old_price_id)
                price=api(
                    "POST",
                    "/v1/prices",
                    form=[
                        ("currency","usd"),
                        ("unit_amount",str(exp["amount_minor"])),
                        ("product",product_id),
                        ("lookup_key",exp["lookup_key"]),
                        ("metadata[df_code]",code),
                        ("metadata[df_system]","desifaces"),
                        ("metadata[df_env]","test"),
                        ("metadata[df_catalog_version]","v4"),
                        ("metadata[df_credits]",str(exp["credits"])),
                    ],
                    idem_key=f"desifaces-v4-test-price-{code.lower()}-{exp['amount_minor']}",
                )
                verify_price(code,price,exp)
                reused=False

            replacements[code]={
                "price_id":str(price["id"]),
                "product_id":product_id,
                "lookup_key":exp["lookup_key"],
                "old_price_id":old_price_id or None,
                "reused":reused,
            }

        mapped_at=datetime.now(timezone.utc).isoformat()
        async with conn.transaction():
            for code, rep in replacements.items():
                patch={
                    "stripe_price_id":rep["price_id"],
                    "stripe_product_id":rep["product_id"],
                    "stripe_lookup_key":rep["lookup_key"],
                    "stripe_env":"test",
                    "stripe_mode":"test",
                    "stripe_account_id":account_id,
                    "stripe_catalog_version":"v4",
                    "stripe_v4_mapped_at":mapped_at,
                }
                result=await conn.execute(
                    """
                    update public.pricing_credit_packs
                    set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb
                    where code=$1
                      and upper(currency)='USD'
                      and coalesce(country_code,'')=''
                      and is_active=true
                    """,
                    code,
                    json.dumps(patch),
                )
                if not result.endswith("1"):
                    raise SystemExit(f"FAIL: expected one DEV pack row updated for {code}, got {result}")

        print("===== REMAP RESULT =====")
        for code, rep in replacements.items():
            exp=EXPECTED[code]
            mapped=await conn.fetchrow(
                """
                select code,credits,price_money,metadata_json
                from public.pricing_credit_packs
                where code=$1 and upper(currency)='USD' and coalesce(country_code,'')=''
                """,
                code,
            )
            md=as_dict(mapped["metadata_json"])
            price=get_price(str(md.get("stripe_price_id") or ""))
            verify_price(code,price,exp)
            print(json.dumps({
                "code":code,
                "old_price_id":rep["old_price_id"],
                "new_price_id":md.get("stripe_price_id"),
                "new_lookup_key":md.get("stripe_lookup_key"),
                "db_price_money":str(mapped["price_money"]),
                "stripe_unit_amount":price.get("unit_amount"),
                "reused_existing_v4_price":rep["reused"],
            },sort_keys=True))

        print("STRIPE_V4_TOPUP_REMAP=PASS")
        print("STRIPE_TOPUP_PRICE_PARITY=PASS")
    finally:
        await conn.close()

asyncio.run(main())
PY

echo
echo "production=UNTOUCHED"
echo "subscription_prices=UNTOUCHED"
echo "generation_services=UNTOUCHED"
echo "checkout_created=NONE"
echo "DEV_STRIPE_V4_TOPUP_REPAIR=COMPLETE"
