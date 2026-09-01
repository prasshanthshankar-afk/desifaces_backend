from __future__ import annotations

import asyncio

from desifaces_shared.pricing.models import PricingReserveRequest

from app.domain.models import FusionJobCreate
from app.services.pricing_confirmation_policy import (
    _ConfirmationPricingClientProxy,
    _confirmation,
    _request_payload,
)


def _request() -> FusionJobCreate:
    return FusionJobCreate.model_validate(
        {
            "face_artifact_id": "11111111-1111-4111-8111-111111111111",
            "voice_mode": "audio",
            "voice_audio": {
                "type": "audio",
                "audio_artifact_id": "22222222-2222-4222-8222-222222222222",
            },
            "video": {"aspect_ratio": "9:16", "duration_sec": 6},
            "provider": "omnihuman_v15",
            "consent": {"external_provider_ok": True},
            "pricing_confirmation": {
                "quote_id": "qt_example",
                "preview_fingerprint": "fp_example",
            },
        }
    )


def test_confirmation_survives_model_validation_but_is_excluded_from_preview_payload() -> None:
    req = _request()
    assert _confirmation(req) == ("qt_example", "fp_example")
    assert req.model_dump()["pricing_confirmation"]["quote_id"] == "qt_example"
    payload = _request_payload(req)
    assert "pricing_confirmation" not in payload
    assert payload["face_artifact_id"] == "11111111-1111-4111-8111-111111111111"


def test_reserve_proxy_forwards_confirmed_quote_identity() -> None:
    class Client:
        enabled = True

        def __init__(self) -> None:
            self.received = None

        async def reserve(self, req):
            self.received = req
            return req

    client = Client()
    proxy = _ConfirmationPricingClientProxy(
        client,
        quote_id="qt_example",
        preview_fingerprint="fp_example",
    )
    reserve_req = PricingReserveRequest(
        user_id="33333333-3333-4333-8333-333333333333",
        service_name="svc-fusion",
        service_action="fusion.video.generate",
        sku_code="FUSION_TALKING_VIDEO",
        units="1",
        external_ref_id="44444444-4444-4444-8444-444444444444",
        idempotency_key="svc-fusion:test:reserve",
    )

    asyncio.run(proxy.reserve(reserve_req))
    assert client.received is not None
    assert client.received.quote_id == "qt_example"
    assert client.received.preview_fingerprint == "fp_example"


def test_confirmation_is_required_as_a_pair() -> None:
    req = _request()
    req.pricing_confirmation.preview_fingerprint = ""
    quote_id, fingerprint = _confirmation(req)
    assert quote_id == "qt_example"
    assert fingerprint == ""
