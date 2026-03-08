# services/svc-commerce/app/app/services/ops/operational_controls.py
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Protocol


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)



def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            v = json.loads(s)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    try:
        return dict(x)
    except Exception:
        return {}



def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()



def _bucket_start(now: datetime, kind: str) -> datetime:
    if kind == "minute":
        return now.replace(second=0, microsecond=0)
    if kind == "hour":
        return now.replace(minute=0, second=0, microsecond=0)
    if kind == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"Unsupported bucket kind: {kind}")


# -----------------------------------------------------------------------------
# repo protocols
# -----------------------------------------------------------------------------


class TenantCredentialsRepoProtocol(Protocol):
    async def get_active_credential(self, key_id: str) -> Optional[Dict[str, Any]]: ...


class TenantRateLimitsRepoProtocol(Protocol):
    async def get_limit(self, tenant_id: str, route_pattern: str) -> Optional[Dict[str, Any]]: ...

    async def get_counter(self, tenant_id: str, route_pattern: str, bucket_kind: str, bucket_start_iso: str) -> Optional[Dict[str, Any]]: ...

    async def upsert_counter(self, tenant_id: str, route_pattern: str, bucket_kind: str, bucket_start_iso: str, request_count: int) -> None: ...

    async def get_active_job_count(self, tenant_id: str) -> int: ...


class BusinessJobsRepoProtocol(Protocol):
    async def get_by_idempotency_key(self, tenant_id: str, idempotency_key: str) -> Optional[Dict[str, Any]]: ...


class AuditLogsRepoProtocol(Protocol):
    async def insert_audit_log(self, payload: Dict[str, Any]) -> None: ...


class WebhooksRepoProtocol(Protocol):
    async def list_active_webhooks(self, tenant_id: str) -> list[Dict[str, Any]]: ...

    async def enqueue_delivery(self, payload: Dict[str, Any]) -> None: ...


# -----------------------------------------------------------------------------
# public types
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class AuthResult:
    tenant_id: str
    credential_id: str
    scopes_json: Dict[str, Any]
    metadata_json: Dict[str, Any]


@dataclass(slots=True)
class RateLimitDecision:
    allowed: bool
    route_pattern: str
    tenant_id: str
    minute_count: int
    hour_count: int
    day_count: int
    concurrent_job_count: int
    limit_snapshot: Dict[str, Any]
    deny_reason: Optional[str] = None


@dataclass(slots=True)
class IdempotencyResult:
    is_replay: bool
    existing_job_id: Optional[str] = None
    conflict: bool = False
    conflict_reason: Optional[str] = None


@dataclass(slots=True)
class WebhookEvent:
    tenant_id: str
    job_id: str
    event_type: str
    payload: Dict[str, Any]


# -----------------------------------------------------------------------------
# auth / rate limits / idempotency / audit / webhooks
# -----------------------------------------------------------------------------


