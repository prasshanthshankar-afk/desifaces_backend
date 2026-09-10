from __future__ import annotations

from app.fusion_execution_runtime import _is_internal_child_pricing_contract


def _payload(mode: str, *, enabled=False, quote_id=None, state="suppressed", suppressed=True):
    return {
        "pricing": {
            "enabled": enabled,
            "state": state,
            "suppressed": suppressed,
            "pricing_suppressed": suppressed,
            "billing_mode": mode,
            "quote_id": quote_id,
        }
    }


def test_route_level_internal_mode_is_suppressed() -> None:
    assert _is_internal_child_pricing_contract(_payload("internal")) is True


def test_persisted_orchestrator_internal_child_mode_is_suppressed() -> None:
    assert _is_internal_child_pricing_contract(_payload("internal_child")) is True


def test_billable_child_response_fails_closed() -> None:
    assert _is_internal_child_pricing_contract(_payload("wallet", enabled=True, suppressed=False)) is False


def test_suppressed_child_must_not_carry_quote() -> None:
    assert _is_internal_child_pricing_contract(_payload("internal_child", quote_id="unexpected-quote")) is False


def test_suppressed_child_must_be_in_suppressed_state() -> None:
    assert _is_internal_child_pricing_contract(_payload("internal_child", state="reserved")) is False
