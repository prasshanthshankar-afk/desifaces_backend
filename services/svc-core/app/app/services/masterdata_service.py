from __future__ import annotations

import os
from typing import Any, Dict

import asyncpg
from fastapi import HTTPException

from app.repos.masterdata_repo import MasterdataRepo
from app.services.masterdata_cache import MasterdataCache

_cache = MasterdataCache(ttl_seconds=int(os.getenv("MASTERDATA_CACHE_TTL_SEC", "3600")))
_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """
    Align with svc-core DB configuration:
    1) Prefer app.db.ensure_db_pool() if present (single canonical place)
    2) Otherwise use DATABASE_URL (your compose/.env uses this)
    3) Then DATABASE_DSN
    4) Finally fall back to DB_* vars (optional)
    """
    # 1) Canonical pool (if svc-core already has it)
    try:
        from app.db import ensure_db_pool  # type: ignore
        return await ensure_db_pool()
    except Exception:
        pass

    global _pool
    if _pool is not None:
        return _pool

    # 2) Prefer DATABASE_URL (your docker-compose passes this)
    dsn = os.getenv("DATABASE_URL") or os.getenv("DATABASE_DSN")

    # 3) Optional fallback
    if not dsn:
        host = os.getenv("DB_HOST", "desifaces-db")
        port = int(os.getenv("DB_PORT", "5432"))
        user = os.getenv("DB_USER", "desifaces_admin")
        pwd = os.getenv("DB_PASSWORD", "")
        db = os.getenv("DB_NAME", "desifaces")
        dsn = f"postgresql://{user}:{pwd}@{host}:{port}/{db}"

    if not dsn:
        # Fail clearly and as JSON
        raise HTTPException(status_code=500, detail="db_config_missing")

    try:
        _pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=int(os.getenv("DB_POOL_MIN", "1")),
            max_size=int(os.getenv("DB_POOL_MAX", "10")),
        )
        return _pool
    except Exception as e:
        # Return JSON so callers (and jq) can parse
        raise HTTPException(status_code=503, detail=f"db_unavailable:{type(e).__name__}")


class MasterdataService:
    def __init__(self, repo: MasterdataRepo, cache: MasterdataCache):
        self.repo = repo
        self.cache = cache

    async def get_version(self, domain: str) -> Dict[str, Any]:
        rev, ver = await self.repo.get_revision(domain)
        return {"domain": domain, "revision": rev, "version": ver}

    async def get_face(self, lang: str = "en") -> Dict[str, Any]:
        rev, _ver = await self.repo.get_revision("face")
        cache_key = f"face:{lang}"

        async def loader():
            lists = await self.repo.get_face_masterdata(lang=lang)
            return {"domain": "face", "revision": rev, "lang": lang, **lists}

        return await self.cache.get(cache_key, rev, loader)

    async def get_tts(self) -> Dict[str, Any]:
        rev, _ver = await self.repo.get_revision("tts")

        async def loader():
            lists = await self.repo.get_tts_masterdata()
            return {"domain": "tts", "revision": rev, **lists}

        return await self.cache.get("tts", rev, loader)


async def get_masterdata_service() -> MasterdataService:
    pool = await get_pool()
    repo = MasterdataRepo(pool)
    return MasterdataService(repo=repo, cache=_cache)