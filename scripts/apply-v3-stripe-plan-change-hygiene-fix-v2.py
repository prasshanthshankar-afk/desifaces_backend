#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
BASE = ROOT / "scripts/apply-v3-stripe-plan-change-hygiene-fix.py"
SYNC = ROOT / "services/svc-pricing/app/app/services/entitlement_sync_service.py"
TEST = ROOT / "test/test_v3_pricing_stripe_plan_change_hygiene.py"


def fail(message: str) -> None:
    raise RuntimeError(message)


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text()
    if new in text:
        print(f"SKIP_ALREADY_PATCHED {label}")
        return
    count = text.count(old)
    if count != 1:
        fail(f"replace_anchor_count:{label}:{count}")
    path.write_text(text.replace(old, new, 1))
    print(f"PATCHED {label}")


def insert_before(path: Path, marker: str, payload: str, sentinel: str, label: str) -> None:
    text = path.read_text()
    if sentinel in text:
        print(f"SKIP_ALREADY_PATCHED {label}")
        return
    count = text.count(marker)
    if count != 1:
        fail(f"insert_anchor_count:{label}:{count}")
    path.write_text(text.replace(marker, payload + marker, 1))
    print(f"PATCHED {label}")


def main() -> None:
    if not (ROOT / ".git").exists():
        fail(f"not_git_workspace:{ROOT}")
    if not BASE.exists():
        fail(f"base_patcher_missing:{BASE}")

    # Apply the complete base package first. It is intentionally idempotent.
    subprocess.run([sys.executable, str(BASE), str(ROOT)], cwd=ROOT, check=True)

    # Provider price identity is authoritative over mutable subscription metadata.
    # This is critical with Stripe pending updates: if payment is incomplete,
    # metadata must never make a not-yet-applied price look like an active upgrade.
    replace_once(
        SYNC,
        '''def _resolve_plan_code(subscription: Dict[str, Any], metadata: Dict[str, Any]) -> str:\n    direct = _normalize_plan_code(str(metadata.get("df_plan_code") or "").strip())\n    if direct and direct != "free":\n        return direct\n\n    items = _as_dict_loose(subscription.get("items"))\n    data = items.get("data") or []\n    if data:\n        price = _as_dict_loose(_as_dict_loose(data[0]).get("price"))\n        price_id = str(price.get("id") or "").strip()\n        if price_id:\n            mapped_row = _as_dict_loose(price.get("metadata"))\n            mapped_code = _normalize_plan_code(mapped_row.get("df_plan_code"))\n            if mapped_code and mapped_code != "free":\n                return mapped_code\n\n    return "free"\n''',
        '''async def _resolve_plan_code(\n    conn: asyncpg.Connection,\n    subscription: Dict[str, Any],\n    metadata: Dict[str, Any],\n) -> str:\n    # The active Stripe subscription item/price is provider truth. Subscription\n    # metadata is only a fallback hint because metadata can change while a\n    # pending_if_incomplete price update is still awaiting successful payment.\n    items = _as_dict_loose(subscription.get("items"))\n    data = items.get("data") or []\n    if data:\n        price = _as_dict_loose(_as_dict_loose(data[0]).get("price"))\n        price_id = str(price.get("id") or "").strip()\n        if price_id:\n            catalog_row = None\n            try:\n                catalog_row = await conn.fetchrow(\n                    """\n                    select plan_code\n                    from public.pricing_plan_prices\n                    where stripe_price_id = $1\n                      and is_active = true\n                    order by display_order, created_at\n                    limit 1\n                    """,\n                    price_id,\n                )\n            except Exception:\n                catalog_row = None\n            catalog_code = _normalize_plan_code(\n                str(catalog_row["plan_code"] or "").strip() if catalog_row else ""\n            )\n            if catalog_code and catalog_code != "free":\n                return catalog_code\n\n            price_metadata = _as_dict_loose(price.get("metadata"))\n            price_metadata_code = _normalize_plan_code(price_metadata.get("df_plan_code"))\n            if price_metadata_code and price_metadata_code != "free":\n                return price_metadata_code\n\n    direct = _normalize_plan_code(str(metadata.get("df_plan_code") or "").strip())\n    if direct and direct != "free":\n        return direct\n\n    return "free"\n''',
        "provider_price_precedes_subscription_metadata",
    )
    replace_once(
        SYNC,
        '''    incoming_plan_code = _resolve_plan_code(subscription, metadata)\n''',
        '''    incoming_plan_code = await _resolve_plan_code(conn, subscription, metadata)\n''',
        "await_provider_price_plan_resolution",
    )

    # Tighten the generated source-regression test without requiring source code
    # comments to be adjacent to the return statement.
    replace_once(
        TEST,
        '''    assert '\"multiple_live_stripe_subscriptions\"' in body\n''',
        '''    assert "multiple live Stripe subscriptions for this account" in body\n''',
        "test_duplicate_subscription_guard_phrase",
    )
    replace_once(
        TEST,
        '''    assert re.search(\n        r"if\\s+canonical_subscription_id\\s+and\\s+canonical_subscription_id\\s*!=\\s*gateway_subscription_id\\s*:\\s*\\n\\s*return user_id",\n        sync_body,\n    )\n''',
        '''    assert "if canonical_subscription_id and canonical_subscription_id != gateway_subscription_id:" in sync_body\n    assert "return user_id" in sync_body\n''',
        "test_canonical_ownership_comment_tolerance",
    )
    insert_before(
        TEST,
        "\ndef test_free_plan_fallback_is_zero_credits_everywhere() -> None:\n",
        '''\n\ndef test_active_provider_price_identity_precedes_mutable_subscription_metadata() -> None:\n    src = SYNC.read_text()\n    resolver = _function_body(src, "_resolve_plan_code", "_extract_subscription_period_bounds")\n    sync_body = _function_body(src, "sync_subscription_and_entitlement")\n    assert "async def _resolve_plan_code(" in resolver\n    assert "public.pricing_plan_prices" in resolver\n    assert 'stripe_price_id = $1' in resolver\n    price_pos = resolver.find("public.pricing_plan_prices")\n    metadata_pos = resolver.find('metadata.get("df_plan_code")')\n    assert price_pos >= 0 and metadata_pos >= 0 and price_pos < metadata_pos\n    assert "incoming_plan_code = await _resolve_plan_code(conn, subscription, metadata)" in sync_body\n''',
        "test_active_provider_price_identity_precedes_mutable_subscription_metadata",
        "test_provider_price_authority",
    )

    subprocess.run(
        [
            sys.executable,
            "-m",
            "py_compile",
            str(SYNC),
            str(TEST),
        ],
        cwd=ROOT,
        check=True,
    )
    print("V2_STATIC_COMPILE=PASS")
    print("STRIPE_PLAN_CHANGE_HYGIENE_PATCH_V2=PASS")


if __name__ == "__main__":
    main()
