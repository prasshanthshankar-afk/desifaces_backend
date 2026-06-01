#!/usr/bin/env bash
set -u

# DesiFaces Google Play IAP backend smoke.
# This script validates route wiring and payload shape. For true Google Play
# verification, set GOOGLE_PLAY_VALIDATE_PURCHASES=1 and use a real Play Billing
# purchase token from an internal tester build.

ROOT_URL="${PRICING_URL:-https://api.desifaces.ai/pricing}"
CORE_URL="${CORE_URL:-https://api.desifaces.ai/core}"
EMAIL="${DF_EMAIL:-user105@desifaces.ai}"
PASSWORD="${DF_PASSWORD:-password105}"

echo "== login =="
AUTH_JSON="$(curl -sS -X POST "$CORE_URL/api/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}")"

TOKEN="$(python3 - <<'PY' "$AUTH_JSON"
import json, sys
data=json.loads(sys.argv[1])
print(data.get("access_token") or data.get("token") or data.get("data",{}).get("access_token") or data.get("data",{}).get("token") or "")
PY
)"
USER_ID="$(python3 - <<'PY' "$AUTH_JSON"
import base64, json, sys
data=json.loads(sys.argv[1])
uid=data.get("user_id") or data.get("user",{}).get("id") or ""
tok=data.get("access_token") or data.get("token") or data.get("data",{}).get("access_token") or data.get("data",{}).get("token") or ""
if not uid and tok.count(".") >= 2:
    p=tok.split(".")[1]
    p += "=" * (-len(p) % 4)
    uid=json.loads(base64.urlsafe_b64decode(p.encode()).decode()).get("sub","")
print(uid)
PY
)"

echo "TOKEN_PRESENT=$([ -n "$TOKEN" ] && echo yes || echo no)"
echo "USER_ID=$USER_ID"

echo "== catalog should expose Apple + Google product IDs =="
curl -sS "$ROOT_URL/api/payments/plans/catalog" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-User-Id: $USER_ID" \
  -H "X-Country-Code: US" | jq '.items[] | {plan_code, price_label, apple_product_id, google_product_id, google_base_plan_id}'

curl -sS "$ROOT_URL/api/payments/topups/catalog" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-User-Id: $USER_ID" \
  -H "X-Country-Code: US" | jq '.items[] | {pack_code, price_label, apple_product_id, google_product_id}'

echo "== local payload-only credit confirm example =="
echo "This only succeeds if DF_GOOGLE_PLAY_IAP_ENABLE=1 and GOOGLE_PLAY_VALIDATE_PURCHASES=0 in svc-pricing."
curl -sS -X POST "$ROOT_URL/api/payments/google/credits/confirm" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-User-Id: $USER_ID" \
  -H "X-Country-Code: US" \
  -H "Content-Type: application/json" \
  -d '{
    "google_product_id":"ai.desifaces.credits.1000",
    "purchase_token":"local-test-token-change-me",
    "package_name":"ai.desifaces.app",
    "order_id":"GPA.LOCAL.TEST",
    "country_code":"US",
    "currency":"USD",
    "raw_purchase_json":{"purchaseState":0,"acknowledgementState":1,"consumptionState":0}
  }' | jq '.'

echo "== overview =="
curl -sS "$ROOT_URL/api/payments/overview" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-User-Id: $USER_ID" \
  -H "X-Country-Code: US" | jq '{plan:.plan, credits:.credits, display:.display}'
