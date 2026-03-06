# services/svc-marketing/app/app/services/orchestration/utils/config.py
from __future__ import annotations

import os
from typing import Any

from app.config import settings


def cfg_str(name: str, default: str = "") -> str:
    v: Any = getattr(settings, name, None)
    if v is None or str(v).strip() == "":
        v = os.getenv(name, "")
    s = str(v).strip() if v is not None else ""
    return s if s else default


def cfg_int(name: str, default: int) -> int:
    s = cfg_str(name, "")
    if not s:
        return default
    try:
        return int(float(s))
    except Exception:
        return default


def cfg_float(name: str, default: float) -> float:
    s = cfg_str(name, "")
    if not s:
        return default
    try:
        return float(s)
    except Exception:
        return default


def cfg_bool(name: str, default: bool = False) -> bool:
    s = cfg_str(name, "")
    if not s:
        return default
    return s.strip().lower() in ("1", "true", "yes", "y", "on")