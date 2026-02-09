from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional
import asyncio
import time

@dataclass
class CacheEntry:
    revision: int
    payload: Dict[str, Any]
    cached_at: float

class MasterdataCache:
    def __init__(self, ttl_seconds: int = 3600):
        self.ttl = ttl_seconds
        self._lock = asyncio.Lock()
        self._cache: Dict[str, CacheEntry] = {}

    async def get(self, domain: str, revision: int, loader) -> Dict[str, Any]:
        now = time.time()
        async with self._lock:
            entry = self._cache.get(domain)
            if entry and entry.revision == revision:
                return entry.payload
            if entry and (now - entry.cached_at) < self.ttl and entry.revision == revision:
                return entry.payload

            payload = await loader()
            self._cache[domain] = CacheEntry(revision=revision, payload=payload, cached_at=now)
            return payload

    async def invalidate(self, domain: str) -> None:
        async with self._lock:
            self._cache.pop(domain, None)