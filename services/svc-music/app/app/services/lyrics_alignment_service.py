from __future__ import annotations

from typing import Any, Dict, List


def naive_timed_lyrics(lyrics_text: str, duration_ms: int, *, language: str | None = None) -> Dict[str, Any]:
    """
    Deterministic, dependency-free timed lyrics for E2E.
    Splits duration across non-empty lines and words.

    Output format is stable so we can later swap in premium alignment (whisperx/MFA/etc.)
    without breaking downstream consumers.
    """
    duration_ms = max(1, int(duration_ms or 1))
    lines = [ln.strip() for ln in (lyrics_text or "").splitlines()]
    lines = [ln for ln in lines if ln]

    if not lines:
        return {"version": 1, "language": language, "segments": []}

    n = len(lines)
    base = duration_ms // n
    rem = duration_ms % n

    segments: List[Dict[str, Any]] = []
    t = 0
    for i, line in enumerate(lines):
        seg_dur = base + (1 if i < rem else 0)
        start = t
        end = min(duration_ms, t + seg_dur)
        t = end

        words = [w for w in line.split(" ") if w]
        if not words:
            segments.append({"start_ms": start, "end_ms": end, "text": line, "words": []})
            continue

        wn = len(words)
        wbase = max(1, (end - start) // wn)
        wrem = (end - start) - (wbase * wn)

        wt = start
        witems = []
        for wi, w in enumerate(words):
            wdur = wbase + (1 if wi < wrem else 0)
            wstart = wt
            wend = min(end, wt + wdur)
            wt = wend
            witems.append({"w": w, "start_ms": wstart, "end_ms": wend})

        segments.append({"start_ms": start, "end_ms": end, "text": line, "words": witems})

    # ensure last segment ends at duration
    if segments:
        segments[-1]["end_ms"] = duration_ms
        if segments[-1]["words"]:
            segments[-1]["words"][-1]["end_ms"] = duration_ms

    return {"version": 1, "language": language, "segments": segments}