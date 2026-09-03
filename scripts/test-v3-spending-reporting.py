#!/usr/bin/env python3
from pathlib import Path

root = Path(__file__).resolve().parents[1]
service = (root / "services/svc-pricing/app/app/services/customer_spending_service.py").read_text()
route = (root / "services/svc-pricing/app/app/api/routes/spending.py").read_text()
main = (root / "services/svc-pricing/app/app/main.py").read_text()

required_service = [
    "pricing_credit_ledger_events",
    "payment_wallet_orders",
    "payment_gateway_checkout_sessions",
    "pricing_invoices",
    "event == \"consume\"",
    "Money paid is based on locally recorded completed payments",
    "credits_consumed_delta_percent",
    "top_category",
    "trend",
]
for marker in required_service:
    assert marker in service, marker

assert 'prefix="/api/pricing/me/spending"' in route
assert '@router.get("/summary")' in route
assert '@router.get("/transactions")' in route
assert "auth.user_id" in route
assert "spending_router" in main and "app.include_router(spending_router)" in main

# Privacy/financial integrity gates: user id is auth-derived; money paid and
# credits consumed stay separate. Reporting must never write to pricing/payment
# tables.
lower = service.lower()
for forbidden in (
    "insert into pricing_credit_ledger_events",
    "update pricing_credit_accounts",
    "delete from pricing_credit_ledger_events",
    "insert into payment_wallet_orders",
):
    assert forbidden not in lower, forbidden

print("V3_SPENDING_REPORTING_SOURCE_TEST=PASS")
