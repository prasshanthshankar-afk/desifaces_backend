# services/svc-marketing/app/app/services/orchestration/utils/media_extract.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.orchestration.utils.jsonx import deep_find_url


def _is_img(u: str) -> bool:
    if not (isinstance(u, str) and u.startswith("http")):
        return False
    u2 = u.lower().split("?", 1)[0]
    return u2.endswith((".png", ".jpg", ".jpeg", ".webp"))


def _is_audio(u: str) -> bool:
    if not (isinstance(u, str) and u.startswith("http")):
        return False
    u2 = u.lower().split("?", 1)[0]
    return u2.endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg"))


def extract_image_urls(resp: Any, limit: int = 24) -> List[str]:
    out: List[str] = []

    def add(u: Any) -> None:
        if isinstance(u, str) and _is_img(u) and u not in out:
            out.append(u)

    def walk(x: Any) -> None:
        if x is None or len(out) >= limit:
            return
        if isinstance(x, str):
            add(x)
            return
        if isinstance(x, dict):
            add(x.get("image_url"))
            add(x.get("url"))
            v = x.get("variants")
            if isinstance(v, list):
                for it in v:
                    if isinstance(it, dict):
                        add(it.get("url"))
                        add(it.get("image_url"))
            elif isinstance(v, dict):
                for it in v.values():
                    if isinstance(it, dict):
                        add(it.get("url"))
                        add(it.get("image_url"))
            for vv in x.values():
                walk(vv)
            return
        if isinstance(x, list):
            for it in x:
                walk(it)

    walk(resp)
    return out


def deep_find_image_url(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj if _is_img(obj) else None
    if isinstance(obj, dict):
        for k in ("image_url", "url"):
            v = obj.get(k)
            if isinstance(v, str) and _is_img(v):
                return v
        for v in obj.values():
            u = deep_find_image_url(v)
            if u:
                return u
    if isinstance(obj, list):
        for it in obj:
            u = deep_find_image_url(it)
            if u:
                return u
    return None


def deep_find_audio_url(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj if _is_audio(obj) else None
    if isinstance(obj, dict):
        for k in ("audio_url", "music_url", "url"):
            v = obj.get(k)
            if isinstance(v, str) and _is_audio(v):
                return v
        for v in obj.values():
            u = deep_find_audio_url(v)
            if u:
                return u
    if isinstance(obj, list):
        for it in obj:
            u = deep_find_audio_url(it)
            if u:
                return u
    return None


def extract_media(resp: Any) -> Dict[str, Optional[str]]:
    """
    Returns: {job_id, status, video_url, audio_url}
    """
    out: Dict[str, Optional[str]] = {"video_url": None, "audio_url": None, "job_id": None, "status": None}

    def _as(d: Any) -> Dict[str, Any]:
        return d if isinstance(d, dict) else {}

    top = _as(resp)
    submit = _as(top.get("submit"))
    final = _as(top.get("final"))
    result = _as(top.get("result")) or _as(top.get("output"))

    for d in (top, submit, final, result):
        for k in ("job_id", "studio_job_id", "id", "run_id"):
            v = d.get(k)
            if isinstance(v, str) and v:
                out["job_id"] = v
                break
        if out["job_id"]:
            break

    for d in (final, top, submit):
        for k in ("status", "state"):
            v = d.get(k)
            if isinstance(v, str) and v:
                out["status"] = v.lower()
                break
        if out["status"]:
            break

    out["video_url"] = deep_find_url(resp)
    out["audio_url"] = deep_find_audio_url(resp) or deep_find_url(resp)

    return out