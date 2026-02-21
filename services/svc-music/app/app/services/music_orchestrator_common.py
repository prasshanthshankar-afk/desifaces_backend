from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.domain.enums import MusicJobStage, MusicProjectMode, MusicTrackType

JsonDict = Dict[str, Any]


def _as_dict(x: Any) -> JsonDict:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        if s.startswith("{") or s.startswith("["):
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
        return {}
    return {}


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return []
        if s.startswith("[") or s.startswith("{"):
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, list) else []
            except Exception:
                return []
        return []
    return []


def _normalize_jsonb_payload(x: Any) -> JsonDict:
    """
    Handles jsonb that is:
      - dict already
      - JSON string representing dict
      - JSON string scalar whose value is JSON text (double-json)
    """
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        for _ in range(2):
            try:
                obj = json.loads(s)
            except Exception:
                return {}
            if isinstance(obj, dict):
                return obj
            if isinstance(obj, str):
                s = obj.strip()
                continue
            return {}
        return {}
    return {}


def _stable_json(obj: Any) -> str:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return str(obj)


def _guess_ext_from_url(url: str) -> str:
    try:
        p = urlparse(url).path.lower()
        for ext in (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".mp4"):
            if p.endswith(ext):
                return ext
    except Exception:
        pass
    return ".mp3"


def _download_http_to_file(
    url: str,
    dst,
    *,
    timeout_s: int = 30,
    max_bytes: int = 150 * 1024 * 1024,
) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": "svc-music-downloader"})
    with urlopen(req, timeout=timeout_s) as r:
        n = 0
        with open(dst, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                n += len(chunk)
                if max_bytes and n > max_bytes:
                    raise RuntimeError("download_too_large")
                f.write(chunk)


def _clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


def _is_truthy(x: Any) -> bool:
    if x is True:
        return True
    if x is False or x is None:
        return False
    if isinstance(x, (int, float)):
        return x != 0
    if isinstance(x, str):
        return x.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(x)


def _guess_audio_content_type(url: Optional[str], default: str = "audio/mpeg") -> str:
    if not url:
        return default
    s = str(url).split("?", 1)[0].lower()
    if s.endswith(".wav"):
        return "audio/wav"
    if s.endswith(".mp3"):
        return "audio/mpeg"
    if s.endswith(".m4a") or s.endswith(".mp4"):
        return "audio/mp4"
    if s.endswith(".aac"):
        return "audio/aac"
    if s.endswith(".ogg") or s.endswith(".opus"):
        return "audio/ogg"
    return default


def _progress01(raw: Any) -> float:
    try:
        p = float(raw or 0)
    except Exception:
        return 0.0
    if p <= 0:
        return 0.0
    if p > 1.0:
        return min(1.0, p / 100.0)
    return min(1.0, p)


def _progress_for_stage(raw: Any) -> int:
    return int(round(_progress01(raw) * 100))


def _safe_stage(val: str | None) -> Optional[MusicJobStage]:
    if not val:
        return None
    try:
        return MusicJobStage(val)
    except Exception:
        return None


def _infer_stage_from_progress(progress_0_100: int) -> MusicJobStage:
    p = int(progress_0_100 or 0)
    if p < 10:
        return MusicJobStage.intent
    if p < 25:
        return MusicJobStage.creative_brief
    if p < 35:
        return MusicJobStage.lyrics
    if p < 45:
        return MusicJobStage.arrangement
    if p < 60:
        return MusicJobStage.provider_route
    if p < 75:
        return MusicJobStage.generate_audio
    if p < 82:
        return MusicJobStage.align_lyrics
    if p < 90:
        return MusicJobStage.generate_performer_videos
    if p < 97:
        return MusicJobStage.compose_video
    return MusicJobStage.publish


def _normalize_mode(val: Any) -> str:
    v = getattr(val, "value", val)
    s = str(v or "").strip()
    if not s:
        return MusicProjectMode.autopilot.value
    return s.lower()


def _normalize_outputs(input_json: JsonDict) -> List[str]:
    outs = _as_list(input_json.get("outputs"))
    out_strs: List[str] = []
    for x in outs:
        if x is None:
            continue
        v = str(x).strip().lower()
        if not v:
            continue
        try:
            MusicTrackType(v)
            out_strs.append(v)
        except Exception:
            continue

    if not out_strs:
        out_strs = [MusicTrackType.full_mix.value]

    seen = set()
    dedup: List[str] = []
    for o in out_strs:
        if o not in seen:
            seen.add(o)
            dedup.append(o)
    return dedup


def _track_url(meta: Any) -> Optional[str]:
    m = _as_dict(meta)
    return m.get("url") or m.get("byo_audio_url") or m.get("audio_master_url")


def _track_ct(meta: Any) -> Optional[str]:
    m = _as_dict(meta)
    return m.get("content_type") or m.get("mime")