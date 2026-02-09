from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class ScriptChunk:
    index: int
    text: str
    duration_sec: int


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WS = re.compile(r"\s+")


def _estimate_duration_seconds(text: str, wpm: int) -> int:
    words = len([w for w in _WS.split(text.strip()) if w])
    if words <= 0:
        return 0
    # seconds = words / (wpm/60)
    sec = int(round(words * 60.0 / float(wpm)))
    return max(1, sec)


def split_script_into_segments(
    script_text: str,
    *,
    target_segment_seconds: int = 60,
    max_segment_seconds: int = 120,
    wpm: int = 150,
) -> List[ScriptChunk]:
    """
    Splits longform script into segments suitable for svc-fusion where
    VideoSettings.duration_sec <= 120.

    Strategy:
      - sentence-ish splitting (.,!,?)
      - greedy pack sentences into a segment until target reached
      - hard cap at max_segment_seconds (will flush current segment)
    """
    s = (script_text or "").strip()
    if not s:
        return []

    # Normalize whitespace
    s = _WS.sub(" ", s)

    # Split into sentences; if no punctuation, treat as one block
    parts = _SENT_SPLIT.split(s)
    parts = [p.strip() for p in parts if p and p.strip()]
    if not parts:
        return []

    # Guardrails
    target = max(10, int(target_segment_seconds))
    cap = max(10, int(max_segment_seconds))
    if cap < target:
        cap = target
    cap = min(cap, 120)   # hard svc-fusion limit
    target = min(target, cap)

    chunks: List[ScriptChunk] = []
    cur: List[str] = []
    cur_sec = 0

    def flush():
        nonlocal cur, cur_sec
        if not cur:
            return
        text = " ".join(cur).strip()
        dur = _estimate_duration_seconds(text, wpm)
        dur = max(1, min(cap, dur))
        chunks.append(ScriptChunk(index=len(chunks), text=text, duration_sec=dur))
        cur = []
        cur_sec = 0

    for sent in parts:
        sent_sec = _estimate_duration_seconds(sent, wpm)
        sent_sec = max(1, sent_sec)

        # If adding this sentence would exceed hard cap, flush current first
        if cur and (cur_sec + sent_sec) > cap:
            flush()

        # If a single sentence is longer than cap, we still accept it as its own chunk (duration will clamp)
        cur.append(sent)
        cur_sec = _estimate_duration_seconds(" ".join(cur), wpm)

        # If we've reached target, flush
        if cur_sec >= target:
            flush()

    flush()

    return chunks