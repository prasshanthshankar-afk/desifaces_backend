import asyncpg
from app.config import settings

_db_pool: asyncpg.Pool | None = None

async def init_db() -> asyncpg.Pool:
    global _db_pool
    if _db_pool is None:
        _db_pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL, min_size=1, max_size=10)
    return _db_pool

async def get_db_pool() -> asyncpg.Pool:
    if _db_pool is None:
        return await init_db()
    return _db_pool

# aliases (so imports don’t break)
async def get_pool() -> asyncpg.Pool:
    return await get_db_pool()