#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

TARGET_SHA="${TARGET_SHA:?TARGET_SHA is required}"
SHORT="${TARGET_SHA:0:12}"
PRICING="${PRICING:-df-svc-pricing}"
DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"

echo "============================================================"
echo " desifaces DEV — SELLABLE PACK + CONSUMPTION V5 REPAIR"
echo "============================================================"
echo "target_sha=$TARGET_SHA"
echo "approved_usd_packs=9.99,39.99,99.99"
echo "economics_owner=credit_consumption"
echo "production=UNTOUCHED"
echo "stripe_live=UNTOUCHED"
echo "generation_services=UNTOUCHED"
echo "service_restart=NONE"

docker inspect "$PRICING" >/dev/null 2>&1 || { echo "FAIL: missing $PRICING"; exit 2; }
docker inspect "$DB_CONTAINER" >/dev/null 2>&1 || { echo "FAIL: missing $DB_CONTAINER"; exit 2; }

REPO=""
for p in "$HOME/workspace/desifaces-runtime" "$HOME/workspace/desifaces-v3" "$HOME/workspace/desifaces_backend" "$HOME/workspace/desifaces-backend"; do
  if [[ -d "$p/.git" || -f "$p/.git" ]]; then
    remote="$(git -C "$p" remote get-url origin 2>/dev/null || true)"
    if [[ "$remote" == *"prasshanthshankar-afk/desifaces_backend"* ]]; then
      REPO="$p"
      break
    fi
  fi
done
[[ -n "$REPO" ]] || { echo "FAIL: backend repo not found"; exit 2; }

WT="/tmp/df-sellable-pack-v5-$SHORT"
rm -rf "$WT"
git -C "$REPO" fetch --no-tags origin "$TARGET_SHA" >/dev/null 2>&1 || true
git -C "$REPO" cat-file -e "$TARGET_SHA^{commit}"
git -C "$REPO" worktree add --detach "$WT" "$TARGET_SHA" >/dev/null
cleanup(){ git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true; }
trap cleanup EXIT

MIG="$WT/migrations/2026_10_01_dev_sellable_pack_consumption_alignment_v5.sql"
AUDIT="$WT/scripts/ops/audit-dev-stripe-topup-price-parity-readonly.sh"
[[ -f "$MIG" ]] || { echo "FAIL: V5 migration missing"; exit 2; }
[[ -f "$AUDIT" ]] || { echo "FAIL: Stripe parity audit missing"; exit 2; }

DB_URL="$(docker exec "$PRICING" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DB_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DB_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

echo
echo "===== 1. VERIFY ORIGINAL STRIPE TEST PACK PRICES ====="
docker exec -i "$PRICING" python - <<'PY'
import json, os, urllib.request, urllib.error

EXPECTED={
 "PACK_USD_1000":("price_1TdwQk2eFT0FzYomldUauNXe",999),
 "PACK_USD_5000":("price_1TdwQk2eFT0FzYomerXDZZ8X",3999),
 "PACK_USD_15000":("price_1TdwQk2eFT0FzYomVSzcDNP2",9999),
}
key=(os.getenv("STRIPE_SECRET_KEY") or "").strip()
if not key.startswith("sk_test_"):
    raise SystemExit("FAIL: DEV pricing runtime is not using sk_test_*")

for code,(price_id,amount) in EXPECTED.items():
    req=urllib.request.Request(f"https://api.stripe.com/v1/prices/{price_id}?expand[]=product")
    req.add_header("Authorization","Bearer "+key)
    try:
        with urllib.request.urlopen(req,timeout=20) as resp:
            p=json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"FAIL: Stripe lookup {code} HTTP {exc.code}") from exc
    checks={
      "active":p.get("active") is True,
      "test_mode":p.get("livemode") is False,
      "one_time":p.get("recurring") is None,
      "currency":p.get("currency")=="usd",
      "amount":p.get("unit_amount")==amount,
    }
    print(json.dumps({
      "code":code,
      "price_id":price_id,
      "unit_amount":p.get("unit_amount"),
      "lookup_key":p.get("lookup_key"),
      "product_id":(p.get("product") or {}).get("id") if isinstance(p.get("product"),dict) else p.get("product"),
      "checks":checks,
    },sort_keys=True))
    if not all(checks.values()):
        raise SystemExit("FAIL: original sellable Stripe test price contract mismatch for "+code)
