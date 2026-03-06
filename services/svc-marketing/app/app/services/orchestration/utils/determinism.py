# services/svc-marketing/app/app/services/orchestration/utils/determinism.py
from __future__ import annotations

import hashlib
from typing import List
from uuid import UUID


def stable_pick_index(key: str, n: int) -> int:
    if n <= 1:
        return 0
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % n


def stable_u32_from_run_id(run_id: UUID) -> int:
    return int(hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF


def pick_from_seed(seed: int, key: str, items: List[str]) -> str:
    if not items:
        return ""
    h = hashlib.sha256(f"{int(seed)}:{key}".encode("utf-8")).hexdigest()
    return items[int(h[:8], 16) % len(items)]