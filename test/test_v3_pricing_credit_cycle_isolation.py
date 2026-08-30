import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECONCILER = ROOT / "services/svc-pricing/app/app/services/entitlements/plan_credit_reconciliation_service.py"
WEBHOOKS = ROOT / "services/svc-pricing/app/app/api/routes/payment_webhooks.py"


def _function_body(source: str, name: str, next_name: str | None = None) -> str:
    marker = f"def {name}("
    async_marker = f"async def {name}("
    start = source.find(async_marker)
    if start < 0:
        start = source.find(marker)
    assert start >= 0, f"missing function {name}"
    if next_name:
        next_markers = [f"\ndef {next_name}(", f"\nasync def {next_name}("]
        ends = [source.find(m, start + 1) for m in next_markers]
        ends = [x for x in ends if x >= 0]
        end = min(ends) if ends else len(source)
    else:
        end = len(source)
    return source[start:end]


def test_stripe_webhook_uses_exact_subscription_period_cycle_identity() -> None:
    src = WEBHOOKS.read_text()
    assert "from desifaces_shared.v3.subscription_cycle import stripe_cycle_key as canonical_stripe_cycle_key" in src
    body = _function_body(src, "_fetch_plan_credit_reconciliation_context", "_reconcile_stripe_plan_credits_after_sync")
    assert re.search(
        r"canonical_stripe_cycle_key\(\s*gateway_subscription_id\s*,\s*period_start\s*,\s*period_end\s*\)",
        body,
    )
    assert "stripe_plan_credit_cycle_identity_missing" in body
    assert "_metadata_cycle_key(ent_dict.get(\"metadata_json\"))" not in body
    assert "_metadata_cycle_key(sub_dict.get(\"metadata_json\"))" not in body


def test_current_cycle_totals_exclude_expired_and_unkeyed_legacy_lots() -> None:
    src = RECONCILER.read_text()
    body = _function_body(src, "_fetch_active_included_cycle_totals", "_expire_previous_cycle_included_lots")
    assert "and (expires_at is null or expires_at > now())" in body
    assert "and metadata_json->>'cycle_key' = $2::text" in body
    assert "coalesce(metadata_json->>'cycle_key', $2::text) = $2::text" not in body


def test_rollover_expires_stale_legacy_lots_before_current_cycle_math() -> None:
    src = RECONCILER.read_text()
    body = _function_body(src, "_expire_previous_cycle_included_lots", "_adopt_legacy_included_lots_for_cycle")
    assert "expires_at <= now()" in body
    assert "coalesce(metadata_json, '{}'::jsonb) ? 'cycle_key'" in body
    assert "coalesce(granted_at, created_at) < $3::timestamptz" in body


def test_legacy_adoption_is_null_safe_unexpired_and_period_bounded() -> None:
    src = RECONCILER.read_text()
    body = _function_body(src, "_adopt_legacy_included_lots_for_cycle", "_reduce_included_unspent")
    assert "not (coalesce(metadata_json, '{}'::jsonb) ? 'cycle_key')" in body
    assert "and (expires_at is null or expires_at > now())" in body
    assert "coalesce(granted_at, created_at) >= $5::timestamptz" in body


def test_downgrade_reduction_targets_only_current_unexpired_cycle() -> None:
    src = RECONCILER.read_text()
    body = _function_body(src, "_reduce_included_unspent", "reconcile_included_plan_credits")
    assert "and (expires_at is null or expires_at > now())" in body
    assert "and metadata_json->>'cycle_key' = $2::text" in body
