from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class ScriptSegment:
    index: int
    text: str
    duration_sec: int
    word_count: int


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WS_RE = re.compile(r"\s+")


def _normalize_text(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip())


def _split_sentences(text: str) -> List[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(normalized) if p.strip()]
    return parts or [normalized]


def _estimate_duration_sec(text: str, wpm: int) -> int:
    words = len(text.split())
    if words <= 0:
        return 1
    minutes = words / max(1, wpm)
    return max(1, int(math.ceil(minutes * 60.0)))


def split_script_into_segments(
    script_text: str,
    *,
    target_segment_seconds: int = 60,
    max_segment_seconds: int = 120,
    wpm: int = 150,
) -> List[ScriptSegment]:
    """
    Split a script into sentence-aware chunks sized for downstream fusion limits.

    Returns ScriptSegment objects with:
      - index
      - text
      - duration_sec
      - word_count
    """
    script_text = _normalize_text(script_text)
    if not script_text:
        return []

    target_segment_seconds = max(1, int(target_segment_seconds))
    max_segment_seconds = max(1, int(max_segment_seconds))
    wpm = max(1, int(wpm))

    # Convert duration targets into approximate word budgets.
    target_words = max(1, int(round((target_segment_seconds / 60.0) * wpm)))
    max_words = max(target_words, int(round((max_segment_seconds / 60.0) * wpm)))

    sentences = _split_sentences(script_text)

    segments: List[ScriptSegment] = []
    current_sentences: List[str] = []
    current_words = 0

    def flush() -> None:
        nonlocal current_sentences, current_words
        if not current_sentences:
            return
        text = " ".join(current_sentences).strip()
        segments.append(
            ScriptSegment(
                index=len(segments),
                text=text,
                duration_sec=min(max_segment_seconds, _estimate_duration_sec(text, wpm)),
                word_count=len(text.split()),
            )
        )
        current_sentences = []
        current_words = 0

    for sentence in sentences:
        sent_words = len(sentence.split())

        # Very long single sentence: split directly by max_words.
        if sent_words > max_words:
            flush()
            words = sentence.split()
            for i in range(0, len(words), max_words):
                chunk_words = words[i : i + max_words]
                chunk_text = " ".join(chunk_words).strip()
                if not chunk_text:
                    continue
                segments.append(
                    ScriptSegment(
                        index=len(segments),
                        text=chunk_text,
                        duration_sec=min(max_segment_seconds, _estimate_duration_sec(chunk_text, wpm)),
                        word_count=len(chunk_text.split()),
                    )
                )
            continue

        proposed_words = current_words + sent_words

        # If adding this sentence would exceed target and we already have content, flush first.
        if current_sentences and proposed_words > target_words:
            flush()

        current_sentences.append(sentence)
        current_words += sent_words

        # Safety flush if we somehow hit/exceed max_words.
        if current_words >= max_words:
            flush()

    flush()
    return segments


class ChunkingService:
    """
    Backward-compatible helper wrapper.
    """

    def split_spoken_text(self, text: str, max_words_per_chunk: int = 28) -> List[str]:
        words = (text or "").split()
        if not words:
            return []

        chunks: List[str] = []
        for idx in range(0, len(words), max(1, int(max_words_per_chunk))):
            chunk = " ".join(words[idx : idx + max(1, int(max_words_per_chunk))]).strip()
            if chunk:
                chunks.append(chunk)
        return chunks

    def split_script_into_segments(
        self,
        script_text: str,
        *,
        target_segment_seconds: int = 60,
        max_segment_seconds: int = 120,
        wpm: int = 150,
    ) -> List[ScriptSegment]:
        return split_script_into_segments(
            script_text,
            target_segment_seconds=target_segment_seconds,
            max_segment_seconds=max_segment_seconds,
            wpm=wpm,
        )