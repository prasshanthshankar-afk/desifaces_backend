import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAYMENTS = ROOT / "services/svc-pricing/app/app/api/routes/payments.py"
GATEWAY = ROOT / "services/svc-pricing/app/app/services/gateways/stripe_gateway.py"
SYNC = ROOT / "services/svc-pricing/app/app/services/entitlement_sync_service.py"
WEBHOOKS = ROOT / "services/svc-pricing/app/app/api/routes/payment_webhooks.py"
RECONCILER = ROOT / "services/svc-pricing/app/app/services/entitlements/plan_credit_reconciliation_service.py"


def _function_body(source: str, name: str, next_name: str | None = None) -> str:
    start = source.find(f"async def {name}(")
    if start < 0:
        start = source.find(f"def {name}(")
    assert start >= 0, f"missing function {name}"
    if next_name:
        markers = [f"\nasync def {next_name}(", f"\ndef {next_name}("]
        ends = [source.find(marker, start + 1) for marker in markers]
        ends = [end for end in ends if end >= 0]
        end = min(ends) if ends else len(source)
    else:
        end = len(source)
    return source[start:end]


def test_existing_stripe_upgrade_changes_existing_subscription_instead_of_creating_second() -> None:
    src = PAYMENTS.read_text()
    body = _function_body(src, "change_subscription", "undo_pending_change")
    assert "if wants_checkout and not has_linked_subscription:" in body
    assert "if wants_checkout:" in body
    assert "gw.change_subscription_price(" in body
    assert 'payment_behavior="pending_if_incomplete"' in body
    assert "multiple live Stripe subscriptions for this account" in body
    assert "stripe_provider_subscription_invariant_failed" in body


def test_direct_subscription_checkout_is_new_subscription_only() -> None:
    src = PAYMENTS.read_text()
    body = _function_body(src, "create_subscription_checkout_session", "get_current_subscription")
    assert "direct_subscription_checkout_requires_no_active_subscription" in body
    assert "stripe_provider_live_subscription_exists" in body
    assert "_fetch_live_stripe_provider_subscriptions" in body


def test_gateway_uses_pending_update_payment_semantics_for_price_change() -> None:
    src = GATEWAY.read_text()
    body = _function_body(src, "change_subscription_price", "set_cancel_at_period_end")
    assert 'payment_behavior: str = "pending_if_incomplete"' in body
    assert '"payment_behavior": payment_behavior' in body
    assert '"proration_behavior": proration_behavior' in body


def test_canonical_subscription_ownership_is_sticky_and_inactive_old_events_cannot_demote_it() -> None:
    src = SYNC.read_text()
    selector = _function_body(src, "_select_canonical_active_subscription_id", "_load_plan_from_guardrails")
    assert "metadata_json->>'gateway_subscription_id'" in selector
    assert "billing_entitlements" in selector
    sync_body = _function_body(src, "sync_subscription_and_entitlement")
    assert "if canonical_subscription_id and canonical_subscription_id != gateway_subscription_id:" in sync_body
    assert "return user_id" in sync_body
    assert 'if entitlement_state in {"active", "grace"} and canonical_subscription_id' not in sync_body


def test_noncanonical_subscription_webhook_cannot_reconcile_plan_credits() -> None:
    src = WEBHOOKS.read_text()
    context = _function_body(src, "_fetch_plan_credit_reconciliation_context", "_reconcile_stripe_plan_credits_after_sync")
    reconcile = _function_body(src, "_reconcile_stripe_plan_credits_after_sync", "_sync_hydrated_subscription_or_raise")
    assert "canonical_gateway_subscription_id" in context
    assert '"skip_reconcile": True' in context
    assert "noncanonical_subscription_event" in context
    assert 'if ctx.get("skip_reconcile"):' in reconcile


def test_historical_missing_provider_objects_are_terminally_ignored_only_after_age_threshold() -> None:
    src = WEBHOOKS.read_text()
    assert "def _is_terminal_stale_provider_object" in src
    assert '"stripe_http_404"' in src
    assert '"resource_missing"' in src
    assert "24 * 60 * 60" in src
    assert 'status="ignored"' in src
    assert "stale_provider_object" in src


def test_canonical_display_uses_entitlement_total_before_stale_plan_snapshot() -> None:
    src = PAYMENTS.read_text()
    body = _function_body(src, "_build_canonical_billing_display", "_sha256_hex")
    ent_pos = body.find('ent.get("included_credits_total")')
    plan_pos = body.find('plan_json.get("included_credits_total")')
    assert ent_pos >= 0 and plan_pos >= 0 and ent_pos < plan_pos
    assert '"included_credits_total": plan_total' in body



def test_active_provider_price_identity_precedes_mutable_subscription_metadata() -> None:
    src = SYNC.read_text()
    resolver = _function_body(src, "_resolve_plan_code", "_extract_subscription_period_bounds")
    sync_body = _function_body(src, "sync_subscription_and_entitlement")
    assert "async def _resolve_plan_code(" in resolver
    assert "public.pricing_plan_prices" in resolver
    assert 'stripe_price_id = $1' in resolver
    price_pos = resolver.find("public.pricing_plan_prices")
    metadata_pos = resolver.find('metadata.get("df_plan_code")')
    assert price_pos >= 0 and metadata_pos >= 0 and price_pos < metadata_pos
    assert "incoming_plan_code = await _resolve_plan_code(conn, subscription, metadata)" in sync_body

def test_free_plan_fallback_is_zero_credits_everywhere() -> None:
    sync_src = SYNC.read_text()
    reconciler_src = RECONCILER.read_text()
    payments_src = PAYMENTS.read_text()
    assert 'included_credits_total=0' in sync_src
    assert 'included_credits_total=100' not in _function_body(sync_src, "_load_plan_from_guardrails", "_effective_tier_code")
    assert 'Decimal("100") if normalized_plan_code == "free"' not in reconciler_src
    assert 'cap = Decimal("0")' in reconciler_src
    pending = _function_body(payments_src, "_pending_change_from_subscription_row", "_undo_pending_change_message")
    assert 'target_total_credits=0' in pending
