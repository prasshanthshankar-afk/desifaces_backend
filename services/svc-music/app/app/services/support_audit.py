from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

import asyncpg


class SupportAuditService:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def upsert_session(
        self,
        *,
        user_id: UUID,
        project_id: UUID,
        job_id: Optional[UUID],
        surface: str,
    ) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM public.support_sessions
                WHERE user_id=$1 AND project_id=$2 AND surface=$3
                  AND status='open'
                  AND (job_id IS NOT DISTINCT FROM $4)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                user_id,
                project_id,
                surface,
                job_id,
            )
            if row:
                return dict(row)

            row2 = await conn.fetchrow(
                """
                INSERT INTO public.support_sessions(user_id, project_id, job_id, surface, status)
                VALUES($1,$2,$3,$4,'open')
                RETURNING *
                """,
                user_id,
                project_id,
                job_id,
                surface,
            )
            return dict(row2)

    async def session_belongs_to_user(self, *, session_id: UUID, user_id: UUID) -> bool:
        row = await self.pool.fetchrow(
            "SELECT 1 FROM public.support_sessions WHERE id=$1 AND user_id=$2",
            session_id,
            user_id,
        )
        return row is not None

    async def append_user_event(
        self,
        *,
        session_id: UUID,
        actor_user_id: UUID,
        kind: str,
        payload: dict[str, Any],
        request_id: Optional[str],
        ip: Optional[str],
        user_agent: Optional[str],
        retention_until=None,  # timestamp or None
    ) -> dict:
        """
        Inserts an end-user event.
        Important: support_events.user_id is legacy NOT NULL, so we set it = actor_user_id.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO public.support_events(
                    session_id,
                    user_id,
                    actor_type,
                    actor_user_id,
                    kind,
                    payload,
                    request_id,
                    ip,
                    user_agent,
                    retention_until
                )
                VALUES($1,$2,'user',$3,$4,$5,$6,$7,$8,$9)
                RETURNING
                    id,
                    created_at,
                    encode(prev_hash,'hex')  AS prev_hash_hex,
                    encode(event_hash,'hex') AS event_hash_hex
                """,
                session_id,
                actor_user_id,  # legacy user_id
                actor_user_id,
                kind,
                payload,
                request_id,
                ip,
                user_agent,
                retention_until,
            )
            return dict(row)

    async def append_admin_event(
        self,
        *,
        session_id: UUID,
        actor_admin_id: UUID,
        kind: str,
        payload: dict[str, Any],
        request_id: Optional[str],
        ip: Optional[str],
        user_agent: Optional[str],
        impersonated_user_id: Optional[UUID] = None,
        retention_until=None,
    ) -> dict:
        """
        Inserts an admin/support event.
        Production invariant: because support_events.user_id is NOT NULL, admin events must
        be tied to a user context -> impersonated_user_id is required.
        """
        if impersonated_user_id is None:
            raise ValueError("impersonated_user_id_required")

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO public.support_events(
                    session_id,
                    user_id,
                    actor_type,
                    actor_admin_id,
                    impersonated_user_id,
                    kind,
                    payload,
                    request_id,
                    ip,
                    user_agent,
                    retention_until
                )
                VALUES($1,$2,'admin',$3,$4,$5,$6,$7,$8,$9,$10)
                RETURNING
                    id,
                    created_at,
                    encode(prev_hash,'hex')  AS prev_hash_hex,
                    encode(event_hash,'hex') AS event_hash_hex
                """,
                session_id,
                impersonated_user_id,  # legacy user_id
                actor_admin_id,
                impersonated_user_id,
                kind,
                payload,
                request_id,
                ip,
                user_agent,
                retention_until,
            )
            return dict(row)