from __future__ import annotations

import asyncio
from uuid import uuid4

from app import fusion_execution_parent_pricing as parent_pricing
from app.fusion_execution_runtime import (
    _verify_suppressed_child_pricing_without_generation_consent,
)


def test_internal_child_pricing_preview_uses_ephemeral_consent_only() -> None:
    original_payload = {
        "provider": "veed_fabric",
        "consent": {"external_provider_ok": False},
        "provider_options": {"v3_request_nonce": "nonce-1"},
        "tags": {},
    }
    child = {
        "dialogue_turn_id": str(uuid4()),
        "participant_id": str(uuid4()),
        "display_name": "Speaker",
        "sequence_no": 1,
        "payload": original_payload,
        "retry_scope": "initial_scene",
    }
    stage_run_id = uuid4()

    class DummyService:
        async def _fusion_post(self, path, *, headers, payload):
            assert path == "/jobs/pricing/preview"
            assert payload["consent"]["external_provider_ok"] is True
            assert payload["pricing_suppressed"] is True
            assert payload["bill_to_parent"] is True
            return {
                "pricing": {
                    "enabled": False,
                    "state": "suppressed",
                    "suppressed": True,
                    "pricing_suppressed": True,
                    "billing_mode": "internal",
                    "amount": "0.00",
                },
                "pricing_summary": {"display_estimate": "0 credits"},
            }

    result = asyncio.run(
        _verify_suppressed_child_pricing_without_generation_consent(
            DummyService(),
            headers={"Authorization": "Bearer test"},
            child=child,
            stage_run_id=stage_run_id,
        )
    )

    assert result["pricing_suppressed"] is True
    assert result["pricing"]["state"] == "suppressed"
    # The durable/original generation payload remains unconsented. The helper
    # modifies only an ephemeral pricing-preview copy.
    assert original_payload["consent"]["external_provider_ok"] is False


def test_generation_path_still_requires_external_provider_consent() -> None:
    # The production child-generation helper is intentionally untouched by the
    # runtime patch and continues to stamp the original consent value.
    payload = parent_pricing._stamp_internal_child(
        {
            "provider": "veed_fabric",
            "consent": {"external_provider_ok": False},
            "provider_options": {"v3_request_nonce": "nonce-2"},
            "tags": {},
        },
        stage_run_id=uuid4(),
        dialogue_turn_id=str(uuid4()),
    )
    assert payload["consent"]["external_provider_ok"] is False
