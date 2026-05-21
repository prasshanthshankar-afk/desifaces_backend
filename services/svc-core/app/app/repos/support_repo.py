from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional


def _json_loose(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return default


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row.items())
    except Exception:
        return dict(row)


class SupportRepo:
    def __init__(self, pool: Any):
        self.pool = pool

    @asynccontextmanager
    async def _conn(self, conn: Any = None) -> AsyncIterator[Any]:
        if conn is not None:
            yield conn
            return
        async with self.pool.acquire() as acquired:
            yield acquired

    async def get_user_context(self, user_id: str, conn: Any = None) -> Optional[Dict[str, Any]]:
        q = """
        SELECT
            u.id::text AS user_id,
            u.email,
            split_part(u.email, '@', 1) AS first_name,
            be.tier_code,
            be.plan_code
        FROM core.users u
        LEFT JOIN billing_entitlements be
          ON be.user_id = u.id
        WHERE u.id = $1::uuid
        LIMIT 1
        """
        async with self._conn(conn) as c:
            row = await c.fetchrow(q, user_id)
        return _row_to_dict(row) if row else None

    async def create_request(
        self,
        *,
        user_id: Optional[str],
        name: str,
        email: str,
        topic: str,
        product_area: str,
        priority: str,
        subject: str,
        tier_code: Optional[str],
        metadata_json: Dict[str, Any],
        conn: Any = None,
    ) -> Dict[str, Any]:
        q = """
        INSERT INTO support_requests (
            user_id,
            name,
            email,
            topic,
            product_area,
            priority,
            subject,
            status,
            tier_code,
            latest_message_at,
            metadata_json
        )
        VALUES (
            $1::uuid,
            $2, $3, $4, $5, $6, $7,
            'open'::support_request_status,
            $8,
            now(),
            $9::jsonb
        )
        RETURNING *
        """
        async with self._conn(conn) as c:
            row = await c.fetchrow(
                q,
                user_id,
                name,
                email,
                topic,
                product_area,
                priority,
                subject,
                tier_code,
                json.dumps(metadata_json or {}),
            )
        d = _row_to_dict(row)
        d["metadata_json"] = _json_loose(d.get("metadata_json"), {})
        return d

    async def add_message(
        self,
        *,
        request_id: str,
        sender_role: str,
        sender_user_id: Optional[str],
        sender_email: Optional[str],
        body: str,
        attachments_json: List[Dict[str, Any]],
        is_internal: bool = False,
        conn: Any = None,
    ) -> Dict[str, Any]:
        q = """
        INSERT INTO support_messages (
            request_id,
            sender_role,
            sender_user_id,
            sender_email,
            body,
            attachments_json,
            is_internal
        )
        VALUES (
            $1::uuid,
            $2::support_sender_role,
            $3::uuid,
            $4,
            $5,
            $6::jsonb,
            $7
        )
        RETURNING *
        """
        async with self._conn(conn) as c:
            row = await c.fetchrow(
                q,
                request_id,
                sender_role,
                sender_user_id,
                sender_email,
                body,
                json.dumps(attachments_json or []),
                is_internal,
            )
        d = _row_to_dict(row)
        d["attachments_json"] = _json_loose(d.get("attachments_json"), [])
        return d

    async def update_request_state(
        self,
        *,
        request_id: str,
        status: Optional[str] = None,
        latest_message_at_now: bool = True,
        conn: Any = None,
    ) -> None:
        q = """
        UPDATE support_requests
        SET
            status = COALESCE($2::support_request_status, status),
            latest_message_at = CASE WHEN $3 THEN now() ELSE latest_message_at END,
            updated_at = now()
        WHERE id = $1::uuid
        """
        async with self._conn(conn) as c:
            await c.execute(q, request_id, status, latest_message_at_now)

    async def list_user_requests(
        self,
        *,
        user_id: str,
        limit: int,
        offset: int,
        conn: Any = None,
    ) -> List[Dict[str, Any]]:
        q = """
        SELECT
            id::text,
            topic,
            product_area,
            priority,
            subject,
            status::text AS status,
            latest_message_at,
            created_at
        FROM support_requests
        WHERE user_id = $1::uuid
        ORDER BY latest_message_at DESC, created_at DESC
        LIMIT $2 OFFSET $3
        """
        async with self._conn(conn) as c:
            rows = await c.fetch(q, user_id, limit, offset)
        return [_row_to_dict(r) for r in rows]

    async def get_request_for_user(
        self,
        *,
        request_id: str,
        user_id: str,
        conn: Any = None,
    ) -> Optional[Dict[str, Any]]:
        q = """
        SELECT
            id::text,
            user_id::text AS user_id,
            name,
            email,
            topic,
            product_area,
            priority,
            subject,
            status::text AS status,
            tier_code,
            latest_message_at,
            metadata_json,
            created_at,
            updated_at
        FROM support_requests
        WHERE id = $1::uuid
          AND user_id = $2::uuid
        LIMIT 1
        """
        async with self._conn(conn) as c:
            row = await c.fetchrow(q, request_id, user_id)
        if not row:
            return None
        d = _row_to_dict(row)
        d["metadata_json"] = _json_loose(d.get("metadata_json"), {})
        return d

    async def get_request_by_id(
        self,
        *,
        request_id: str,
        conn: Any = None,
    ) -> Optional[Dict[str, Any]]:
        q = """
        SELECT
            id::text,
            user_id::text AS user_id,
            name,
            email,
            topic,
            product_area,
            priority,
            subject,
            status::text AS status,
            tier_code,
            latest_message_at,
            metadata_json,
            created_at,
            updated_at
        FROM support_requests
        WHERE id = $1::uuid
        LIMIT 1
        """
        async with self._conn(conn) as c:
            row = await c.fetchrow(q, request_id)
        if not row:
            return None
        d = _row_to_dict(row)
        d["metadata_json"] = _json_loose(d.get("metadata_json"), {})
        return d

    async def list_messages(
        self,
        *,
        request_id: str,
        include_internal: bool = False,
        conn: Any = None,
    ) -> List[Dict[str, Any]]:
        q = """
        SELECT
            id::text,
            sender_role::text AS sender_role,
            sender_user_id::text AS sender_user_id,
            sender_email,
            body,
            attachments_json,
            is_internal,
            created_at
        FROM support_messages
        WHERE request_id = $1::uuid
          AND ($2::bool IS TRUE OR is_internal = FALSE)
        ORDER BY created_at ASC
        """
        async with self._conn(conn) as c:
            rows = await c.fetch(q, request_id, include_internal)

        out: List[Dict[str, Any]] = []
        for row in rows:
            d = _row_to_dict(row)
            d["attachments_json"] = _json_loose(d.get("attachments_json"), [])
            out.append(d)
        return out