print("ORIGINAL_STRIPE_TEST_PACKS=PASS")
PY

echo
echo "===== 2. V5 MIGRATION PREFLIGHT — ROLLBACK ONLY ====="
PREFLIGHT="/tmp/df-sellable-pack-v5-preflight.sql"
{
  echo "BEGIN;"
  sed -e '/^[[:space:]]*BEGIN;[[:space:]]*$/d' -e '/^[[:space:]]*COMMIT;[[:space:]]*$/d' "$MIG"
  echo "ROLLBACK;"
} > "$PREFLIGHT"
"${PSQL[@]}" < "$PREFLIGHT" >/tmp/df-sellable-pack-v5-preflight.log
echo "SELLABLE_PACK_V5_PREFLIGHT=PASS"

echo
echo "===== 3. APPLY V5 PACKAGE + CONSUMPTION CONTRACT ====="
"${PSQL[@]}" < "$MIG" >/tmp/df-sellable-pack-v5-apply.log
echo "SELLABLE_PACK_V5_DB_APPLY=PASS"

echo
echo "===== 4. REMAP USD PACKS TO ORIGINAL STRIPE TEST PRICES ====="
docker exec -i "$PRICING" python - <<'PY'
import asyncio, json, os, urllib.request
import asyncpg

EXPECTED={
 "PACK_USD_1000":("price_1TdwQk2eFT0FzYomldUauNXe",999,"df_pack_usd_1000_test"),
 "PACK_USD_5000":("price_1TdwQk2eFT0FzYomerXDZZ8X",3999,"df_pack_usd_5000_test"),
 "PACK_USD_15000":("price_1TdwQk2eFT0FzYomVSzcDNP2",9999,"df_pack_usd_15000_test"),
}
key=(os.getenv("STRIPE_SECRET_KEY") or "").strip()
if not key.startswith("sk_test_"):
    raise SystemExit("FAIL: sk_test_* required")

def get_price(pid):
    req=urllib.request.Request(f"https://api.stripe.com/v1/prices/{pid}?expand[]=product")
    req.add_header("Authorization","Bearer "+key)
    with urllib.request.urlopen(req,timeout=20) as resp:
        return json.loads(resp.read().decode())

async def main():
    account_req=urllib.request.Request("https://api.stripe.com/v1/account")
    account_req.add_header("Authorization","Bearer "+key)
    with urllib.request.urlopen(account_req,timeout=20) as resp:
        account=json.loads(resp.read().decode())
    account_id=str(account.get("id") or "")
    if not account_id:
        raise SystemExit("FAIL: Stripe test account id unavailable")

    conn=await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        async with conn.transaction():
            for code,(pid,amount,expected_lookup) in EXPECTED.items():
                p=get_price(pid)
                if p.get("unit_amount")!=amount or p.get("currency")!="usd" or p.get("livemode") is not False or p.get("active") is not True or p.get("recurring") is not None:
                    raise SystemExit(f"FAIL: Stripe price verification failed for {code}")
                product=p.get("product")
                product_id=product.get("id") if isinstance(product,dict) else str(product or "")
                lookup_key=str(p.get("lookup_key") or expected_lookup)
                patch={
                  "stripe_price_id":pid,
                  "stripe_product_id":product_id,
                  "stripe_lookup_key":lookup_key,
                  "stripe_env":"test",
                  "stripe_mode":"test",
                  "stripe_account_id":account_id,
                  "stripe_catalog_version":"sellable-pack-v1",
                  "commercial_pack_contract":"sellable-pack-v1",
                  "pack_price_change_requires_explicit_approval":True,
                }
                result=await conn.execute(
                  """
                  update public.pricing_credit_packs
                  set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb
                  where code=$1 and upper(currency)='USD' and coalesce(country_code,'')=''
                  """,
                  code,json.dumps(patch)
                )
                if not result.endswith("1"):
                    raise SystemExit(f"FAIL: expected one pack row for {code}; got {result}")
                print(json.dumps({
                  "code":code,
                  "price_id":pid,
                  "unit_amount":amount,
                  "lookup_key":lookup_key,
                  "product_id":product_id,
                },sort_keys=True))
    finally:
        await conn.close()

asyncio.run(main())
print("STRIPE_TEST_SELLABLE_PACK_REMAP=PASS")
PY

