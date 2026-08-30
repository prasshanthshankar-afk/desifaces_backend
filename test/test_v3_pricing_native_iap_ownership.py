from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, Optional


PAYMENTS_PATH = Path("services/svc-pricing/app/app/api/routes/payments.py")
TARGETS = {
    "_subscription_row_is_live",
    "_is_apple_managed_subscription",
    "_is_google_play_managed_subscription",
    "_is_native_iap_managed_subscription",
}


def _load_ownership_helpers():
    tree = ast.parse(PAYMENTS_PATH.read_text(encoding="utf-8"), filename=str(PAYMENTS_PATH))
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in TARGETS
    ]
    assert {node.name for node in selected} == TARGETS

    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "Any": Any,
        "Dict": Dict,
        "Optional": Optional,
        "_subscription_provider": lambda row: str((row or {}).get("gateway_provider") or "").strip().lower(),
    }
    exec(compile(module, str(PAYMENTS_PATH), "exec"), namespace)
    return namespace


def _row(provider: str, subscription_state: str, entitlement_state: str = "inactive") -> dict[str, object]:
    return {
        "gateway_provider": provider,
        "subscription_state": subscription_state,
        "entitlement_state": entitlement_state,
        "cancel_at_period_end": False,
    }


def test_canceled_apple_history_does_not_own_new_web_billing() -> None:
    helpers = _load_ownership_helpers()
    row = _row("apple_iap", "canceled", "inactive")
    assert helpers["_subscription_row_is_live"](row) is False
    assert helpers["_is_apple_managed_subscription"](row) is False
    assert helpers["_is_native_iap_managed_subscription"](row) is False


def test_canceled_google_history_does_not_own_new_web_billing() -> None:
    helpers = _load_ownership_helpers()
    row = _row("google_play", "canceled", "inactive")
    assert helpers["_subscription_row_is_live"](row) is False
    assert helpers["_is_google_play_managed_subscription"](row) is False
    assert helpers["_is_native_iap_managed_subscription"](row) is False


def test_live_apple_subscription_remains_apple_managed() -> None:
    helpers = _load_ownership_helpers()
    row = _row("apple_iap", "active", "active")
    assert helpers["_subscription_row_is_live"](row) is True
    assert helpers["_is_apple_managed_subscription"](row) is True
    assert helpers["_is_native_iap_managed_subscription"](row) is True


def test_live_google_subscription_remains_google_managed() -> None:
    helpers = _load_ownership_helpers()
    row = _row("google_play", "active", "active")
    assert helpers["_subscription_row_is_live"](row) is True
    assert helpers["_is_google_play_managed_subscription"](row) is True
    assert helpers["_is_native_iap_managed_subscription"](row) is True
