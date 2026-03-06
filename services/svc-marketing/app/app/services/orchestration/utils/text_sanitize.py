
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


_PREFIX_RE = re.compile(
    r"^\s*(script|voiceover|narration|audio script|tts|speaker)\s*[:\-]\s*",
    re.IGNORECASE,
)

_FENCE_RE = re.compile(r"^```.*?$|```$", re.MULTILINE)


def _strip_prefixes(s: str) -> str:
    s = s.strip()
    s = _FENCE_RE.sub("", s).strip()
    # remove markdown headings/bullets that sometimes appear in LLM outputs
    s = re.sub(r"^\s*#+\s*", "", s)
    s = re.sub(r"^\s*[\-\*\u2022]\s+", "", s)
    # remove "Script: ..." style prefixes
    s = _PREFIX_RE.sub("", s).strip()
    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def coerce_voice_text(x: Any) -> str:
    """
    Ensure we always end up with *plain narration text* (no dicts, no labels like 'Script:').
    This prevents 'script' from appearing in spoken audio, subtitles, or provider overlays.
    """
    if x is None:
        return ""

    if isinstance(x, str):
        return _strip_prefixes(x)

    if isinstance(x, dict):
        d: Dict[str, Any] = x
        # common keys we might store
        for k in ("voiceover_text", "voiceover", "narration", "audio_script", "script", "text", "tts_text"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return _strip_prefixes(v)
        # last resort: stringify safely, then strip prefixes
        try:
            return _strip_prefixes(json.dumps(d, ensure_ascii=False))
        except Exception:
            return _strip_prefixes(str(d))

    # lists/other objects
    return _strip_prefixes(str(x))