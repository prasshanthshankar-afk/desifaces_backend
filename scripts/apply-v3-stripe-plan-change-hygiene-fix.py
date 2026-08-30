#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()

PAYMENTS = ROOT / "services/svc-pricing/app/app/api/routes/payments.py"
GATEWAY = ROOT / "services/svc-pricing/app/app/services/gateways/stripe_gateway.py"
SYNC = ROOT / "services/svc-pricing/app/app/services/entitlement_sync_service.py"
WEBHOOKS = ROOT / "services/svc-pricing/app/app/api/routes/payment_webhooks.py"
RECONCILER = ROOT / "services/svc-pricing/app/app/services/entitlements/plan_credit_reconciliation_service.py"
WORKFLOW = ROOT / ".github/workflows/v3-contract-tests.yml"
TEST = ROOT / "test/test_v3_pricing_stripe_plan_change_hygiene.py"


def fail(message: str) -> None:
    raise RuntimeError(message)


def read(path: Path) -> str:
    if not path.exists():
        fail(f"required_file_missing:{path.relative_to(ROOT)}")
    return path.read_text()


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = read(path)
    if new in text:
        print(f"SKIP_ALREADY_PATCHED {label}")
        return
    count = text.count(old)
    if count != 1:
        fail(f"replace_anchor_count:{label}:{count}")
    path.write_text(text.replace(old, new, 1))
    print(f"PATCHED {label}")


def insert_before(path: Path, marker: str, payload: str, sentinel: str, label: str) -> None:
    text = read(path)
    if sentinel in text:
        print(f"SKIP_ALREADY_PATCHED {label}")
        return
    count = text.count(marker)
    if count != 1:
        fail(f"insert_anchor_count:{label}:{count}")
    path.write_text(text.replace(marker, payload + marker, 1))
    print(f"PATCHED {label}")


def ensure_test_file() -> None:
    content = r'''import re
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
    assert '"multiple_live_stripe_subscriptions"' in body
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
    assert re.search(
        r"if\s+canonical_subscription_id\s+and\s+canonical_subscription_id\s*!=\s*gateway_subscription_id\s*:\s*\n\s*return user_id",
        sync_body,
    )
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
'''
    if TEST.exists():
        current = TEST.read_text()
        if current != content:
            fail(f"test_file_exists_with_different_content:{TEST.relative_to(ROOT)}")
        print("SKIP_ALREADY_PATCHED focused_test")
        return
    TEST.write_text(content)
    print("PATCHED focused_test")


