from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional


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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_max_attempts(channel: str) -> int:
    c = (channel or "").strip().lower()
    if c == "email":
        return 4
    if c == "push":
        return 3
    if c == "in_app":
        return 1
    return 2


class NotificationsRepo:
    DEFAULT_CATEGORIES = (
        "jobs",
        "billing",
        "account",
        "support",
        "announcements",
    )

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

    async def get_event_by_dedupe_key(self, dedupe_key: str, conn: Any = None) -> Optional[Dict[str, Any]]:
        q = """
        SELECT *
        FROM notification_events
        WHERE dedupe_key = $1
        LIMIT 1
        """
        async with self._conn(conn) as c:
            row = await c.fetchrow(q, dedupe_key)
        return _row_to_dict(row) if row else None

    async def create_event(
        self,
        *,
        event_type: str,
        category: str,
        priority: str,
        source_service: str,
        source_ref_type: Optional[str],
        source_ref_id: Optional[str],
        actor_user_id: Optional[str],
        title: str,
        body: str,
        action_route: Optional[str],
        action_label: Optional[str],
        image_url: Optional[str],
        payload_json: Dict[str, Any],
        metadata_json: Dict[str, Any],
        dedupe_key: Optional[str],
        conn: Any = None,
    ) -> Dict[str, Any]:
        q = """
        INSERT INTO notification_events (
            event_type,
            category,
            priority,
            source_service,
            source_ref_type,
            source_ref_id,
            actor_user_id,
            title,
            body,
            action_route,
            action_label,
            image_url,
            payload_json,
            metadata_json,
            dedupe_key
        )
        VALUES (
            $1, $2::notification_category, $3::notification_priority, $4,
            $5, $6, $7::uuid, $8, $9, $10, $11, $12,
            $13::jsonb, $14::jsonb, $15
        )
        RETURNING *
        """
        async with self._conn(conn) as c:
            row = await c.fetchrow(
                q,
                event_type,
                category,
                priority,
                source_service,
                source_ref_type,
                source_ref_id,
                actor_user_id,
                title,
                body,
                action_route,
                action_label,
                image_url,
                json.dumps(payload_json or {}),
                json.dumps(metadata_json or {}),
                dedupe_key,
            )
        return _row_to_dict(row)

    async def create_user_item(
        self,
        *,
        event_id: str,
        user_id: str,
        category: str,
        priority: str,
        event_type: str,
        title: str,
        body: str,
        action_route: Optional[str],
        action_label: Optional[str],
        image_url: Optional[str],
        metadata_json: Dict[str, Any],
        conn: Any = None,
    ) -> Dict[str, Any]:
        q = """
        INSERT INTO notification_user_items (
            event_id,
            user_id,
            category,
            priority,
            event_type,
            title,
            body,
            action_route,
            action_label,
            image_url,
            metadata_json
        )
        VALUES (
            $1::uuid, $2::uuid, $3::notification_category, $4::notification_priority,
            $5, $6, $7, $8, $9, $10, $11::jsonb
        )
        ON CONFLICT (event_id, user_id)
        DO UPDATE SET
            title = EXCLUDED.title,
            body = EXCLUDED.body,
            action_route = EXCLUDED.action_route,
            action_label = EXCLUDED.action_label,
            image_url = EXCLUDED.image_url,
            metadata_json = EXCLUDED.metadata_json
        RETURNING *
        """
        async with self._conn(conn) as c:
            row = await c.fetchrow(
                q,
                event_id,
                user_id,
                category,
                priority,
                event_type,
                title,
                body,
                action_route,
                action_label,
                image_url,
                json.dumps(metadata_json or {}),
            )
        return _row_to_dict(row)

    async def create_delivery(
        self,
        *,
        event_id: str,
        user_id: str,
        channel: str,
        destination: Optional[str],
        provider: Optional[str],
        payload_json: Dict[str, Any],
        next_attempt_at: Optional[datetime] = None,
        max_attempts: Optional[int] = None,
        conn: Any = None,
    ) -> Dict[str, Any]:
        effective_next_attempt_at = next_attempt_at or _utcnow()
        effective_max_attempts = int(max_attempts or _default_max_attempts(channel))

        q = """
        INSERT INTO notification_deliveries (
            event_id,
            user_id,
            channel,
            destination,
            provider,
            payload_json,
            status,
            next_attempt_at,
            max_attempts
        )
        VALUES (
            $1::uuid, $2::uuid, $3::notification_channel, $4, $5, $6::jsonb,
            'queued'::notification_delivery_status,
            $7,
            $8
        )
        ON CONFLICT (event_id, user_id, channel)
        DO UPDATE SET
            destination = EXCLUDED.destination,
            provider = EXCLUDED.provider,
            payload_json = EXCLUDED.payload_json,
            updated_at = now()
        RETURNING *
        """
        async with self._conn(conn) as c:
            row = await c.fetchrow(
                q,
                event_id,
                user_id,
                channel,
                destination,
                provider,
                json.dumps(payload_json or {}),
                effective_next_attempt_at,
                effective_max_attempts,
            )
        d = _row_to_dict(row)
        d["payload_json"] = _json_loose(d.get("payload_json"), {})
        return d

    async def list_user_items(
        self,
        *,
        user_id: str,
        category: Optional[str],
        limit: int,
        offset: int,
        conn: Any = None,
    ) -> List[Dict[str, Any]]:
        q = """
        SELECT
            id::text,
            title,
            body,
            category::text AS category,
            priority::text AS priority,
            event_type,
            created_at,
            is_read,
            image_url,
            action_route,
            action_label,
            metadata_json
        FROM notification_user_items
        WHERE user_id = $1::uuid
          AND ($2::text IS NULL OR category::text = $2::text)
        ORDER BY created_at DESC
        LIMIT $3 OFFSET $4
        """
        async with self._conn(conn) as c:
            rows = await c.fetch(q, user_id, category, limit, offset)

        items: List[Dict[str, Any]] = []
        for row in rows:
            d = _row_to_dict(row)
            d["metadata_json"] = _json_loose(d.get("metadata_json"), {})
            items.append(d)
        return items

    async def get_unread_count(self, *, user_id: str, conn: Any = None) -> int:
        q = """
        SELECT COUNT(*)::int AS unread_count
        FROM notification_user_items
        WHERE user_id = $1::uuid
          AND is_read = FALSE
        """
        async with self._conn(conn) as c:
            row = await c.fetchrow(q, user_id)
        return int((row or {}).get("unread_count", 0))

    async def mark_read(self, *, user_id: str, item_id: str, conn: Any = None) -> None:
        q = """
        UPDATE notification_user_items
        SET
            is_read = TRUE,
            read_at = COALESCE(read_at, now())
        WHERE id = $1::uuid
          AND user_id = $2::uuid
        """
        async with self._conn(conn) as c:
            await c.execute(q, item_id, user_id)

    async def mark_all_read(self, *, user_id: str, conn: Any = None) -> None:
        q = """
        UPDATE notification_user_items
        SET
            is_read = TRUE,
            read_at = COALESCE(read_at, now())
        WHERE user_id = $1::uuid
          AND is_read = FALSE
        """
        async with self._conn(conn) as c:
            await c.execute(q, user_id)

    async def get_preferences(self, *, user_id: str, conn: Any = None) -> List[Dict[str, Any]]:
        q = """
        SELECT
            category::text AS category,
            in_app_enabled,
            push_enabled,
            email_enabled,
            updated_at
        FROM notification_preferences
        WHERE user_id = $1::uuid
        ORDER BY category::text
        """
        async with self._conn(conn) as c:
            rows = await c.fetch(q, user_id)
        return [_row_to_dict(r) for r in rows]

    async def upsert_preferences(
        self,
        *,
        user_id: str,
        items: Iterable[Dict[str, Any]],
        conn: Any = None,
    ) -> None:
        q = """
        INSERT INTO notification_preferences (
            user_id,
            category,
            in_app_enabled,
            push_enabled,
            email_enabled,
            updated_at
        )
        VALUES (
            $1::uuid, $2::notification_category, $3, $4, $5, now()
        )
        ON CONFLICT (user_id, category)
        DO UPDATE SET
            in_app_enabled = EXCLUDED.in_app_enabled,
            push_enabled = EXCLUDED.push_enabled,
            email_enabled = EXCLUDED.email_enabled,
            updated_at = now()
        """
        async with self._conn(conn) as c:
            for item in items:
                await c.execute(
                    q,
                    user_id,
                    item["category"],
                    bool(item.get("in_app_enabled", True)),
                    bool(item.get("push_enabled", True)),
                    bool(item.get("email_enabled", True)),
                )

    async def ensure_default_preferences(self, *, user_id: str, conn: Any = None) -> None:
        existing = await self.get_preferences(user_id=user_id, conn=conn)
        existing_categories = {str(x.get("category")) for x in existing}
        missing = [c for c in self.DEFAULT_CATEGORIES if c not in existing_categories]
        if not missing:
            return

        await self.upsert_preferences(
            user_id=user_id,
            items=[
                {
                    "category": c,
                    "in_app_enabled": True,
                    "push_enabled": True,
                    "email_enabled": True,
                }
                for c in missing
            ],
            conn=conn,
        )

    async def register_device(
        self,
        *,
        user_id: str,
        platform: str,
        expo_push_token: str,
        device_name: Optional[str],
        app_version: Optional[str],
        conn: Any = None,
    ) -> None:
        q = """
        INSERT INTO notification_devices (
            user_id,
            platform,
            expo_push_token,
            device_name,
            app_version,
            is_active,
            last_seen_at
        )
        VALUES (
            $1::uuid, $2, $3, $4, $5, TRUE, now()
        )
        ON CONFLICT (user_id, expo_push_token)
        DO UPDATE SET
            platform = EXCLUDED.platform,
            device_name = EXCLUDED.device_name,
            app_version = EXCLUDED.app_version,
            is_active = TRUE,
            last_seen_at = now()
        """
        async with self._conn(conn) as c:
            await c.execute(
                q,
                user_id,
                platform,
                expo_push_token,
                device_name,
                app_version,
            )

    async def list_active_devices(self, *, user_id: str, conn: Any = None) -> List[Dict[str, Any]]:
        q = """
        SELECT
            id::text,
            platform,
            expo_push_token,
            device_name,
            app_version,
            last_seen_at
        FROM notification_devices
        WHERE user_id = $1::uuid
          AND is_active = TRUE
        ORDER BY last_seen_at DESC
        """
        async with self._conn(conn) as c:
            rows = await c.fetch(q, user_id)
        return [_row_to_dict(r) for r in rows]

    async def deactivate_device_tokens(
        self,
        *,
        user_id: str,
        tokens: List[str],
        conn: Any = None,
    ) -> None:
        if not tokens:
            return

        q = """
        UPDATE notification_devices
        SET
            is_active = FALSE,
            last_seen_at = now()
        WHERE user_id = $1::uuid
          AND expo_push_token = ANY($2::text[])
        """
        async with self._conn(conn) as c:
            await c.execute(q, user_id, tokens)

    async def claim_due_deliveries(
        self,
        *,
        channel: Optional[str] = None,
        limit: int = 100,
        stale_processing_after_minutes: int = 20,
        conn: Any = None,
    ) -> List[Dict[str, Any]]:
        q = """
        WITH candidates AS (
            SELECT d.id
            FROM notification_deliveries d
            WHERE
              (
                (d.status = 'queued'::notification_delivery_status AND d.next_attempt_at <= now())
                OR
                (
                  d.status = 'processing'::notification_delivery_status
                  AND d.processing_started_at IS NOT NULL
                  AND d.processing_started_at <= now() - make_interval(mins => $3)
                )
              )
              AND ($1::text IS NULL OR d.channel::text = $1::text)
              AND d.attempt_count < d.max_attempts
            ORDER BY d.next_attempt_at ASC, d.created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT $2
        ),
        updated AS (
            UPDATE notification_deliveries d
            SET
                status = 'processing'::notification_delivery_status,
                attempt_count = d.attempt_count + 1,
                processing_started_at = now(),
                last_attempt_at = now(),
                updated_at = now(),
                error_code = NULL,
                error_message = NULL
            FROM candidates c
            WHERE d.id = c.id
            RETURNING d.*
        )
        SELECT
            u.id::text,
            u.event_id::text AS event_id,
            u.user_id::text AS user_id,
            u.channel::text AS channel,
            u.destination,
            u.provider,
            u.provider_message_id,
            u.status::text AS status,
            u.attempt_count,
            u.max_attempts,
            u.next_attempt_at,
            u.processing_started_at,
            u.last_attempt_at,
            u.delivered_at,
            u.error_code,
            u.error_message,
            u.terminal_reason,
            u.payload_json,
            e.event_type,
            e.category::text AS category,
            e.priority::text AS priority,
            e.title,
            e.body
        FROM updated u
        JOIN notification_events e
          ON e.id = u.event_id
        ORDER BY u.last_attempt_at ASC, u.id ASC
        """
        async with self._conn(conn) as c:
            rows = await c.fetch(
                q,
                channel,
                limit,
                stale_processing_after_minutes,
            )

        out: List[Dict[str, Any]] = []
        for row in rows:
            d = _row_to_dict(row)
            d["payload_json"] = _json_loose(d.get("payload_json"), {})
            out.append(d)
        return out

    async def list_due_deliveries(
        self,
        *,
        channel: Optional[str] = None,
        limit: int = 100,
        conn: Any = None,
    ) -> List[Dict[str, Any]]:
        """
        Read-only inspection helper. Does not claim rows.
        """
        q = """
        SELECT
            d.id::text,
            d.event_id::text,
            d.user_id::text,
            d.channel::text AS channel,
            d.destination,
            d.provider,
            d.provider_message_id,
            d.status::text AS status,
            d.attempt_count,
            d.max_attempts,
            d.next_attempt_at,
            d.processing_started_at,
            d.last_attempt_at,
            d.delivered_at,
            d.error_code,
            d.error_message,
            d.terminal_reason,
            d.payload_json,
            e.event_type,
            e.category::text AS category,
            e.priority::text AS priority,
            e.title,
            e.body
        FROM notification_deliveries d
        JOIN notification_events e
          ON e.id = d.event_id
        WHERE
          (
            (d.status = 'queued'::notification_delivery_status AND d.next_attempt_at <= now())
            OR
            d.status = 'processing'::notification_delivery_status
          )
          AND ($1::text IS NULL OR d.channel::text = $1::text)
        ORDER BY d.next_attempt_at ASC, d.created_at ASC
        LIMIT $2
        """
        async with self._conn(conn) as c:
            rows = await c.fetch(q, channel, limit)

        out: List[Dict[str, Any]] = []
        for row in rows:
            d = _row_to_dict(row)
            d["payload_json"] = _json_loose(d.get("payload_json"), {})
            out.append(d)
        return out

    async def schedule_delivery_retry(
        self,
        *,
        delivery_id: str,
        next_attempt_at: datetime,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        conn: Any = None,
    ) -> None:
        q = """
        UPDATE notification_deliveries
        SET
            status = 'queued'::notification_delivery_status,
            next_attempt_at = $2,
            processing_started_at = NULL,
            error_code = $3,
            error_message = $4,
            updated_at = now()
        WHERE id = $1::uuid
        """
        async with self._conn(conn) as c:
            await c.execute(
                q,
                delivery_id,
                next_attempt_at,
                error_code,
                error_message,
            )

    async def mark_delivery_delivered(
        self,
        *,
        delivery_id: str,
        provider_message_id: Optional[str] = None,
        conn: Any = None,
    ) -> None:
        q = """
        UPDATE notification_deliveries
        SET
            status = 'delivered'::notification_delivery_status,
            provider_message_id = COALESCE($2, provider_message_id),
            delivered_at = now(),
            processing_started_at = NULL,
            error_code = NULL,
            error_message = NULL,
            terminal_reason = NULL,
            updated_at = now()
        WHERE id = $1::uuid
        """
        async with self._conn(conn) as c:
            await c.execute(q, delivery_id, provider_message_id)

    async def mark_delivery_failed(
        self,
        *,
        delivery_id: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        terminal_reason: Optional[str] = None,
        conn: Any = None,
    ) -> None:
        q = """
        UPDATE notification_deliveries
        SET
            status = 'failed'::notification_delivery_status,
            processing_started_at = NULL,
            error_code = $2,
            error_message = $3,
            terminal_reason = $4,
            updated_at = now()
        WHERE id = $1::uuid
        """
        async with self._conn(conn) as c:
            await c.execute(
                q,
                delivery_id,
                error_code,
                error_message,
                terminal_reason,
            )

    async def mark_delivery_skipped(
        self,
        *,
        delivery_id: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        terminal_reason: Optional[str] = None,
        conn: Any = None,
    ) -> None:
        q = """
        UPDATE notification_deliveries
        SET
            status = 'skipped'::notification_delivery_status,
            processing_started_at = NULL,
            error_code = $2,
            error_message = $3,
            terminal_reason = $4,
            updated_at = now()
        WHERE id = $1::uuid
        """
        async with self._conn(conn) as c:
            await c.execute(
                q,
                delivery_id,
                error_code,
                error_message,
                terminal_reason,
            )

    async def update_delivery_status(
        self,
        *,
        delivery_id: str,
        status: str,
        provider_message_id: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        terminal_reason: Optional[str] = None,
        next_attempt_at: Optional[datetime] = None,
        conn: Any = None,
    ) -> None:
        """
        Backward-compatible generic updater.
        Does NOT increment attempt_count.
        Dispatcher should prefer the explicit methods above.
        """
        q = """
        UPDATE notification_deliveries
        SET
            status = $2::notification_delivery_status,
            provider_message_id = COALESCE($3, provider_message_id),
            delivered_at = CASE
                WHEN $2::notification_delivery_status = 'delivered' THEN now()
                ELSE delivered_at
            END,
            next_attempt_at = COALESCE($6, next_attempt_at),
            processing_started_at = CASE
                WHEN $2::notification_delivery_status = 'processing' THEN now()
                ELSE NULL
            END,
            error_code = $4,
            error_message = $5,
            terminal_reason = $7,
            updated_at = now()
        WHERE id = $1::uuid
        """
        async with self._conn(conn) as c:
            await c.execute(
                q,
                delivery_id,
                status,
                provider_message_id,
                error_code,
                error_message,
                next_attempt_at,
                terminal_reason,
            )