# services/svc-marketing/app/app/services/orchestration/utils/jsonx.py
from __future__ import annotations

import json
from typing import Any, Dict, Optional


def as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            y = json.loads(s)
            return y if isinstance(y, dict) else {}
        except Exception:
            return {}
    try:
        return dict(x)
    except Exception:
        return {}


def truncate_json(obj: Any, max_chars: int = 2000) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    return s if len(s) <= max_chars else (s[: max_chars - 3] + "...")


def deep_find_url(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj if obj.startswith("http") else None
    if isinstance(obj, dict):
        for k in ("reel_url", "video_url", "final_url", "preview_url", "output_url", "url", "audio_url", "music_url"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for v in obj.values():
            u = deep_find_url(v)
            if u:
                return u
    if isinstance(obj, list):
        for it in obj:
            u = deep_find_url(it)
            if u:
                return u
    return None