def main() -> None:
    if not (ROOT / ".git").exists():
        fail(f"not_git_workspace:{ROOT}")

    # 1. Stripe gateway: provider inventory + safe existing-subscription price change.
    insert_before(
        GATEWAY,
        "\n    async def retrieve_subscription(self, subscription_id: str) -> Dict[str, Any]:\n",
        '''\n    async def list_subscriptions(\n        self,\n        *,\n        customer_id: str,\n        status: str = "all",\n        limit: int = 100,\n    ) -> Dict[str, Any]:\n        return await self._request(\n            "GET",\n            "/v1/subscriptions",\n            params={"customer": customer_id, "status": status, "limit": limit},\n        )\n''',
        "async def list_subscriptions(",
        "gateway_list_subscriptions",
    )
    replace_once(
        GATEWAY,
        '''    async def change_subscription_price(\n        self,\n        *,\n        subscription_id: str,\n        subscription_item_id: str,\n        new_price_id: str,\n        proration_behavior: str = "always_invoice",\n        idempotency_key: Optional[str] = None,\n        metadata: Optional[Dict[str, Any]] = None,\n    ) -> Dict[str, Any]:\n        form: Dict[str, Any] = {\n            "items": [{"id": subscription_item_id, "price": new_price_id}],\n            "proration_behavior": proration_behavior,\n        }\n''',
        '''    async def change_subscription_price(\n        self,\n        *,\n        subscription_id: str,\n        subscription_item_id: str,\n        new_price_id: str,\n        proration_behavior: str = "always_invoice",\n        payment_behavior: str = "pending_if_incomplete",\n        idempotency_key: Optional[str] = None,\n        metadata: Optional[Dict[str, Any]] = None,\n    ) -> Dict[str, Any]:\n        form: Dict[str, Any] = {\n            "items": [{"id": subscription_item_id, "price": new_price_id}],\n            "proration_behavior": proration_behavior,\n            "payment_behavior": payment_behavior,\n        }\n''',
        "gateway_pending_update_semantics",
    )

    # 2. Payments API: provider/local live-subscription invariants.
    insert_before(
        PAYMENTS,
        "\n\nasync def _get_latest_subscription_row(conn, *, user_id: UUID):\n",
        '''\n\nasync def _get_live_stripe_subscription_rows(conn, *, user_id: UUID) -> List[Dict[str, Any]]:\n    rows = await conn.fetch(\n        """\n        select\n          user_id,\n          gateway_provider,\n          gateway_customer_id,\n          gateway_subscription_id,\n          gateway_price_id,\n          plan_code,\n          subscription_state,\n          entitlement_state,\n          cancel_at_period_end,\n          current_period_start,\n          current_period_end,\n          created_at,\n          updated_at\n        from payment_plan_subscriptions\n        where user_id = $1\n          and gateway_provider = 'stripe'\n          and gateway_subscription_id is not null\n          and subscription_state in ('trialing', 'active', 'past_due', 'unpaid', 'paused')\n          and entitlement_state in ('active', 'grace')\n        order by updated_at desc, created_at desc\n        """,\n        user_id,\n    )\n    return [dict(row) for row in rows]\n\n\ndef _stripe_provider_subscription_is_live(subscription: Any) -> bool:\n    state = str(_record_get(subscription, "status", "") or "").strip().lower()\n    return state in {"trialing", "active", "past_due", "unpaid", "paused"}\n\n\nasync def _fetch_live_stripe_provider_subscriptions(\n    gw: StripeGateway,\n    *,\n    customer_id: str,\n) -> List[Dict[str, Any]]:\n    payload = await gw.list_subscriptions(customer_id=customer_id, status="all", limit=100)\n    return [\n        dict(item)\n        for item in (payload.get("data") or [])\n        if isinstance(item, dict) and _stripe_provider_subscription_is_live(item)\n    ]\n''',
        "async def _get_live_stripe_subscription_rows(",
        "payments_live_subscription_helpers",
    )

    insert_before(
        PAYMENTS,
        '''        purpose, current_plan_code = await _determine_subscription_purpose(\n            conn,\n            user_id=auth.user_id,\n            requested_plan_code=plan_code,\n        )\n''',
        '''        current_active = await _get_current_active_subscription(conn, user_id=auth.user_id)\n        if current_active:\n            provider = str(current_active.get("gateway_provider") or "").strip().lower()\n            raise HTTPException(\n                status_code=409,\n                detail={\n                    "code": "direct_subscription_checkout_requires_no_active_subscription",\n                    "message": (\n                        "This account already has a live subscription. Use the subscription-change endpoint "\n                        "so the existing provider subscription is modified instead of creating another one."\n                    ),\n                    "gateway_provider": provider or None,\n                    "current_plan_code": str(current_active.get("plan_code") or "") or None,\n                    "gateway_subscription_id": str(current_active.get("gateway_subscription_id") or "") or None,\n                },\n            )\n\n''',
        "direct_subscription_checkout_requires_no_active_subscription",
        "direct_checkout_live_subscription_guard",
    )

    replace_once(
        PAYMENTS,
        '''        customer = await _sync_customer_row(\n            conn,\n            user_id=auth.user_id,\n            email=None,\n            gw=gw,\n            idempotency_key=f"stripe-customer-sync:{auth.user_id}",\n        )\n\n        try:\n            session = await gw.create_subscription_checkout_session(\n''',
        '''        customer = await _sync_customer_row(\n            conn,\n            user_id=auth.user_id,\n            email=None,\n            gw=gw,\n            idempotency_key=f"stripe-customer-sync:{auth.user_id}",\n        )\n        try:\n            provider_live = await _fetch_live_stripe_provider_subscriptions(\n                gw,\n                customer_id=customer["gateway_customer_id"],\n            )\n        except StripeGatewayError as exc:\n            raise HTTPException(status_code=502, detail=str(exc))\n        if provider_live:\n            raise HTTPException(\n                status_code=409,\n                detail={\n                    "code": "stripe_provider_live_subscription_exists",\n                    "message": (\n                        "Stripe already has a live subscription for this customer. "\n                        "Use the subscription-change endpoint instead of creating a second subscription."\n                    ),\n                    "gateway_subscription_ids": [str(item.get("id") or "") for item in provider_live],\n                },\n            )\n\n        try:\n            session = await gw.create_subscription_checkout_session(\n''',
        "provider_live_guard_before_checkout",
    )

    replace_once(
        PAYMENTS,
        '''        target_plan = await _resolve_plan_async(conn, inp.target_plan_code, country_code=country_code)\n        target_plan_code = target_plan["plan_code"]\n        current_row = await _get_latest_subscription_row(conn, user_id=auth.user_id)\n        current_plan_code = await _resolve_current_plan_code(conn, user_id=auth.user_id)\n''',
        '''        target_plan = await _resolve_plan_async(conn, inp.target_plan_code, country_code=country_code)\n        target_plan_code = target_plan["plan_code"]\n        current_row = await _get_latest_subscription_row(conn, user_id=auth.user_id)\n        current_plan_code = await _resolve_current_plan_code(conn, user_id=auth.user_id)\n        live_stripe_rows = await _get_live_stripe_subscription_rows(conn, user_id=auth.user_id)\n''',
        "change_route_load_live_stripe_rows",
    )

    insert_before(
        PAYMENTS,
        '''    if (not has_linked_subscription) and current_rank > _plan_rank_value("free") and target_rank <= current_rank:\n''',
        '''    if len(live_stripe_rows) > 1:\n        return SubscriptionMutationOut(\n            status="manual_change_required",\n            current_plan_code=current_plan_code,\n            target_plan_code=target_plan_code,\n            change_mode=inp.change_mode or "immediate",\n            message=(\n                "Billing integrity check found multiple live Stripe subscriptions for this account. "\n                "No further plan change will be started until the duplicate provider state is reconciled."\n            ),\n        )\n\n''',
        "multiple live Stripe subscriptions for this account",
        "multiple_live_stripe_subscriptions_guard",
    )

    replace_once(
        PAYMENTS,
        '''    wants_checkout = target_rank > current_rank\n\n    if wants_checkout:\n        create_out = await create_subscription_checkout_session(\n            SubscriptionCreateIn(\n                plan_code=target_plan_code,\n                success_url=inp.success_url,\n                cancel_url=inp.cancel_url,\n                idempotency_key=inp.idempotency_key or f"sub-change:{auth.user_id}:{target_plan_code}",\n                credit_reset_acknowledged=bool(inp.credit_reset_acknowledged),\n                credit_reset_acknowledged_at=inp.credit_reset_acknowledged_at,\n                credit_reset_acknowledgement_text=inp.credit_reset_acknowledgement_text,\n            ),\n            auth=auth,\n            pool=pool,\n        )\n        return SubscriptionMutationOut(\n            status="checkout_required",\n            current_plan_code=current_plan_code,\n            target_plan_code=target_plan_code,\n            change_mode=inp.change_mode or "immediate",\n            checkout_url=create_out.checkout_url,\n            message="Redirect the user to checkout to complete this subscription change.",\n        )\n\n''',
        '''    wants_checkout = target_rank > current_rank\n\n    # First paid subscription: Checkout is allowed to create provider ownership.\n    if wants_checkout and not has_linked_subscription:\n        create_out = await create_subscription_checkout_session(\n            SubscriptionCreateIn(\n                plan_code=target_plan_code,\n                success_url=inp.success_url,\n                cancel_url=inp.cancel_url,\n                idempotency_key=inp.idempotency_key or f"sub-change:{auth.user_id}:{target_plan_code}",\n                credit_reset_acknowledged=bool(inp.credit_reset_acknowledged),\n                credit_reset_acknowledged_at=inp.credit_reset_acknowledged_at,\n                credit_reset_acknowledgement_text=inp.credit_reset_acknowledgement_text,\n            ),\n            auth=auth,\n            pool=pool,\n        )\n        return SubscriptionMutationOut(\n            status="checkout_required",\n            current_plan_code=current_plan_code,\n            target_plan_code=target_plan_code,\n            change_mode=inp.change_mode or "immediate",\n            checkout_url=create_out.checkout_url,\n            message="Redirect the user to checkout to start the first paid subscription.",\n        )\n\n    # Existing Stripe subscriber: change the price on the SAME subscription.\n    # Never create a second Stripe subscription for an upgrade.\n    if wants_checkout:\n        current = dict(current_row) if current_row else {}\n        if str(current.get("gateway_provider") or "").strip().lower() != "stripe":\n            return SubscriptionMutationOut(\n                status="manual_change_required",\n                current_plan_code=current_plan_code,\n                target_plan_code=target_plan_code,\n                change_mode=inp.change_mode or "immediate",\n                message="The current paid subscription is not Stripe-managed and cannot be changed through web billing.",\n            )\n\n        subscription_id = str(current.get("gateway_subscription_id") or "").strip()\n        if (\n            len(live_stripe_rows) != 1\n            or not subscription_id\n            or str(live_stripe_rows[0].get("gateway_subscription_id") or "") != subscription_id\n        ):\n            return SubscriptionMutationOut(\n                status="manual_change_required",\n                current_plan_code=current_plan_code,\n                target_plan_code=target_plan_code,\n                change_mode=inp.change_mode or "immediate",\n                message="Stripe subscription ownership is ambiguous; no provider mutation was attempted.",\n            )\n\n        gw = _gateway()\n        try:\n            provider_sub = await gw.retrieve_subscription(subscription_id)\n            provider_customer = provider_sub.get("customer")\n            if isinstance(provider_customer, dict):\n                provider_customer_id = str(provider_customer.get("id") or "").strip()\n            else:\n                provider_customer_id = str(provider_customer or current.get("gateway_customer_id") or "").strip()\n\n            if provider_customer_id:\n                provider_live = await _fetch_live_stripe_provider_subscriptions(\n                    gw,\n                    customer_id=provider_customer_id,\n                )\n                if len(provider_live) != 1 or str(provider_live[0].get("id") or "") != subscription_id:\n                    return SubscriptionMutationOut(\n                        status="manual_change_required",\n                        current_plan_code=current_plan_code,\n                        target_plan_code=target_plan_code,\n                        change_mode=inp.change_mode or "immediate",\n                        message="stripe_provider_subscription_invariant_failed: expected exactly one live provider subscription.",\n                    )\n\n            items = _as_dict_deep_loose(provider_sub.get("items")).get("data") or []\n            if len(items) != 1:\n                raise HTTPException(\n                    status_code=409,\n                    detail={\n                        "code": "stripe_subscription_item_invariant_failed",\n                        "message": "Expected exactly one recurring Stripe subscription item before changing plans.",\n                        "subscription_id": subscription_id,\n                        "item_count": len(items),\n                    },\n                )\n            subscription_item_id = str(_as_dict_deep_loose(items[0]).get("id") or "").strip()\n            new_price_id = str(target_plan.get("price_id") or "").strip()\n            if not subscription_item_id or not new_price_id:\n                raise HTTPException(status_code=409, detail="stripe_subscription_change_identity_missing")\n\n            changed = await gw.change_subscription_price(\n                subscription_id=subscription_id,\n                subscription_item_id=subscription_item_id,\n                new_price_id=new_price_id,\n                proration_behavior="always_invoice",\n                payment_behavior="pending_if_incomplete",\n                idempotency_key=inp.idempotency_key or f"sub-change:{auth.user_id}:{subscription_id}:{target_plan_code}",\n                metadata={\n                    "df_order_type": "plan_upgrade",\n                    "df_plan_code": target_plan_code,\n                    "df_currency": str(target_plan.get("currency") or "USD"),\n                    "df_user_id": str(auth.user_id),\n                    "df_service": "svc-pricing",\n                },\n            )\n        except StripeGatewayError as exc:\n            raise HTTPException(status_code=502, detail=str(exc))\n\n        return SubscriptionMutationOut(\n            status="processing",\n            current_plan_code=current_plan_code,\n            target_plan_code=target_plan_code,\n            change_mode=inp.change_mode or "immediate",\n            subscription_state=str(changed.get("status") or current.get("subscription_state") or "active"),\n            message=(\n                "Stripe plan change submitted against the existing subscription. "\n                "The new plan becomes authoritative only after Stripe webhook reconciliation confirms provider state."\n            ),\n        )\n\n''',
        "existing_subscription_upgrade_same_provider_object",
    )

    # 3. Canonical display: entitlement is authoritative; expose explicit denominator.
    replace_once(
        PAYMENTS,
        '''    plan_total_decimal = _first_decimal(\n        plan_json.get("included_credits_total"),\n        ent.get("included_credits_total"),\n        overview_credits.get("total_credits"),\n    )\n''',
        '''    plan_total_decimal = _first_decimal(\n        ent.get("included_credits_total"),\n        plan_json.get("included_credits_total"),\n        overview_credits.get("total_credits"),\n    )\n''',
        "canonical_display_entitlement_total_precedence",
    )
    replace_once(
        PAYMENTS,
        '''        "total_credits": plan_total,\n\n        # New explicit split used by product surfaces.\n''',
        '''        "total_credits": plan_total,\n        "included_credits_total": plan_total,\n\n        # New explicit split used by product surfaces.\n''',
        "canonical_display_explicit_included_total",
    )
    replace_once(
        PAYMENTS,
        '''            target_total_credits=100,\n''',
        '''            target_total_credits=0,\n''',
        "pending_cancellation_free_zero",
    )

    # 4. Entitlement ownership: sticky canonical subscription and Free=0 fallback.
    replace_once(
        SYNC,
        '''        order by\n          case when cancel_at_period_end = false then 0 else 1 end,\n          updated_at desc,\n          created_at desc\n''',
        '''        order by\n          case\n            when gateway_subscription_id = (\n              select be.metadata_json->>'gateway_subscription_id'\n              from billing_entitlements be\n              where be.user_id = $1\n                and be.effective_from <= now()\n                and (be.effective_to is null or be.effective_to > now())\n              order by be.effective_from desc, be.updated_at desc\n              limit 1\n            ) then 0 else 1\n          end,\n          case when cancel_at_period_end = false then 0 else 1 end,\n          updated_at desc,\n          created_at desc\n''',
        "sticky_canonical_subscription_selector",
    )
    replace_once(
        SYNC,
        '''    if entitlement_state in {"active", "grace"} and canonical_subscription_id and canonical_subscription_id != gateway_subscription_id:\n        return user_id\n''',
        '''    if canonical_subscription_id and canonical_subscription_id != gateway_subscription_id:\n        # This provider row is lifecycle history, not the canonical entitlement owner.\n        # In particular, an old subscription cancellation/update must never demote\n        # a newer active subscription to Free or re-key its included-credit cycle.\n        return user_id\n''',
        "noncanonical_subscription_cannot_demote_entitlement",
    )
    replace_once(
        SYNC,
        '''                included_credits_total=100,\n''',
        '''                included_credits_total=0,\n''',
        "free_guardrail_fallback_zero",
    )
    replace_once(
        RECONCILER,
        '''    if cap is None:\n        cap = Decimal("100") if normalized_plan_code == "free" else Decimal("0")\n''',
        '''    if cap is None:\n        cap = Decimal("0")\n''',
        "reconciler_free_fallback_zero",
    )

    # 5. Webhook hygiene: stale noncanonical subscriptions cannot touch credits,
    # and old 404 provider objects become terminal ignored events only after 24h.
    replace_once(
        WEBHOOKS,
        '''import json\nimport os\nimport urllib.request\n''',
        '''import json\nimport os\nimport time\nimport urllib.request\n''',
        "webhook_import_time",
    )
    insert_before(
        WEBHOOKS,
        "\n\ndef _notifications_base_url() -> str:\n",
        '''\n\ndef _is_terminal_stale_provider_object(event: Dict[str, Any], exc: Exception) -> bool:\n    message = str(exc)\n    if (\n        "stripe_subscription_hydration_failed" not in message\n        or "stripe_http_404" not in message\n        or "resource_missing" not in message\n    ):\n        return False\n    try:\n        created = int(event.get("created") or 0)\n    except Exception:\n        return False\n    if created <= 0:\n        return False\n    return (int(time.time()) - created) >= (24 * 60 * 60)\n''',
        "def _is_terminal_stale_provider_object",
        "historical_provider_object_classifier",
    )
    replace_once(
        WEBHOOKS,
        '''    gateway_subscription_id = str(\n        sub_dict.get("gateway_subscription_id") or subscription_id or ""\n    ).strip()\n    if not gateway_subscription_id or period_start is None or period_end is None:\n        raise RuntimeError("stripe_plan_credit_cycle_identity_missing")\n''',
        '''    gateway_subscription_id = str(\n        sub_dict.get("gateway_subscription_id") or subscription_id or ""\n    ).strip()\n    entitlement_metadata = _as_dict_loose(ent_dict.get("metadata_json"))\n    canonical_gateway_subscription_id = str(\n        entitlement_metadata.get("gateway_subscription_id") or ""\n    ).strip()\n    if (\n        canonical_gateway_subscription_id\n        and gateway_subscription_id\n        and canonical_gateway_subscription_id != gateway_subscription_id\n    ):\n        return {\n            "user_id": user_id,\n            "plan_code": plan_code,\n            "tier_code": tier_code,\n            "included_credit_cap": included_cap,\n            "skip_reconcile": True,\n            "skip_reason": "noncanonical_subscription_event",\n            "canonical_gateway_subscription_id": canonical_gateway_subscription_id,\n            "subscription": sub_dict,\n            "entitlement": ent_dict,\n        }\n    if not gateway_subscription_id or period_start is None or period_end is None:\n        raise RuntimeError("stripe_plan_credit_cycle_identity_missing")\n''',
        "noncanonical_webhook_reconcile_context_guard",
    )
    replace_once(
        WEBHOOKS,
        '''    sub = ctx.get("subscription") or {}\n    ent = ctx.get("entitlement") or {}\n    metadata_json = {\n''',
        '''    sub = ctx.get("subscription") or {}\n    ent = ctx.get("entitlement") or {}\n    if ctx.get("skip_reconcile"):\n        return {\n            "action": "plan_credit_reconcile_skipped",\n            "reason": str(ctx.get("skip_reason") or "noncanonical_subscription_event"),\n            "subscription_id": str(sub.get("gateway_subscription_id") or subscription_id or ""),\n            "canonical_gateway_subscription_id": str(ctx.get("canonical_gateway_subscription_id") or ""),\n        }\n    metadata_json = {\n''',
        "noncanonical_webhook_reconcile_skip",
    )
    replace_once(
        WEBHOOKS,
        '''        except Exception as exc:\n            await _mark_webhook_status(conn, event_id=event_id, status="failed", failure_reason=str(exc))\n            raise HTTPException(status_code=500, detail=f"webhook_processing_failed:{exc}")\n''',
        '''        except Exception as exc:\n            if _is_terminal_stale_provider_object(event, exc):\n                await _mark_webhook_status(\n                    conn,\n                    event_id=event_id,\n                    status="ignored",\n                    failure_reason=f"stale_provider_object:{exc}",\n                )\n                return {\n                    "ok": True,\n                    "event_id": event_id,\n                    "event_type": event_type,\n                    "ignored": True,\n                    "reason": "stale_provider_object",\n                }\n            await _mark_webhook_status(conn, event_id=event_id, status="failed", failure_reason=str(exc))\n            raise HTTPException(status_code=500, detail=f"webhook_processing_failed:{exc}")\n''',
        "historical_missing_provider_object_terminal_ignore",
    )

    # 6. CI: compile all modified modules and execute focused regression file.
    replace_once(
        WORKFLOW,
        '''            services/svc-pricing/app/app/api/routes/payment_webhooks.py \\\n            services/svc-pricing/app/app/services/entitlements/plan_credit_reconciliation_service.py \\\n''',
        '''            services/svc-pricing/app/app/api/routes/payment_webhooks.py \\\n            services/svc-pricing/app/app/services/gateways/stripe_gateway.py \\\n            services/svc-pricing/app/app/services/entitlement_sync_service.py \\\n            services/svc-pricing/app/app/services/entitlements/plan_credit_reconciliation_service.py \\\n''',
        "workflow_compile_stripe_hygiene_modules",
    )
    replace_once(
        WORKFLOW,
        '''            test/test_v3_pricing_credit_cycle_isolation.py \\\n            test/test_v3_multiperson_story_foundation.py \\\n''',
        '''            test/test_v3_pricing_credit_cycle_isolation.py \\\n            test/test_v3_pricing_stripe_plan_change_hygiene.py \\\n            test/test_v3_multiperson_story_foundation.py \\\n''',
        "workflow_run_stripe_hygiene_test",
    )

    ensure_test_file()

    compile_targets = [
        PAYMENTS,
        GATEWAY,
        SYNC,
        WEBHOOKS,
        RECONCILER,
        TEST,
    ]
    subprocess.run(
        [sys.executable, "-m", "py_compile", *[str(path) for path in compile_targets]],
        cwd=ROOT,
        check=True,
    )
    print("STATIC_COMPILE=PASS")
    print("STRIPE_PLAN_CHANGE_HYGIENE_PATCH=PASS")


if __name__ == "__main__":
    main()
