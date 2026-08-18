"""Shared compatibility-adapter primitives for desifaces-v3.

This module intentionally has no FastAPI, database, provider, or service-specific
imports. Capability adapters use these helpers to translate current public
contracts into the canonical V3 vocabulary without duplicating cross-service
rules.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping, Sequence
from uuid import UUID

from .common import (
    ActorType,
    ApiError,
    ErrorCode,
    RequestActor,
    RequestContext,
)
from .domain import JobState


_JOB_STATE_ALIASES: dict[str, JobState] = {
    "accepted": JobState.SUBMITTED,
    "created": JobState.SUBMITTED,
    "submitted": JobState.SUBMITTED,
    "pending": JobState.QUEUED,
    "waiting": JobState.QUEUED,
    "queued": JobState.QUEUED,
    "processing": JobState.RUNNING,
    "in_progress": JobState.RUNNING,
    "running": JobState.RUNNING,
    "complete": JobState.SUCCEEDED,
    "completed": JobState.SUCCEEDED,
    "success": JobState.SUCCEEDED,
    "succeeded": JobState.SUCCEEDED,
    "error": JobState.FAILED,
    "failed": JobState.FAILED,
    "blocked": JobState.BLOCKED,
    "safety_blocked": JobState.BLOCKED,
    "cancelled": JobState.CANCELED,
    "canceled": JobState.CANCELED,
    "expired": JobState.EXPIRED,
    "timed_out": JobState.EXPIRED,
    "timeout": JobState.EXPIRED,
}


def _normalize_token(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )


def normalize_job_state(
    value: str | JobState | None,
    *,
    default: JobState = JobState.SUBMITTED,
) -> JobState:
    """Map compatibility/provider status vocabulary to canonical ``JobState``.

    Unknown values deliberately fall back to the caller-selected default rather
    than becoming new canonical states.
    """

    if isinstance(value, JobState):
        return value

    token = _normalize_token(value)
    if not token:
        return default
    return _JOB_STATE_ALIASES.get(token, default)


def stable_payload_digest(payload: Mapping[str, Any] | Sequence[Any] | Any) -> str:
    """Return a stable SHA-256 digest for an adapter request payload."""

    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def derive_idempotency_key(
    *,
    scope: str,
    actor_id: UUID | str,
    payload: Mapping[str, Any] | Sequence[Any] | Any,
    explicit_key: str | None = None,
) -> str:
    """Return the explicit idempotency key or derive a stable adapter key.

    The derived key is deterministic for ``scope + actor + payload`` and is
    intentionally independent of provider identity. This prevents retry logic
    from creating duplicate expensive provider calls or duplicate billing events.
    """

    if explicit_key is not None:
        value = explicit_key.strip()
        if len(value) < 8:
            raise ValueError("idempotency_key_must_be_at_least_8_characters")
        if len(value) > 200:
            raise ValueError("idempotency_key_must_be_at_most_200_characters")
        return value

    clean_scope = _normalize_token(scope)
    if not clean_scope:
        raise ValueError("idempotency_scope_required")

    material = {
        "scope": clean_scope,
        "actor_id": str(actor_id),
        "payload_digest": stable_payload_digest(payload),
    }
    digest = stable_payload_digest(material)
    return f"v3:{clean_scope}:{digest[:40]}"


def make_request_context(
    *,
    actor_id: UUID,
    actor_type: ActorType = ActorType.USER,
    account_id: UUID | None = None,
    roles: Sequence[str] = (),
    scopes: Sequence[str] = (),
    request_id: UUID | None = None,
    correlation_id: UUID | None = None,
    idempotency_key: str | None = None,
    client_app: str | None = None,
    client_version: str | None = None,
    requested_at: datetime | None = None,
) -> RequestContext:
    """Build the canonical request context used by all compatibility adapters."""

    actor = RequestActor(
        actor_type=actor_type,
        actor_id=actor_id,
        account_id=account_id,
        roles=tuple(roles),
        scopes=tuple(scopes),
    )

    values: dict[str, Any] = {
        "actor": actor,
        "idempotency_key": idempotency_key,
        "client_app": client_app,
        "client_version": client_version,
    }
    if request_id is not None:
        values["request_id"] = request_id
    if correlation_id is not None:
        values["correlation_id"] = correlation_id
    if requested_at is not None:
        values["requested_at"] = requested_at

    return RequestContext(**values)


def normalize_error_code(
    *,
    http_status: int | None = None,
    error_code: str | None = None,
    message: str | None = None,
) -> ErrorCode:
    """Normalize service/provider error vocabulary to canonical ``ErrorCode``."""

    haystack = " ".join(
        part for part in (error_code or "", message or "") if part
    ).lower()

    # Semantic markers are evaluated before HTTP status because several current
    # services return domain-specific failures using generic 400/409 responses.
    if "idempot" in haystack:
        return ErrorCode.IDEMPOTENCY_CONFLICT
    if any(token in haystack for token in ("unsafe", "safety_block", "content_safety")):
        return ErrorCode.SAFETY_BLOCKED
    if any(token in haystack for token in ("insufficient_credit", "not enough credits", "credit_shortage")):
        return ErrorCode.INSUFFICIENT_CREDITS
    if any(token in haystack for token in ("entitlement", "module_disabled", "plan_required", "upgrade_required")):
        return ErrorCode.ENTITLEMENT_REQUIRED
    if any(token in haystack for token in ("provider_unavailable", "provider_timeout", "provider_exhausted")):
        return ErrorCode.PROVIDER_UNAVAILABLE
    if any(token in haystack for token in ("rate_limit", "too many requests")):
        return ErrorCode.RATE_LIMITED
    if any(token in haystack for token in ("unauth", "invalid_token", "token_expired", "auth_required")):
        return ErrorCode.UNAUTHENTICATED
    if any(token in haystack for token in ("forbidden", "permission_denied", "ownership")):
        return ErrorCode.FORBIDDEN
    if any(token in haystack for token in ("not_found", "not found", "missing_resource")):
        return ErrorCode.NOT_FOUND

    if http_status in (401,):
        return ErrorCode.UNAUTHENTICATED
    if http_status in (403,):
        return ErrorCode.FORBIDDEN
    if http_status in (404,):
        return ErrorCode.NOT_FOUND
    if http_status in (409,):
        return ErrorCode.CONFLICT
    if http_status in (429,):
        return ErrorCode.RATE_LIMITED
    if http_status is not None and 400 <= http_status < 500:
        return ErrorCode.VALIDATION_ERROR
    if http_status is not None and http_status >= 500:
        return ErrorCode.INTERNAL_ERROR

    return ErrorCode.INTERNAL_ERROR


def make_api_error(
    *,
    message: str,
    http_status: int | None = None,
    error_code: str | None = None,
    retryable: bool | None = None,
    field: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> ApiError:
    """Create a canonical ``ApiError`` from a compatibility/service error."""

    code = normalize_error_code(
        http_status=http_status,
        error_code=error_code,
        message=message,
    )

    if retryable is None:
        retryable = code in {
            ErrorCode.RATE_LIMITED,
            ErrorCode.PROVIDER_UNAVAILABLE,
            ErrorCode.INTERNAL_ERROR,
        }

    return ApiError(
        code=code,
        message=message,
        retryable=retryable,
        field=field,
        details=dict(details or {}),
    )
