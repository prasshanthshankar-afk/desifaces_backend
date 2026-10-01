#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
PRICING_CONTAINER="${PRICING_CONTAINER:-df-svc-pricing}"

for c in "$DB_CONTAINER" "$PRICING_CONTAINER"; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "FAIL: missing $c"; exit 2; }
done

DB_URL="$(docker exec "$PRICING_CONTAINER" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DB_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DB_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

echo "============================================================"
echo " desifaces DEV — DASHBOARD SPENDING DIAGNOSTIC"
echo " READ ONLY / NO MUTATION"
echo "============================================================"
echo "production=UNTOUCHED"
echo "generation_mutation=NONE"
echo "pricing_mutation=NONE"
echo "db_mutation=NONE"

echo
echo "===== 1. FIND SCREENSHOT ACCOUNT ====="
MATCHES="$("${PSQL[@]}" -AtF '|' -c "
select user_id::text,balance_credits,reserved_credits,(balance_credits-reserved_credits) available
from public.pricing_credit_accounts
where (balance_credits-reserved_credits)=11423
  and reserved_credits=1048
order by updated_at desc;
")"

if [[ -z "$MATCHES" ]]; then
  echo "SCREENSHOT_ACCOUNT_MATCH=NONE"
  echo "Recent highest-reserved accounts:"
  "${PSQL[@]}" -P pager=off -c "
  select user_id,balance_credits,reserved_credits,(balance_credits-reserved_credits) available,updated_at
  from public.pricing_credit_accounts
  order by reserved_credits desc,updated_at desc
  limit 10;
  "
  exit 3
fi

COUNT="$(printf '%s\n' "$MATCHES" | grep -c .)"
echo "SCREENSHOT_ACCOUNT_MATCHES=$COUNT"
printf '%s\n' "$MATCHES"

USER_ID="$(printf '%s\n' "$MATCHES" | head -1 | cut -d'|' -f1)"
echo "USER_ID=$USER_ID"

echo
echo "===== 2. OCTOBER CONSUME LEDGER ====="
"${PSQL[@]}" -P pager=off -c "
select created_at,event_type,credits_delta,sku_code,service_name,service_action,studio_job_id,idempotency_key
from public.pricing_credit_ledger_events
where user_id='$USER_ID'::uuid
  and created_at>=date_trunc('month',now())
order by created_at desc
limit 100;
"

OCT_CONSUMED="$("${PSQL[@]}" -Atq -c "
select coalesce(sum(abs(credits_delta)),0)
from public.pricing_credit_ledger_events
where user_id='$USER_ID'::uuid
  and created_at>=date_trunc('month',now())
  and lower(event_type)='consume'
  and credits_delta<0;
")"

SEP_CONSUMED="$("${PSQL[@]}" -Atq -c "
select coalesce(sum(abs(credits_delta)),0)
from public.pricing_credit_ledger_events
where user_id='$USER_ID'::uuid
  and created_at>=date_trunc('month',now())-interval '1 month'
  and created_at<date_trunc('month',now())
  and lower(event_type)='consume'
  and credits_delta<0;
")"

echo "CURRENT_MONTH_CONSUMED=$OCT_CONSUMED"
echo "PREVIOUS_MONTH_CONSUMED=$SEP_CONSUMED"

echo
echo "===== 3. CURRENT-MONTH RESERVATION / COMMIT TRUTH ====="
"${PSQL[@]}" -P pager=off -c "
select
  created_at,
  status,
  quote_json->>'variant_code' variant_code,
  reserved_credits,
  quote_json->>'total_credits' quoted_credits,
  quote_json->>'final_charged_credits' final_charged_credits,
  job_ref,
  finalized_at,
  expires_at
from public.pricing_credit_reservations
where user_id='$USER_ID'::uuid
  and created_at>=date_trunc('month',now())
order by created_at desc
limit 100;
"

COMMITTED_FINAL="$("${PSQL[@]}" -Atq -c "
select coalesce(sum(nullif(quote_json->>'final_charged_credits','')::numeric),0)
from public.pricing_credit_reservations
where user_id='$USER_ID'::uuid
  and created_at>=date_trunc('month',now())
  and status='committed';
")"

ACTIVE_RESERVED="$("${PSQL[@]}" -Atq -c "
select coalesce(sum(reserved_credits),0)
from public.pricing_credit_reservations
where user_id='$USER_ID'::uuid
  and status='reserved'
  and (expires_at is null or expires_at>now());
")"

STALE_RESERVED="$("${PSQL[@]}" -Atq -c "
select count(*)
from public.pricing_credit_reservations
where user_id='$USER_ID'::uuid
  and status='reserved'
  and created_at<now()-interval '30 minutes';
")"

