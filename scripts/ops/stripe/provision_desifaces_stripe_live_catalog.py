from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
APPLY = os.environ.get("DF_STRIPE_LIVE_PROVISION_CONFIRM", "") == "YES"
API_VERSION = os.environ.get("STRIPE_API_VERSION", "2025-03-31.basil").strip()
OUT_SQL = Path(os.environ.get("DF_STRIPE_LIVE_MAPPING_SQL", "/tmp/desifaces-stripe-live-price-mapping.sql"))
OUT_JSON = Path(os.environ.get("DF_STRIPE_LIVE_CATALOG_JSON", "/tmp/desifaces-stripe-live-catalog.json"))

if not KEY.startswith("sk_live_"):
    raise SystemExit("FAIL: STRIPE_SECRET_KEY must be a live-mode sk_live_* key")


def api(method: str, path: str, *, form=None, params=None, idem_key=None):
    url = "https://api.stripe.com" + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form, doseq=True).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + KEY)
    req.add_header("Stripe-Version", API_VERSION)
    if idem_key:
        req.add_header("Idempotency-Key", idem_key)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"FAIL: Stripe API {exc.code}: {body}") from exc


def q(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def find_price(lookup_key: str):
    payload = api(
        "GET",
        "/v1/prices",
        params=[
            ("active", "true"),
            ("limit", "2"),
            ("lookup_keys[]", lookup_key),
            ("expand[]", "data.product"),
        ],
    )
    rows = payload.get("data") or []
    if len(rows) > 1:
        raise SystemExit(f"FAIL: duplicate active Stripe prices for lookup_key={lookup_key}")
    return rows[0] if rows else None


def validate_existing(spec, price):
    if not price.get("livemode"):
        raise SystemExit(f"FAIL: lookup key resolved to non-live price: {spec['lookup_key']}")
    if str(price.get("currency") or "").lower() != spec["currency"]:
        raise SystemExit(f"FAIL: currency mismatch for {spec['lookup_key']}")
    if int(price.get("unit_amount") or -1) != spec["amount_minor"]:
        raise SystemExit(f"FAIL: amount mismatch for {spec['lookup_key']}")
    recurring = price.get("recurring")
    if spec.get("recurring_interval"):
        if not isinstance(recurring, dict) or recurring.get("interval") != spec["recurring_interval"] or int(recurring.get("interval_count") or 1) != 1:
            raise SystemExit(f"FAIL: recurring interval mismatch for {spec['lookup_key']}")
    elif recurring:
        raise SystemExit(f"FAIL: expected one-time price for {spec['lookup_key']}")
    product = price.get("product") or {}
    product_id = product.get("id") if isinstance(product, dict) else str(product)
    return product_id


def create_price(spec):
    product = api(
        "POST",
        "/v1/products",
        form=[
            ("name", spec["name"]),
            ("metadata[df_code]", spec["code"]),
            ("metadata[df_system]", "desifaces"),
            ("metadata[df_env]", "live"),
            ("metadata[df_currency]", spec["currency"].upper()),
        ],
        idem_key="desifaces-live-product-" + spec["lookup_key"],
    )
    form = [
        ("currency", spec["currency"]),
        ("unit_amount", str(spec["amount_minor"])),
        ("product", product["id"]),
        ("lookup_key", spec["lookup_key"]),
        ("metadata[df_code]", spec["code"]),
        ("metadata[df_system]", "desifaces"),
        ("metadata[df_env]", "live"),
        ("metadata[df_currency]", spec["currency"].upper()),
    ]
    if spec.get("recurring_interval"):
        form.append(("recurring[interval]", spec["recurring_interval"]))
    price = api(
        "POST",
        "/v1/prices",
        form=form,
        idem_key="desifaces-live-price-" + spec["lookup_key"],
    )
    return price, product["id"]


SPECS = [
    {"kind":"plan","code":"pro_monthly_v1","interval_code":"monthly","country_code":"","name":"desifaces Pro Monthly USD","amount_minor":2899,"currency":"usd","recurring_interval":"month","lookup_key":"df_pro_monthly_v1_usd_month_live"},
    {"kind":"plan","code":"pro_monthly_v1","interval_code":"monthly","country_code":"IN","name":"desifaces Pro Monthly INR","amount_minor":299900,"currency":"inr","recurring_interval":"month","lookup_key":"df_pro_monthly_v1_inr_month_live"},
    {"kind":"plan","code":"pro_yearly_v1","interval_code":"yearly","country_code":"","name":"desifaces Pro Yearly USD","amount_minor":28999,"currency":"usd","recurring_interval":"year","lookup_key":"df_pro_yearly_v1_usd_year_live"},
    {"kind":"plan","code":"pro_yearly_v1","interval_code":"yearly","country_code":"IN","name":"desifaces Pro Yearly INR","amount_minor":2990000,"currency":"inr","recurring_interval":"year","lookup_key":"df_pro_yearly_v1_inr_year_live"},
    {"kind":"plan","code":"business_monthly_v1","interval_code":"monthly","country_code":"","name":"desifaces Business Monthly USD","amount_minor":9999,"currency":"usd","recurring_interval":"month","lookup_key":"df_business_monthly_v1_usd_month_live"},
    {"kind":"plan","code":"business_monthly_v1","interval_code":"monthly","country_code":"IN","name":"desifaces Business Monthly INR","amount_minor":990000,"currency":"inr","recurring_interval":"month","lookup_key":"df_business_monthly_v1_inr_month_live"},
    {"kind":"plan","code":"business_yearly_v1","interval_code":"yearly","country_code":"","name":"desifaces Business Yearly USD","amount_minor":98999,"currency":"usd","recurring_interval":"year","lookup_key":"df_business_yearly_v1_usd_year_live"},
    {"kind":"plan","code":"business_yearly_v1","interval_code":"yearly","country_code":"IN","name":"desifaces Business Yearly INR","amount_minor":8390000,"currency":"inr","recurring_interval":"year","lookup_key":"df_business_yearly_v1_inr_year_live"},
    {"kind":"pack","code":"PACK_USD_1000","country_code":"","name":"desifaces 1000 Credits USD","amount_minor":999,"currency":"usd","lookup_key":"df_pack_usd_1000_live"},
    {"kind":"pack","code":"PACK_USD_5000","country_code":"","name":"desifaces 5000 Credits USD","amount_minor":3999,"currency":"usd","lookup_key":"df_pack_usd_5000_live"},
    {"kind":"pack","code":"PACK_USD_15000","country_code":"","name":"desifaces 15000 Credits USD","amount_minor":9999,"currency":"usd","lookup_key":"df_pack_usd_15000_live"},
    {"kind":"pack","code":"PACK_INR_1000","country_code":"IN","name":"desifaces 1000 Credits INR","amount_minor":99900,"currency":"inr","lookup_key":"df_pack_inr_1000_live"},
    {"kind":"pack","code":"PACK_INR_5000","country_code":"IN","name":"desifaces 5000 Credits INR","amount_minor":399900,"currency":"inr","lookup_key":"df_pack_inr_5000_live"},
    {"kind":"pack","code":"PACK_INR_15000","country_code":"IN","name":"desifaces 15000 Credits INR","amount_minor":999900,"currency":"inr","lookup_key":"df_pack_inr_15000_live"},
]

# Authenticate before any mutation.
account = api("GET", "/v1/account")
print("stripe_account_id=" + str(account.get("id") or "unknown"))
print("stripe_country=" + str(account.get("country") or "unknown"))
print("mode=" + ("APPLY" if APPLY else "PLAN"))

existing = {}
for spec in SPECS:
    price = find_price(spec["lookup_key"])
    if price:
        product_id = validate_existing(spec, price)
        existing[spec["lookup_key"]] = (price, product_id)

print(f"catalog_items={len(SPECS)}")
print(f"existing_live_prices={len(existing)}")
print(f"missing_live_prices={len(SPECS)-len(existing)}")

if not APPLY:
    print("STRIPE_LIVE_CATALOG_PLAN=PASS")
    print("STRIPE_MUTATION=NONE")
    print("NEXT=set DF_STRIPE_LIVE_PROVISION_CONFIRM=YES to create only missing exact prices")
    raise SystemExit(0)

rows = []
for spec in SPECS:
    found = existing.get(spec["lookup_key"])
    if found:
        price, product_id = found
        reused = True
    else:
        price, product_id = create_price(spec)
        validate_existing(spec, price)
        reused = False
    rows.append({
        **spec,
        "price_id": price["id"],
        "product_id": product_id,
        "reused": reused,
    })
    print(f"LIVE_PRICE=PASS code={spec['code']} currency={spec['currency'].upper()} price_id={price['id']} reused={str(reused).lower()}")

OUT_JSON.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "rows": rows}, indent=2) + "\n")
OUT_JSON.chmod(0o600)

