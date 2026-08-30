from __future__ import annotations

import json
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg


async def audit_log(
    conn: asyncpg.Connection,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    actor_user_id: Optional[str] = None,
    request_id: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    before: Optional[Dict[str, Any]] = None,
    after: Optional[Dict[str, Any]] = None,
    strict: bool = False,
) -> None:
    """Append an audit event to ``core.audit_log``.

    Existing authentication flows may keep best-effort audit behavior by using
    the default ``strict=False``. Privileged Admin mutations use
    ``strict=True`` inside the same database transaction as the mutation so a
    change cannot commit without its corresponding audit record.
    """
    before_json = json.dumps(before, default=str) if before is not None else None
    after_json = json.dumps(after, default=str) if after is not None else None

    try:
        await conn.execute(
            """
            INSERT INTO core.audit_log(
              actor_user_id, action, entity_type, entity_id, request_id,
              before_json, after_json, ip, user_agent
            )
            VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9)
            """,
            UUID(actor_user_id) if actor_user_id else None,
            action,
            entity_type,
            entity_id,
            request_id,
            before_json,
            after_json,
            ip,
            user_agent,
        )
    except Exception:
        if strict:
            raise
        # Authentication and other legacy callers remain best-effort.
        return