echo "CURRENT_MONTH_COMMITTED_FINAL_CREDITS=$COMMITTED_FINAL"
echo "ACTIVE_RESERVATION_CREDITS=$ACTIVE_RESERVED"
echo "STALE_RESERVED_ROWS_OLDER_30M=$STALE_RESERVED"

echo
echo "===== 4. COMPLETED JOBS TODAY ====="
"${PSQL[@]}" -P pager=off -c "
select
  created_at,id,studio_type,status,
  coalesce(payload_json->'pricing'->>'state',meta_json->'pricing'->>'state') pricing_state,
  coalesce(payload_json->'pricing'->>'quote_id',meta_json->'pricing'->>'quote_id') quote_id,
  coalesce(payload_json->'pricing'->>'variant_code',meta_json->'pricing'->>'variant_code') variant_code
from public.studio_jobs
where user_id='$USER_ID'::uuid
  and created_at>=date_trunc('day',now())
order by created_at desc
limit 100;
"

echo
echo "===== 5. CURRENT-MONTH MONEY RECORDS ====="
for t in payment_wallet_orders payment_gateway_checkout_sessions pricing_invoices; do
  exists="$("${PSQL[@]}" -Atq -c "select case when to_regclass('public.$t') is null then 0 else 1 end;")"
  echo "TABLE=$t exists=$exists"
done

if "${PSQL[@]}" -Atq -c "select case when to_regclass('public.payment_wallet_orders') is null then 0 else 1 end;" | grep -qx 1; then
  "${PSQL[@]}" -P pager=off -c "
  select created_at,fulfilled_at,currency,amount_minor,credits_to_grant,gateway_provider,payment_state,fulfillment_state
  from public.payment_wallet_orders
  where user_id='$USER_ID'::uuid and created_at>=date_trunc('month',now())
  order by created_at desc;
  "
fi

if "${PSQL[@]}" -Atq -c "select case when to_regclass('public.payment_gateway_checkout_sessions') is null then 0 else 1 end;" | grep -qx 1; then
  "${PSQL[@]}" -P pager=off -c "
  select created_at,completed_at,gateway_provider,purpose,currency,amount_minor,status,local_subscription_id
  from public.payment_gateway_checkout_sessions
  where user_id='$USER_ID'::uuid
    and coalesce(completed_at,created_at)>=date_trunc('month',now())
  order by coalesce(completed_at,created_at) desc;
  "
fi

echo
echo "===== 6. DIAGNOSTIC VERDICT ====="
echo "current_month_consume_ledger=$OCT_CONSUMED"
echo "current_month_committed_final=$COMMITTED_FINAL"
echo "active_reserved=$ACTIVE_RESERVED"
echo "stale_reserved_rows=$STALE_RESERVED"

python3 - "$OCT_CONSUMED" "$COMMITTED_FINAL" "$SEP_CONSUMED" "$ACTIVE_RESERVED" "$STALE_RESERVED" <<'PY'
from decimal import Decimal
import sys

ledger=Decimal(sys.argv[1] or "0")
committed=Decimal(sys.argv[2] or "0")
previous=Decimal(sys.argv[3] or "0")
active=Decimal(sys.argv[4] or "0")
stale=int(Decimal(sys.argv[5] or "0"))

if committed > 0 and ledger == 0:
    print("DASHBOARD_USAGE_ROOT_CAUSE=COMMITTED_WITHOUT_VISIBLE_CONSUME_LEDGER")
elif committed > 0 and ledger != committed:
    print(f"DASHBOARD_USAGE_ROOT_CAUSE=LEDGER_COMMIT_MISMATCH ledger={ledger} committed={committed}")
elif ledger > 0:
    print("DASHBOARD_USAGE_LEDGER=HAS_CURRENT_MONTH_USAGE")
elif previous > 0:
    print("DASHBOARD_USAGE_ROOT_CAUSE=CALENDAR_MONTH_BOUNDARY")
else:
    print("DASHBOARD_USAGE_ROOT_CAUSE=NO_USAGE_EVIDENCE_IN_CURRENT_OR_PREVIOUS_MONTH")

if stale > 0:
    print("DASHBOARD_RESERVED_ROOT_CAUSE=STALE_RESERVATIONS_PRESENT")
elif active > 0:
    print("DASHBOARD_RESERVED_STATE=ACTIVE_RESERVATIONS_PRESENT")
else:
    print("DASHBOARD_RESERVED_STATE=NO_ACTIVE_RESERVATIONS")
PY

echo
echo "production=UNTOUCHED"
echo "generation_mutation=NONE"
echo "pricing_mutation=NONE"
echo "db_mutation=NONE"
echo "DASHBOARD_SPENDING_DIAGNOSTIC=COMPLETE"