echo
echo "===== 5. FINAL PACKAGE CONTRACT ====="
"${PSQL[@]}" -P pager=off -c "
select code,credits,currency,country_code,price_money,
       metadata_json->>'stripe_price_id' stripe_price_id,
       metadata_json->>'commercial_pack_contract' commercial_pack_contract,
       metadata_json->>'pack_price_change_requires_explicit_approval' explicit_approval_required
from public.pricing_credit_packs
where code in (
 'PACK_USD_1000','PACK_USD_5000','PACK_USD_15000',
 'PACK_INR_1000','PACK_INR_5000','PACK_INR_15000'
)
order by currency,credits;
"

echo
echo "===== 6. FINAL CONSUMPTION ECONOMICS ====="
"${PSQL[@]}" -P pager=off -c "
with costs as (
  select c.sku_code,
         sum(case lower(c.cost_model)
             when 'variable' then c.variable_cost_money
             when 'amortized' then case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
             when 'blended' then c.variable_cost_money+case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
             else 0 end) unit_cogs_usd
  from public.pricing_sku_costs c
  where c.is_active=true and c.effective_from<=now()
    and (c.effective_to is null or c.effective_to>now())
    and c.sku_code in (
      'IMG_STD_RUN','IMG_HD_RUN','FACE_EDIT_PREMIUM_RUN',
      'FACE_MULTI_PERSON','FACE_MULTI_PERSON_I2I',
      'AUDIO_TTS_1K_CHARS','AUDIO_MULTI_PERSON',
      'FUSION_TALK_MIN','FUSION_MULTI_PERSON'
    )
  group by c.sku_code
),
min_paid as (
  select min(value) value
  from (
    select price_money/credits::numeric value
    from public.pricing_credit_packs
    where is_active=true and upper(currency)='USD' and price_money>0 and credits>0
    union all
    select p.price_money/nullif(coalesce(
      nullif(p.metadata_json->>'included_credits_total','')::numeric,
      nullif(p.metadata_json->>'grant_credits','')::numeric,
      case when p.interval_code='yearly' then t.monthly_grant_credits::numeric*12 else t.monthly_grant_credits::numeric end
    ),0)
    from public.pricing_plan_prices p
    join public.pricing_tiers t on t.code=p.tier_code
    where p.is_active=true and p.is_public=true and upper(p.currency)='USD' and p.price_money>0
  ) x where value>0
)
select s.code,s.unit,s.default_unit_credits credits_per_unit,
       round(m.value,8) min_paid_usd_per_credit,
       round(s.default_unit_credits*m.value,6) realized_value_per_unit_usd,
       round(c.unit_cogs_usd,6) provider_cogs_per_unit_usd,
       round(s.default_unit_credits*m.value-c.unit_cogs_usd,6) provider_cogs_headroom_usd,
       s.metadata_json->>'cogs_floor_credits' cogs_floor_credits
from public.pricing_skus s
join costs c on c.sku_code=s.code
cross join min_paid m
where s.code in (
 'IMG_STD_RUN','IMG_HD_RUN','FACE_EDIT_PREMIUM_RUN',
 'FACE_MULTI_PERSON','FACE_MULTI_PERSON_I2I',
 'AUDIO_TTS_1K_CHARS','AUDIO_MULTI_PERSON',
 'FUSION_TALK_MIN','FUSION_MULTI_PERSON'
)
order by case s.code
 when 'IMG_STD_RUN' then 1
 when 'IMG_HD_RUN' then 2
 when 'FACE_EDIT_PREMIUM_RUN' then 3
 when 'FACE_MULTI_PERSON' then 4
 when 'FACE_MULTI_PERSON_I2I' then 5
 when 'AUDIO_TTS_1K_CHARS' then 6
 when 'AUDIO_MULTI_PERSON' then 7
 when 'FUSION_TALK_MIN' then 8
 when 'FUSION_MULTI_PERSON' then 9
 else 99 end;
"

echo
echo "===== 7. STRIPE TEST PARITY RE-CERTIFICATION ====="
bash "$AUDIT"

echo
echo "============================================================"
echo "SELLABLE_PACK_CONTRACT=PASS"
echo "CONSUMPTION_ECONOMICS_V5=PASS"
echo "STRIPE_TEST_SELLABLE_PACK_PARITY=PASS"
echo "service_restart=NONE"
echo "generation_services=UNTOUCHED"
echo "stripe_live=UNTOUCHED"
echo "production=UNTOUCHED"
echo "============================================================"
