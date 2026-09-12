from __future__ import annotations

import json
from uuid import UUID

from redis.asyncio import Redis

from .config import settings


class SessionStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(account_id: UUID, user_id: UUID, session_id: UUID) -> str:
        return f"assistant:v3:{account_id}:{user_id}:{session_id}"

    async def history(self, *, account_id: UUID, user_id: UUID, session_id: UUID) -> list[dict]:
        value = await self._redis.get(self._key(account_id, user_id, session_id))
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [x for x in parsed if isinstance(x, dict)][-settings.DF_ASSISTANT_MAX_HISTORY_MESSAGES:]

    async def append(
        self,
        *,
        account_id: UUID,
        user_id: UUID,
        session_id: UUID,
        role: str,
        content: str,
    ) -> None:
        history = await self.history(account_id=account_id, user_id=user_id, session_id=session_id)
        history.append({"role": role, "content": content[:8000]})
        history = history[-settings.DF_ASSISTANT_MAX_HISTORY_MESSAGES:]
        await self._redis.setex(
            self._key(account_id, user_id, session_id),
            max(300, settings.DF_ASSISTANT_SESSION_TTL_SECONDS),
            json.dumps(history, ensure_ascii=False),
        )

    async def delete(self, *, account_id: UUID, user_id: UUID, session_id: UUID) -> None:
        await self._redis.delete(self._key(account_id, user_id, session_id))
