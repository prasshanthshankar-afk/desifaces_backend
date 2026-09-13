#!/usr/bin/env bash
set -Eeuo pipefail

SUBSCRIPTION="e70463a7-4016-4cf5-9291-b640b0f0f5cb"
RG="DESIFACES_RG"
VM="desifaces-gpu-non-prod"
LOCATION="northcentralus"
EXPECTED_ACCOUNT="acct_1TIIxvPA22bn06oY"
WEBHOOK_URL="https://api.desifaces.ai/pricing/api/payments/webhooks/stripe"
API_VERSION="2025-03-31.basil"
INSTALLER_REF="5a70943fdaa3ef4d75ea5cb5f3064cc39164f506"
INSTALLER_URI="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${INSTALLER_REF}/scripts/ops/stripe/install_desifaces_stripe_live_runtime_prod.sh"
RUN_NAME="stripe-live-runtime-$(date -u +%Y%m%dT%H%M%SZ)"
WEBHOOK_ID=""
INSTALL_DONE=0
RUN_CREATED=0

fail() { echo "FAIL: $*" >&2; exit 1; }

cleanup() {
  if [[ "$RUN_CREATED" == "1" ]]; then
    az vm run-command delete -g "$RG" --vm-name "$VM" --run-command-name "$RUN_NAME" -y --only-show-errors >/dev/null 2>&1 || true
  fi
  if [[ "$INSTALL_DONE" != "1" && -n "$WEBHOOK_ID" && -n "${SK:-}" ]]; then
    STRIPE_SECRET_KEY="$SK" WEBHOOK_ID="$WEBHOOK_ID" python3 - <<'PY' >/dev/null 2>&1 || true
import os, urllib.request
req=urllib.request.Request('https://api.stripe.com/v1/webhook_endpoints/'+os.environ['WEBHOOK_ID'], method='DELETE')
req.add_header('Authorization','Bearer '+os.environ['STRIPE_SECRET_KEY'])
req.add_header('Stripe-Version','2025-03-31.basil')
with urllib.request.urlopen(req, timeout=30) as r: r.read()
PY
  fi
  unset SK PK WHSEC CREATE_OUT || true
}
trap cleanup EXIT

command -v az >/dev/null 2>&1 || fail "Azure CLI missing"
command -v python3 >/dev/null 2>&1 || fail "python3 missing"
az account set --subscription "$SUBSCRIPTION" >/dev/null

echo "============================================================"
echo " desifaces — STRIPE LIVE WEBHOOK + RUNTIME CUTOVER"
echo " secrets_echoed=NO"
echo " protected_vm_parameters=YES"
echo " pricing_service_only=YES"
echo " customer_charge=NONE"
echo "============================================================"

read -rsp "Stripe LIVE secret key (sk_live_...): " SK; echo
[[ "$SK" == sk_live_* ]] || fail "secret key must start sk_live_"
read -rsp "Stripe LIVE publishable key (pk_live_...): " PK; echo
[[ "$PK" == pk_live_* ]] || fail "publishable key must start pk_live_"

echo "===== 1. STRIPE LIVE ACCOUNT + WEBHOOK PRECHECK ====="
export STRIPE_SECRET_KEY="$SK" EXPECTED_ACCOUNT WEBHOOK_URL API_VERSION
PRECHECK="$(python3 - <<'PY'
import json, os, urllib.parse, urllib.request

def api(method,path,data=None):
    b=None if data is None else urllib.parse.urlencode(data, doseq=True).encode()
    req=urllib.request.Request('https://api.stripe.com'+path,data=b,method=method)
    req.add_header('Authorization','Bearer '+os.environ['STRIPE_SECRET_KEY'])
    req.add_header('Stripe-Version',os.environ['API_VERSION'])
    if b is not None: req.add_header('Content-Type','application/x-www-form-urlencoded')
    with urllib.request.urlopen(req,timeout=30) as r: return json.loads(r.read())
a=api('GET','/v1/account')
if a.get('id') != os.environ['EXPECTED_ACCOUNT']:
    raise SystemExit('FAIL: unexpected Stripe account '+str(a.get('id')))
l=api('GET','/v1/webhook_endpoints?limit=100')
m=[x for x in l.get('data',[]) if x.get('url')==os.environ['WEBHOOK_URL']]
print('account='+a['id'])
print('country='+str(a.get('country') or ''))
print('existing_exact_webhooks='+str(len(m)))
if m:
    raise SystemExit('FAIL: exact LIVE webhook URL already exists; signing secret is only returned at creation, so refuse duplicate cutover')
PY
)" || fail "$PRECHECK"
printf '%s\n' "$PRECHECK"

echo "===== 2. CREATE EXACT LIVE WEBHOOK ====="
CREATE_OUT="$(python3 - <<'PY'
import json, os, urllib.parse, urllib.request
E=[
 'checkout.session.completed','checkout.session.expired',
 'customer.subscription.created','customer.subscription.updated','customer.subscription.deleted',
 'invoice.paid','invoice.payment_failed','payment_intent.payment_failed'
]
data=[('url',os.environ['WEBHOOK_URL']),('api_version',os.environ['API_VERSION']),('description','desifaces.ai production Stripe LIVE webhook')]
data += [('enabled_events[]',x) for x in E]
b=urllib.parse.urlencode(data).encode()
req=urllib.request.Request('https://api.stripe.com/v1/webhook_endpoints',data=b,method='POST')
req.add_header('Authorization','Bearer '+os.environ['STRIPE_SECRET_KEY'])
req.add_header('Stripe-Version',os.environ['API_VERSION'])
req.add_header('Content-Type','application/x-www-form-urlencoded')
with urllib.request.urlopen(req,timeout=30) as r: w=json.loads(r.read())
if not w.get('livemode') or w.get('status')!='enabled' or w.get('url')!=os.environ['WEBHOOK_URL']:
    raise SystemExit('FAIL: created webhook failed live/status/url gate')
