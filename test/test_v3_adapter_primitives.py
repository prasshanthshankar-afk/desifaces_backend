from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from services.shared.df_contracts.v3.adapters import (
    derive_idempotency_key,
    make_api_error,
    make_request_context,
    normalize_error_code,
    normalize_job_state,
)
from services.shared.df_contracts.v3.common import ActorType, ErrorCode
from services.shared.df_contracts.v3.domain import JobState


def test_normalize_job_state_aliases() -> None:
    assert normalize_job_state("accepted") is JobState.SUBMITTED
    assert normalize_job_state("pending") is JobState.QUEUED
    assert normalize_job_state("in-progress") is JobState.RUNNING
    assert normalize_job_state("completed") is JobState.SUCCEEDED
    assert normalize_job_state("safety blocked") is JobState.BLOCKED
    assert normalize_job_state("cancelled") is JobState.CANCELED
    assert normalize_job_state("timed out") is JobState.EXPIRED


def test_normalize_job_state_unknown_uses_selected_default() -> None:
    assert normalize_job_state("provider_mystery") is JobState.SUBMITTED
    assert (
        normalize_job_state("provider_mystery", default=JobState.RUNNING)
        is JobState.RUNNING
    )


def test_derived_idempotency_key_is_stable_across_mapping_order() -> None:
    actor_id = uuid4()
    a = derive_idempotency_key(
        scope="face.generate",
        actor_id=actor_id,
        payload={"mode": "text-to-image", "variants": 2},
    )
    b = derive_idempotency_key(
        scope="face.generate",
        actor_id=actor_id,
        payload={"variants": 2, "mode": "text-to-image"},
    )

    assert a == b
    assert a.startswith("v3:face.generate:")
    assert 8 <= len(a) <= 200


def test_explicit_idempotency_key_is_preserved() -> None:
    explicit = "mobile-request-12345"
    result = derive_idempotency_key(
        scope="audio.tts",
        actor_id=uuid4(),
        payload={"text": "hello"},
        explicit_key=explicit,
    )
    assert result == explicit


def test_short_explicit_idempotency_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="at_least_8"):
        derive_idempotency_key(
            scope="fusion.generate",
            actor_id=uuid4(),
            payload={},
            explicit_key="short",
        )


def test_make_request_context_propagates_actor_and_ids() -> None:
    actor_id = uuid4()
    account_id = uuid4()
    request_id = uuid4()
    correlation_id = uuid4()
    requested_at = datetime.now(timezone.utc)

    ctx = make_request_context(
        actor_id=actor_id,
        actor_type=ActorType.USER,
        account_id=account_id,
        roles=("creator",),
        scopes=("face:generate",),
        request_id=request_id,
        correlation_id=correlation_id,
        idempotency_key="request-key-123",
        client_app="desifaces-mobile",
        client_version="3.0.0",
        requested_at=requested_at,
    )

    assert ctx.request_id == request_id
    assert ctx.correlation_id == correlation_id
    assert ctx.actor.actor_id == actor_id
    assert ctx.actor.account_id == account_id
    assert ctx.actor.roles == ("creator",)
    assert ctx.actor.scopes == ("face:generate",)
    assert ctx.idempotency_key == "request-key-123"
    assert ctx.client_app == "desifaces-mobile"
    assert ctx.client_version == "3.0.0"
    assert ctx.requested_at == requested_at


@pytest.mark.parametrize(
    ("http_status", "error_code", "message", "expected"),
    [
        (400, "DF_UNSAFE_PROMPT", "unsafe prompt", ErrorCode.SAFETY_BLOCKED),
        (402, None, "Not enough credits", ErrorCode.INSUFFICIENT_CREDITS),
        (403, None, "forbidden", ErrorCode.FORBIDDEN),
        (404, None, "missing", ErrorCode.NOT_FOUND),
        (409, "idempotency_conflict", "replay differs", ErrorCode.IDEMPOTENCY_CONFLICT),
        (429, None, "too many requests", ErrorCode.RATE_LIMITED),
        (503, "provider_unavailable", "provider down", ErrorCode.PROVIDER_UNAVAILABLE),
    ],
)
def test_normalize_error_code(
    http_status: int,
    error_code: str | None,
    message: str,
    expected: ErrorCode,
) -> None:
    assert (
        normalize_error_code(
            http_status=http_status,
            error_code=error_code,
            message=message,
        )
        is expected
    )


def test_make_api_error_sets_retryability_and_details() -> None:
    err = make_api_error(
        message="provider temporarily unavailable",
        http_status=503,
        error_code="provider_unavailable",
        details={"provider": "example"},
    )

    assert err.code is ErrorCode.PROVIDER_UNAVAILABLE
    assert err.retryable is True
    assert err.details == {"provider": "example"}
