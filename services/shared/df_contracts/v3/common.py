"""Canonical desifaces-v3 cross-service contract primitives.

These models are additive. They do not replace V2 request/response models until an
explicit #v3-core cutover decision is approved.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Generic, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


T = TypeVar("T")


class V3ContractModel(BaseModel):
    """Base model for V3 service contracts.

    Unknown fields are rejected so contract drift is detected at service boundaries
    rather than silently accepted.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        use_enum_values=False,
    )


class ActorType(StrEnum):
    USER = "user"
    SERVICE = "service"
    API_KEY = "api_key"
    SYSTEM = "system"


class RequestActor(V3ContractModel):
    actor_type: ActorType
    actor_id: UUID
    account_id: UUID | None = None
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()


class RequestContext(V3ContractModel):
    """Context propagated across synchronous APIs and asynchronous jobs."""

    request_id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID = Field(default_factory=uuid4)
    actor: RequestActor
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)
    client_app: str | None = Field(default=None, max_length=100)
    client_version: str | None = Field(default=None, max_length=50)
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "validation_error"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    ENTITLEMENT_REQUIRED = "entitlement_required"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    SAFETY_BLOCKED = "safety_blocked"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INTERNAL_ERROR = "internal_error"


class ApiError(V3ContractModel):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool = False
    field: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ApiMeta(V3ContractModel):
    request_id: UUID
    correlation_id: UUID
    contract_version: str = "v3"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApiEnvelope(V3ContractModel, Generic[T]):
    """Standard V3 API response envelope.

    Exactly one of data/error should normally be populated by API adapters.
    """

    data: T | None = None
    error: ApiError | None = None
    meta: ApiMeta


class PageInfo(V3ContractModel):
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = Field(default=50, ge=1, le=200)


class PagedResult(V3ContractModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    page: PageInfo
