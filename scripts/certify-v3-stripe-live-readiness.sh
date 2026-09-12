#!/usr/bin/env bash
set -Eeuo pipefail

MODE="${1:-prepare}"
[[ "$MODE" == "prepare" || "$MODE" == "cutover" ]] || { echo "usage: $0 [prepare|cutover]"; exit 2; }

PRICING_CONTAINER="${DF_PRICING_CONTAINER:-df-v3-svc-pricing}"
if ! docker inspect "$PRICING_CONTAINER" >/dev/null 2>&1; then
  for c in df-svc-pricing svc-pricing; do
    if docker inspect "$c" >/dev/null 2>&1; then PRICING_CONTAINER="$c"; break; fi
  done
fi
[[ -n "$PRICING_CONTAINER" ]] && docker inspect "$PRICING_CONTAINER" >/dev/null 2>&1 || { echo "FAIL: pricing container not found"; exit 1; }

getenv() {
  local key="$1"
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$PRICING_CONTAINER" | awk -F= -v k="$key" '$1==k{sub(/^[^=]*=/,""); print; exit}'
}

SECRET="$(getenv STRIPE_SECRET_KEY)"
PUB="$(getenv STRIPE_PUBLISHABLE_KEY)"
WEBHOOK="$(getenv STRIPE_WEBHOOK_SECRET)"
GATEWAY_ENABLED="$(getenv DF_PAYMENT_GATEWAY_ENABLED)"
GATEWAY_PROVIDER="$(getenv DF_PAYMENT_GATEWAY_PROVIDER)"

is_live_secret=false; [[ "$SECRET" == sk_live_* ]] && is_live_secret=true
is_live_pub=false; [[ "$PUB" == pk_live_* ]] && is_live_pub=true
has_webhook=false; [[ "$WEBHOOK" == whsec_* ]] && has_webhook=true
provider_ok=false; [[ "${GATEWAY_PROVIDER,,}" == "stripe" || -z "$GATEWAY_PROVIDER" ]] && provider_ok=true

echo "============================================================"
echo " desifaces V3 — STRIPE LIVE READINESS"
echo "============================================================"
echo "mode=$MODE"
echo "pricing_container=$PRICING_CONTAINER"
echo "stripe_secret_live=$is_live_secret"
echo "stripe_publishable_live=$is_live_pub"
echo "stripe_webhook_secret_present=$has_webhook"
echo "gateway_provider_stripe=$provider_ok"
echo "gateway_enabled=${GATEWAY_ENABLED:-unset}"

# Never print secret/key values.
if [[ "$MODE" == "cutover" ]]; then
  $is_live_secret || { echo "STRIPE_LIVE_SECRET_GATE=FAIL"; exit 1; }
  $is_live_pub || { echo "STRIPE_LIVE_PUBLISHABLE_GATE=FAIL"; exit 1; }
  $has_webhook || { echo "STRIPE_LIVE_WEBHOOK_GATE=FAIL"; exit 1; }
  $provider_ok || { echo "STRIPE_PROVIDER_GATE=FAIL"; exit 1; }
fi

DB="${DF_DB_CONTAINER:-}"
if [[ -z "$DB" ]]; then
  for candidate in desifaces-v3-db df-v3-db desifaces-db; do
    if docker inspect "$candidate" >/dev/null 2>&1; then DB="$candidate"; break; fi
  done
fi
[[ -n "$DB" ]] || { echo "FAIL: postgres container not found"; exit 1; }
PGUSER="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_USER"{print $2; exit}')"; PGUSER="${PGUSER:-postgres}"
PGDB="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_DB"{print $2; exit}')"; PGDB="${PGDB:-postgres}"
PSQL=(docker exec -i "$DB" psql -X -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB")

echo "===== STRIPE PRICE-ID COVERAGE ====="
# Introspect both plan and credit-pack catalogs. JSON access keeps this compatible
# with schemas where stripe_price_id lives in metadata_json rather than a scalar column.
"${PSQL[@]}" -P pager=off -c "
select 'tier_price' catalog,
       coalesce(to_jsonb(t)->>'tier_code',to_jsonb(t)->>'plan_code','') item,
       coalesce(to_jsonb(t)->>'billing_period',to_jsonb(t)->>'period','') period,
       coalesce(to_jsonb(t)->>'currency','') currency,
       coalesce(to_jsonb(t)->>'stripe_price_id',to_jsonb(t)#>>'{metadata_json,stripe_price_id}','') stripe_price_id
from public.pricing_tier_prices t
where coalesce(to_jsonb(t)->>'status','active')='active'
union all
select 'credit_pack',
       coalesce(to_jsonb(p)->>'code',to_jsonb(p)->>'pack_code',''),
       '',coalesce(to_jsonb(p)->>'currency',''),
       coalesce(to_jsonb(p)->>'stripe_price_id',to_jsonb(p)#>>'{metadata_json,stripe_price_id}','')
from public.pricing_credit_packs p
where coalesce(to_jsonb(p)->>'status','active')='active'
order by 1,2,3,4;" 2>/dev/null || echo "PRICE_ID_COVERAGE_QUERY=SCHEMA_VARIANT_REQUIRES_EXISTING_VALIDATOR"

if [[ "$MODE" == "prepare" ]]; then
  echo "STRIPE_PREPARATION_STATUS=INSPECTED"
  if ! $is_live_secret || ! $is_live_pub || ! $has_webhook; then
    echo "STRIPE_CUTOVER_READY=NO"
    echo "NEXT=provision live keys, live webhook signing secret, and live Price IDs without committing secrets"
  else
    echo "STRIPE_CUTOVER_READY=SECRETS_PRESENT_REQUIRES_LIVE_PRICE_AND_WEBHOOK_E2E"
  fi
else
  echo "STRIPE_SECRET_SHAPE_GATE=PASS"
  echo "STRIPE_CUTOVER_READINESS=PASS_REQUIRES_REAL_PAYMENT_SMOKE_AND_REFUND"
fi
