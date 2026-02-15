from __future__ import annotations

from app.db import get_pool


class BaseRepo:
    async def pool(self):
        return await get_pool()