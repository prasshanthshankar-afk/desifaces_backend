import os
import json
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone

key = os.environ.get("STRIPE_SECRET_KEY", "")
if not key.startswith("sk_test_"):
    raise SystemExit("Refusing to create Stripe test prices because STRIPE_SECRET_KEY is not sk_test_*")

def api(method, path, form=None, params=None, idem_key=None):
    url = "https://api.stripe.com" + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    data = None
    if form is not None:
        data = urllib.parse.urlencode(form, doseq=True).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    if idem_key:
        req.add_header("Idempotency-Key", idem_key)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Stripe API error {e.code}: {body}") from e

def q(s):
    return "'" + str(s).replace("'", "''") + "'"

def find_price(lookup_key):
    data = api(
        "GET",
        "/v1/prices",
        params=[
            ("active", "true"),
            ("limit", "1"),
            ("lookup_keys[]", lookup_key),
            ("expand[]", "data.product"),
        ],
    )
    rows = data.get("data") or []
    return rows[0] if rows else None

def ensure_price(code, name, amount_minor, currency, lookup_key, recurring_interval=None):
    existing = find_price(lookup_key)
    if existing:
        product = existing.get("product") or {}
        product_id = product.get("id") if isinstance(product, dict) else str(product)
        return {
            "code": code,
            "price_id": existing["id"],
            "product_id": product_id,
            "lookup_key": lookup_key,
            "reused": True,
        }

    product = api(
        "POST",
        "/v1/products",
        form=[
            ("name", name),
            ("metadata[df_code]", code),
            ("metadata[df_system]", "desifaces"),
            ("metadata[df_env]", "test"),
        ],
        idem_key="desifaces-test-product-" + lookup_key,
    )

    form = [
        ("currency", currency.lower()),
        ("unit_amount", str(amount_minor)),
        ("product", product["id"]),
        ("lookup_key", lookup_key),
        ("metadata[df_code]", code),
        ("metadata[df_system]", "desifaces"),
        ("metadata[df_env]", "test"),
    ]

    if recurring_interval:
        form.append(("recurring[interval]", recurring_interval))

    price = api(
        "POST",
        "/v1/prices",
        form=form,
        idem_key="desifaces-test-price-" + lookup_key,
    )

    return {
        "code": code,
        "price_id": price["id"],
        "product_id": product["id"],
        "lookup_key": lookup_key,
        "reused": False,
    }

subscription_specs = [
    {
        "code": "pro_monthly_v1",
        "name": "DesiFaces Pro Monthly",
        "amount_minor": 2899,
        "currency": "usd",
        "interval_code": "monthly",
        "recurring_interval": "month",
        "lookup_key": "df_pro_monthly_v1_usd_month_test",
    },
    {
        "code": "pro_yearly_v1",
        "name": "DesiFaces Pro Yearly",
        "amount_minor": 28999,
        "currency": "usd",
        "interval_code": "yearly",
        "recurring_interval": "year",
        "lookup_key": "df_pro_yearly_v1_usd_year_test",
    },
    {
        "code": "business_monthly_v1",
        "name": "DesiFaces Business Monthly",
        "amount_minor": 9999,
        "currency": "usd",
        "interval_code": "monthly",
        "recurring_interval": "month",
        "lookup_key": "df_business_monthly_v1_usd_month_test",
    },
    {
        "code": "business_yearly_v1",
        "name": "DesiFaces Business Yearly",
        "amount_minor": 98999,
        "currency": "usd",
        "interval_code": "yearly",
        "recurring_interval": "year",
        "lookup_key": "df_business_yearly_v1_usd_year_test",
    },
]

pack_specs = [
    {
        "code": "PACK_USD_1000",
        "name": "DesiFaces Starter Pack - 1000 Credits",
        "amount_minor": 999,
        "currency": "usd",
        "lookup_key": "df_pack_usd_1000_test",
    },
    {
        "code": "PACK_USD_5000",
        "name": "DesiFaces Value Pack - 5000 Credits",
        "amount_minor": 3999,
        "currency": "usd",
        "lookup_key": "df_pack_usd_5000_test",
    },
    {
        "code": "PACK_USD_15000",
        "name": "DesiFaces Pro Pack - 15000 Credits",
        "amount_minor": 9999,
        "currency": "usd",
        "lookup_key": "df_pack_usd_15000_test",
    },
]

subs = []
packs = []

for spec in subscription_specs:
    created = ensure_price(
        code=spec["code"],
        name=spec["name"],
        amount_minor=spec["amount_minor"],
        currency=spec["currency"],
        lookup_key=spec["lookup_key"],
        recurring_interval=spec["recurring_interval"],
    )
    created["interval_code"] = spec["interval_code"]
    subs.append(created)

for spec in pack_specs:
    created = ensure_price(
        code=spec["code"],
        name=spec["name"],
        amount_minor=spec["amount_minor"],
        currency=spec["currency"],
        lookup_key=spec["lookup_key"],
        recurring_interval=None,
    )
    packs.append(created)

print("-- DesiFaces Stripe TEST price mapping seed")
print("-- Generated at " + datetime.now(timezone.utc).isoformat())
print("-- This file contains TEST MODE Stripe price IDs only.")
print("begin;")
print()

for row in subs:
    print(f"-- {row['code']} => {row['price_id']} product={row['product_id']} reused={row['reused']}")
    print(f"""
update public.pricing_plan_prices
set stripe_price_id = {q(row['price_id'])},
    metadata_json = jsonb_set(
      jsonb_set(
        coalesce(metadata_json, '{{}}'::jsonb),
        '{{stripe_price_id}}',
        to_jsonb({q(row['price_id'])}::text),
        true
      ),
      '{{stripe_product_id}}',
      to_jsonb({q(row['product_id'])}::text),
      true
    ),
    updated_at = now()
where plan_code = {q(row['code'])}
  and interval_code = {q(row['interval_code'])}
  and upper(currency) = 'USD';
""".strip())
    print()

for row in packs:
    print(f"-- {row['code']} => {row['price_id']} product={row['product_id']} reused={row['reused']}")
    print(f"""
update public.pricing_credit_packs
set metadata_json = jsonb_set(
      jsonb_set(
        coalesce(metadata_json, '{{}}'::jsonb),
        '{{stripe_price_id}}',
        to_jsonb({q(row['price_id'])}::text),
        true
      ),
      '{{stripe_product_id}}',
      to_jsonb({q(row['product_id'])}::text),
      true
    )
where code = {q(row['code'])}
  and upper(currency) = 'USD';
""".strip())
    print()

print("""
do $$
declare
  missing_count integer;
begin
  select count(*) into missing_count
  from public.pricing_plan_prices
  where plan_code in ('pro_monthly_v1','pro_yearly_v1','business_monthly_v1','business_yearly_v1')
    and upper(currency) = 'USD'
    and coalesce(stripe_price_id, '') = '';

  if missing_count > 0 then
    raise exception 'Missing Stripe plan price mappings after seed: %', missing_count;
  end if;

  select count(*) into missing_count
  from public.pricing_credit_packs
  where code in ('PACK_USD_1000','PACK_USD_5000','PACK_USD_15000')
    and upper(currency) = 'USD'
    and coalesce(metadata_json->>'stripe_price_id', '') = '';

  if missing_count > 0 then
    raise exception 'Missing Stripe top-up price mappings after seed: %', missing_count;
  end if;
end $$;
""".strip())

print()
print("commit;")