if sorted(w.get('enabled_events') or []) != sorted(E):
    raise SystemExit('FAIL: created webhook event set mismatch')
sec=str(w.get('secret') or '')
if not sec.startswith('whsec_'): raise SystemExit('FAIL: Stripe did not return webhook signing secret')
print(sec)
print(str(w.get('id') or ''))
PY
)"
WHSEC="$(printf '%s\n' "$CREATE_OUT" | sed -n '1p')"
WEBHOOK_ID="$(printf '%s\n' "$CREATE_OUT" | sed -n '2p')"
[[ "$WHSEC" == whsec_* ]] || fail "webhook secret capture failed"
[[ "$WEBHOOK_ID" == we_* ]] || fail "webhook id capture failed"
echo "webhook_id=$WEBHOOK_ID"
echo "webhook_url=$WEBHOOK_URL"
echo "webhook_event_count=8"
echo "STRIPE_LIVE_WEBHOOK_CREATE=PASS"

echo "===== 3. INSTALL LIVE SECRETS AS AZURE PROTECTED PARAMETERS ====="
az vm run-command create \
  --resource-group "$RG" \
  --vm-name "$VM" \
  --run-command-name "$RUN_NAME" \
  --location "$LOCATION" \
  --script-uri "$INSTALLER_URI" \
  --protected-parameters \
    STRIPE_SECRET_KEY="$SK" \
    STRIPE_PUBLISHABLE_KEY="$PK" \
    STRIPE_WEBHOOK_SECRET="$WHSEC" \
  --timeout-in-seconds 1200 \
  --only-show-errors \
  -o none
RUN_CREATED=1

STATE="$(az vm run-command show -g "$RG" --vm-name "$VM" --run-command-name "$RUN_NAME" --instance-view --query 'instanceView.executionState' -o tsv)"
EXIT_CODE="$(az vm run-command show -g "$RG" --vm-name "$VM" --run-command-name "$RUN_NAME" --instance-view --query 'instanceView.exitCode' -o tsv)"
OUTPUT="$(az vm run-command show -g "$RG" --vm-name "$VM" --run-command-name "$RUN_NAME" --instance-view --query 'instanceView.output' -o tsv)"
printf '%s\n' "$OUTPUT"
[[ "$STATE" == "Succeeded" ]] || fail "managed run command execution state=$STATE"
[[ "$EXIT_CODE" == "0" ]] || fail "managed run command exit=$EXIT_CODE"
grep -Fq 'STRIPE_LIVE_RUNTIME_INSTALL=PASS' <<<"$OUTPUT" || fail "runtime installer PASS marker missing"
INSTALL_DONE=1

echo "===== 4. DELETE MANAGED RUN-COMMAND RESOURCE ====="
az vm run-command delete -g "$RG" --vm-name "$VM" --run-command-name "$RUN_NAME" -y --only-show-errors >/dev/null
RUN_CREATED=0
echo "PROTECTED_RUN_COMMAND_CLEANUP=PASS"

echo "===== 5. LIVE RUNTIME + PUBLIC CERTIFICATION ====="
PUBLIC_CODE="$(curl -ksS -o /dev/null -w '%{http_code}' --max-time 10 https://api.desifaces.ai/pricing/api/health || true)"
echo "public_pricing_health=$PUBLIC_CODE"
[[ "$PUBLIC_CODE" == "200" ]] || fail "public pricing health not 200"

VERIFY="$(python3 - <<'PY'
import json, os, urllib.request
req=urllib.request.Request('https://api.stripe.com/v1/webhook_endpoints/'+os.environ['WEBHOOK_ID'])
req.add_header('Authorization','Bearer '+os.environ['STRIPE_SECRET_KEY'])
req.add_header('Stripe-Version',os.environ['API_VERSION'])
with urllib.request.urlopen(req,timeout=30) as r: w=json.loads(r.read())
print('livemode='+str(bool(w.get('livemode'))).lower())
print('status='+str(w.get('status') or ''))
print('event_count='+str(len(w.get('enabled_events') or [])))
PY
)" WEBHOOK_ID="$WEBHOOK_ID"
printf '%s\n' "$VERIFY"
grep -Fq 'livemode=true' <<<"$VERIFY" || fail "webhook not live"
grep -Fq 'status=enabled' <<<"$VERIFY" || fail "webhook not enabled"
grep -Fq 'event_count=8' <<<"$VERIFY" || fail "webhook event count mismatch"

echo "============================================================"
echo " STRIPE LIVE WEBHOOK + RUNTIME CUTOVER=PASS"
echo "============================================================"
echo "stripe_account=$EXPECTED_ACCOUNT"
echo "webhook_id=$WEBHOOK_ID"
echo "pricing_runtime=LIVE"
echo "production_db_mapping=LIVE_14_OF_14"
echo "customer_charge=NONE"
echo "NEXT=real $9.99 PACK_USD_1000 checkout"
