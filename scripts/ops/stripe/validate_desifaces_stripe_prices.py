import json
import sys
import urllib.request
from pathlib import Path

def load_env(path):
    env = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env

env = load_env("infra/.env")
key = env.get("STRIPE_SECRET_KEY", "")

if not key.startswith("sk_test_"):
    raise SystemExit("Refusing to validate because STRIPE_SECRET_KEY is not sk_test_*")

expected = {
    "pro_monthly_v1": {
        "price_id": "price_1TkRbe2eFT0FzYomY4KOdpD5",
        "amount": 2899,
        "currency": "usd",
        "recurring_interval": "month",
    },
    "pro_yearly_v1": {
        "price_id": "price_1TkRbf2eFT0FzYomjNvaJ1EN",
        "amount": 28999,
        "currency": "usd",
        "recurring_interval": "year",
    },
    "business_monthly_v1": {
        "price_id": "price_1TkRbf2eFT0FzYomAwQiIHsw",
        "amount": 9999,
        "currency": "usd",
        "recurring_interval": "month",
    },
    "business_yearly_v1": {
        "price_id": "price_1TkRbg2eFT0FzYomCTsQc7h6",
        "amount": 98999,
        "currency": "usd",
        "recurring_interval": "year",
    },
    "PACK_USD_1000": {
        "price_id": "price_1TdwQk2eFT0FzYomldUauNXe",
        "amount": 999,
        "currency": "usd",
        "recurring_interval": None,
    },
    "PACK_USD_5000": {
        "price_id": "price_1TdwQk2eFT0FzYomerXDZZ8X",
        "amount": 3999,
        "currency": "usd",
        "recurring_interval": None,
    },
    "PACK_USD_15000": {
        "price_id": "price_1TdwQk2eFT0FzYomVSzcDNP2",
        "amount": 9999,
        "currency": "usd",
        "recurring_interval": None,
    },
}

def get_price(price_id):
    url = f"https://api.stripe.com/v1/prices/{price_id}?expand[]=product"
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())

failures = []

for code, spec in expected.items():
    price = get_price(spec["price_id"])
    recurring = price.get("recurring")
    actual_interval = recurring.get("interval") if recurring else None

    checks = {
        "active": price.get("active") is True,
        "test_mode": price.get("livemode") is False,
        "amount": price.get("unit_amount") == spec["amount"],
        "currency": price.get("currency") == spec["currency"],
        "recurring_interval": actual_interval == spec["recurring_interval"],
    }

    print(json.dumps({
        "code": code,
        "price_id": spec["price_id"],
        "stripe_active": price.get("active"),
        "stripe_livemode": price.get("livemode"),
        "stripe_amount": price.get("unit_amount"),
        "expected_amount": spec["amount"],
        "stripe_currency": price.get("currency"),
        "expected_currency": spec["currency"],
        "stripe_recurring_interval": actual_interval,
        "expected_recurring_interval": spec["recurring_interval"],
        "product_id": price.get("product", {}).get("id") if isinstance(price.get("product"), dict) else price.get("product"),
        "checks": checks,
    }, indent=2))

    for name, ok in checks.items():
        if not ok:
            failures.append(f"{code}: failed {name}")

if failures:
    print("\nFAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)

print("\nPASS: All Stripe subscription plans and top-up prices match expected test-mode configuration.")