class TenantAuthService:
    def __init__(self, credentials_repo: TenantCredentialsRepoProtocol) -> None:
        self._credentials_repo = credentials_repo

    async def authenticate_bearer(self, bearer_token: str) -> AuthResult:
        token = (bearer_token or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            raise PermissionError("Missing bearer token")

        try:
            key_id, raw_secret = token.split(".", 1)
        except ValueError as exc:
            raise PermissionError("Malformed bearer token") from exc

        row = await self._credentials_repo.get_active_credential(key_id)
        if not row:
            raise PermissionError("Unknown credential")
        stored_hash = str(row.get("secret_hash") or "")
        if not hmac.compare_digest(stored_hash, _hash_secret(raw_secret)):
            raise PermissionError("Invalid credential secret")
        if not bool(row.get("is_active", True)):
            raise PermissionError("Credential inactive")

        return AuthResult(
            tenant_id=str(row.get("tenant_id")),
            credential_id=str(row.get("id")),
            scopes_json=_as_dict(row.get("scopes_json")),
            metadata_json=_as_dict(row.get("metadata_json")),
        )


class TenantRateLimitService:
    def __init__(self, repo: TenantRateLimitsRepoProtocol) -> None:
        self._repo = repo

    async def check_and_increment(
        self,
        *,
        tenant_id: str,
        route_pattern: str,
        increment: int = 1,
    ) -> RateLimitDecision:
        now = _utcnow()
        limit = _as_dict(await self._repo.get_limit(tenant_id, route_pattern))
        if not limit:
            limit = {
                "requests_per_minute": 60,
                "requests_per_hour": 1000,
                "requests_per_day": 10000,
                "max_concurrent_jobs": 25,
                "max_payload_mb": 25,
                "route_pattern": route_pattern,
            }

        minute_bucket = _bucket_start(now, "minute").isoformat()
        hour_bucket = _bucket_start(now, "hour").isoformat()
        day_bucket = _bucket_start(now, "day").isoformat()

        minute_count = int((_as_dict(await self._repo.get_counter(tenant_id, route_pattern, "minute", minute_bucket))).get("request_count") or 0)
        hour_count = int((_as_dict(await self._repo.get_counter(tenant_id, route_pattern, "hour", hour_bucket))).get("request_count") or 0)
        day_count = int((_as_dict(await self._repo.get_counter(tenant_id, route_pattern, "day", day_bucket))).get("request_count") or 0)
        concurrent_job_count = int(await self._repo.get_active_job_count(tenant_id))

        next_minute = minute_count + increment
        next_hour = hour_count + increment
        next_day = day_count + increment

        deny_reason: Optional[str] = None
        if next_minute > int(limit.get("requests_per_minute") or 60):
            deny_reason = "RATE_LIMITED_MINUTE"
        elif next_hour > int(limit.get("requests_per_hour") or 1000):
            deny_reason = "RATE_LIMITED_HOUR"
        elif next_day > int(limit.get("requests_per_day") or 10000):
            deny_reason = "RATE_LIMITED_DAY"
        elif concurrent_job_count >= int(limit.get("max_concurrent_jobs") or 25):
            deny_reason = "RATE_LIMITED_CONCURRENCY"

        allowed = deny_reason is None
        if allowed:
            await self._repo.upsert_counter(tenant_id, route_pattern, "minute", minute_bucket, next_minute)
            await self._repo.upsert_counter(tenant_id, route_pattern, "hour", hour_bucket, next_hour)
            await self._repo.upsert_counter(tenant_id, route_pattern, "day", day_bucket, next_day)

        return RateLimitDecision(
            allowed=allowed,
            route_pattern=route_pattern,
            tenant_id=tenant_id,
            minute_count=next_minute if allowed else minute_count,
            hour_count=next_hour if allowed else hour_count,
            day_count=next_day if allowed else day_count,
            concurrent_job_count=concurrent_job_count,
            limit_snapshot=limit,
            deny_reason=deny_reason,
        )


class IdempotencyService:
    def __init__(self, jobs_repo: BusinessJobsRepoProtocol) -> None:
        self._jobs_repo = jobs_repo

    async def evaluate(self, *, tenant_id: str, idempotency_key: Optional[str], request_fingerprint: str) -> IdempotencyResult:
        key = (idempotency_key or "").strip()
        if not key:
            return IdempotencyResult(is_replay=False)
        row = _as_dict(await self._jobs_repo.get_by_idempotency_key(tenant_id, key))
        if not row:
            return IdempotencyResult(is_replay=False)

        existing_request_json = _as_dict(row.get("request_json"))
        existing_fingerprint = str(existing_request_json.get("request_fingerprint") or "")
        if existing_fingerprint and existing_fingerprint != request_fingerprint:
            return IdempotencyResult(
                is_replay=False,
                existing_job_id=str(row.get("id") or ""),
                conflict=True,
                conflict_reason="IDEMPOTENCY_CONFLICT",
            )
        return IdempotencyResult(is_replay=True, existing_job_id=str(row.get("id") or ""))


class AuditLogService:
    def __init__(self, audit_repo: AuditLogsRepoProtocol) -> None:
        self._audit_repo = audit_repo

    async def log(
        self,
        *,
        request_id: str,
        route_pattern: str,
        method: str,
        http_status: int,
        tenant_id: Optional[str],
        credential_id: Optional[str],
        client_job_id: Optional[str],
        business_job_id: Optional[str],
        remote_addr: Optional[str],
        user_agent: Optional[str],
        payload_size_bytes: Optional[int],
        duration_ms: Optional[int],
        provider_name: Optional[str],
        provider_request_id: Optional[str],
        audit_json: Optional[Dict[str, Any]] = None,
    ) -> None:
        await self._audit_repo.insert_audit_log(
            {
                "request_id": request_id,
                "route_pattern": route_pattern,
                "method": method,
                "http_status": http_status,
                "tenant_id": tenant_id,
                "credential_id": credential_id,
                "client_job_id": client_job_id,
                "business_job_id": business_job_id,
                "remote_addr": remote_addr,
                "user_agent": user_agent,
                "payload_size_bytes": payload_size_bytes,
                "duration_ms": duration_ms,
                "provider_name": provider_name,
                "provider_request_id": provider_request_id,
                "audit_json": audit_json or {},
            }
        )


class WebhookDeliveryService:
    def __init__(self, webhooks_repo: WebhooksRepoProtocol) -> None:
        self._webhooks_repo = webhooks_repo

    async def enqueue_job_event(self, event: WebhookEvent) -> int:
        hooks = await self._webhooks_repo.list_active_webhooks(event.tenant_id)
        if not hooks:
            return 0

        enqueued = 0
        for hook in hooks:
            raw_event_types = hook.get("event_types_json") or []
            if isinstance(raw_event_types, str):
                try:
                    raw_event_types = json.loads(raw_event_types)
                except Exception:
                    raw_event_types = []
            event_types = {str(v) for v in raw_event_types}
            if event_types and event.event_type not in event_types:
                continue
            await self._webhooks_repo.enqueue_delivery(
                {
                    "tenant_id": event.tenant_id,
                    "webhook_id": str(hook.get("id")),
                    "job_id": event.job_id,
                    "event_type": event.event_type,
                    "request_json": event.payload,
                    "delivery_status": "queued",
                    "attempt_count": 0,
                    "next_attempt_at": _utcnow().isoformat(),
                }
            )
            enqueued += 1
        return enqueued


def make_request_fingerprint(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
