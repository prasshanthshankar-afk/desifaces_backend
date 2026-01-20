from __future__ import annotations

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
) -> None:
    """
    Append-only audit logger.
    Never throws (best-effort) — but you can change this to strict mode later.
    """
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
            before,
            after,
            ip,
            user_agent,
        )
    except Exception:
        # Best-effort: do not break auth flows because audit insert failed.
        # If you want strict mode later, re-raise.
        return