sql = []
sql.append("-- desifaces Stripe LIVE mapping generated by provision_desifaces_stripe_live_catalog.py")
sql.append("-- Contains Stripe object IDs only; no credentials.")
sql.append("begin;")
for row in rows:
    if row["kind"] == "plan":
        sql.append(f"""
update public.pricing_plan_prices
set stripe_price_id = {q(row['price_id'])},
    metadata_json = coalesce(metadata_json, '{{}}'::jsonb)
      || jsonb_build_object('stripe_price_id',{q(row['price_id'])},'stripe_product_id',{q(row['product_id'])},'stripe_env','live'),
    updated_at = now()
where plan_code = {q(row['code'])}
  and interval_code = {q(row['interval_code'])}
  and upper(currency) = {q(row['currency'].upper())}
  and coalesce(country_code,'') = {q(row['country_code'])};
""".strip())
    else:
        sql.append(f"""
update public.pricing_credit_packs
set metadata_json = coalesce(metadata_json, '{{}}'::jsonb)
      || jsonb_build_object('stripe_price_id',{q(row['price_id'])},'stripe_product_id',{q(row['product_id'])},'stripe_env','live')
where code = {q(row['code'])}
  and upper(currency) = {q(row['currency'].upper())}
  and coalesce(country_code,'') = {q(row['country_code'])};
""".strip())

sql.append("""
do $$
declare
  missing integer;
begin
  select count(*) into missing
  from public.pricing_plan_prices
  where is_active=true and is_public=true and self_serve=true
    and tier_code in ('pro','business')
    and upper(currency) in ('USD','INR')
    and coalesce(stripe_price_id,'')='';
  if missing <> 0 then raise exception 'Missing live Stripe plan mappings: %', missing; end if;

  select count(*) into missing
  from public.pricing_credit_packs
  where is_active=true and upper(currency) in ('USD','INR')
    and coalesce(metadata_json->>'stripe_price_id','')='';
  if missing <> 0 then raise exception 'Missing live Stripe pack mappings: %', missing; end if;
end $$;
""".strip())
sql.append("commit;")
OUT_SQL.write_text("\n\n".join(sql) + "\n")
OUT_SQL.chmod(0o600)

print("STRIPE_LIVE_CATALOG_PROVISION=PASS")
print("live_price_count=14")
print("db_mapping_applied=NO")
print("runtime_changed=NO")
print("customer_charge=NONE")
print("mapping_sql=" + str(OUT_SQL))
print("catalog_json=" + str(OUT_JSON))
