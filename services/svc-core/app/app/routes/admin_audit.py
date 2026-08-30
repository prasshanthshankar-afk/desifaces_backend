from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.db import get_pool
from app.deps import require_admin

router = APIRouter()

AUDIT_CATEGORIES = (
    "all",
    "sessions",
    "access",
    "users",
    "billing",
    "jobs",
    "workflows",
    "media",
    "providers",
    "assistant",
    "support",
    "developer",
    "system",
    "other",
)

_SENSITIVE_KEYS = {
    "access_token",
    "refresh_token",
    "token",
    "password",
    "password_hash",
    "secret",
    "authorization",
    "cookie",
    "code_hash",
    "otp",
    "otp_code",
    "verification_code",
}


def _audit_category(action: str | None, entity_type: str | None) -> str:
    a = str(action or "").strip().lower()
    e = str(entity_type or "").strip().lower()

    if a.startswith("auth.") or e in {"session", "auth", "login_attempt", "email_challenge"}:
        return "sessions"
    if (
        a.startswith("admin.role.")
        or a.startswith("admin.super_admin.")
        or "access" in a
        or "role.grant" in a
        or "role.revoke" in a
        or e in {"role", "user_role"}
    ):
        return "access"
    if a.startswith("admin.user") or (e == "user" and not a.startswith("auth.")):
        return "users"
    if any(part in a for part in ("billing", "pricing", "commerce", "credit", "subscription", "payment")) or e in {
        "billing",
        "credit",
        "subscription",
        "payment",
    }:
        return "billing"
    if "workflow" in a or "director" in a or "story" in a or e in {"workflow", "director_run", "story"}:
        return "workflows"
    if "job" in a or e.endswith("job") or e == "job":
        return "jobs"
    if "media" in a or "asset" in a or e in {"media", "media_asset", "asset"}:
        return "media"
    if "provider" in a or e == "provider":
        return "providers"
    if "assistant" in a or e == "assistant":
        return "assistant"
    if "support" in a or e in {"support", "support_request", "ticket"}:
        return "support"
    if "developer" in a or "api_key" in a or e in {"developer_key", "api_key"}:
        return "developer"
    if any(part in a for part in ("system", "deploy", "notification", "email", "health")) or e in {
        "system",
        "deployment",
        "notification",
    }:
        return "system"
    return "other"


def _audit_outcome(action: str | None) -> str:
    a = str(action or "").strip().lower()
    if any(part in a for part in ("failed", "error", "denied", "rejected")):
        return "failed"
    if any(part in a for part in ("pending", "queued", "started", "requested")):
        return "pending"
    return "success"


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if lowered in _SENSITIVE_KEYS or any(
                marker in lowered for marker in ("password", "secret", "authorization", "refresh_token", "access_token", "code_hash")
            ):
                safe[key_text] = "[REDACTED]"
            else:
                safe[key_text] = _redact_payload(item)
        return safe
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


def _safe_target(entity_type: str | None, entity_id: str | None) -> str:
    e = str(entity_type or "entity").strip() or "entity"
    raw = str(entity_id or "").strip()
    if e.lower() in {"session", "auth", "login_attempt", "email_challenge", "token"}:
        return f"{e}:redacted"
    return f"{e}:{raw or '—'}"


def _searchable(entry: dict[str, Any]) -> str:
    parts = [
        entry.get("action"),
        entry.get("actor_email"),
        entry.get("actor_user_id"),
        entry.get("target"),
        entry.get("request_id"),
        entry.get("category"),
        entry.get("outcome"),
    ]
    return " ".join(str(part or "") for part in parts).lower()


@router.get("/audit")
async def admin_audit_events(
    limit: int = Query(default=100, ge=1, le=500),
    category: str = Query(default="all", max_length=32),
    action: str | None = Query(default=None, max_length=160),
    q: str | None = Query(default=None, max_length=200),
    _: dict = Depends(require_admin),
):
    """Read a privacy-minimized operational audit stream.

    The Admin console may categorize sessions/authentication, privileged access,
    user administration, billing, jobs, workflows and other audited operations.
    Sensitive token/password/secret fields are recursively redacted and raw
    session/auth identifiers are never returned.
    """
    requested_category = str(category or "all").strip().lower()
    if requested_category not in AUDIT_CATEGORIES:
        requested_category = "all"
    requested_action = str(action or "").strip()
    requested_query = str(q or "").strip().lower()
    scan_limit = min(2500, max(500, limit * 5))

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                a.id,
                a.actor_user_id::text AS actor_user_id,
                u.email AS actor_email,
                a.action,
                a.entity_type,
                a.entity_id,
                a.request_id,
                a.before_json,
                a.after_json,
                a.created_at
            FROM core.audit_log a
            LEFT JOIN core.users u ON u.id = a.actor_user_id
            WHERE ($1 = '' OR a.action = $1)
            ORDER BY a.created_at DESC
            LIMIT $2
            """,
            requested_action,
            scan_limit,
        )

    items: list[dict[str, Any]] = []
    counts = {name: 0 for name in AUDIT_CATEGORIES if name != "all"}
    for row in rows:
        raw = dict(row)
        event_category = _audit_category(raw.get("action"), raw.get("entity_type"))
        counts[event_category] = counts.get(event_category, 0) + 1
        entry = {
            "id": raw.get("id"),
            "actor_user_id": raw.get("actor_user_id"),
            "actor_email": raw.get("actor_email"),
            "action": raw.get("action"),
            "category": event_category,
            "outcome": _audit_outcome(raw.get("action")),
            "target": _safe_target(raw.get("entity_type"), raw.get("entity_id")),
            "request_id": raw.get("request_id"),
            "before": _redact_payload(raw.get("before_json")),
            "after": _redact_payload(raw.get("after_json")),
            "created_at": raw.get("created_at"),
            "source": "Core",
        }
        if requested_category != "all" and event_category != requested_category:
            continue
        if requested_query and requested_query not in _searchable(entry):
            continue
        items.append(entry)
        if len(items) >= limit:
            break

    return {
        "items": items,
        "count": len(items),
        "category": requested_category,
        "categories": list(AUDIT_CATEGORIES),
        "category_counts": counts,
        "source": "core.audit_log",
        "privacy": {
            "session_identifiers_redacted": True,
            "sensitive_payload_keys_redacted": True,
        },
    }
