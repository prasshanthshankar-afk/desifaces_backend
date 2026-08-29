from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from pathlib import Path
import re
from typing import Iterable

from langchain_openai import OpenAIEmbeddings

from .config import settings

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,}", re.IGNORECASE)


@dataclass(frozen=True)
class KnowledgeChunk:
    ref: str
    title: str
    text: str


def _chunk_document(path: Path, text: str) -> list[KnowledgeChunk]:
    parts = [part.strip() for part in re.split(r"\n(?=#{1,3}\s)", text) if part.strip()]
    if not parts:
        parts = [text.strip()]
    chunks = []
    for index, part in enumerate(parts):
        title = path.stem.replace("_", " ").title()
        first = part.splitlines()[0].lstrip("# ").strip() if part else title
        chunks.append(KnowledgeChunk(ref=f"{path.name}#{index + 1}", title=first or title, text=part[:8000]))
    return chunks


def _tokens(text: str) -> set[str]:
    return {x.lower() for x in _WORD_RE.findall(text)}


def _cosine(a: Iterable[float], b: Iterable[float]) -> float:
    av, bv = list(a), list(b)
    dot = sum(x * y for x, y in zip(av, bv))
    an = sqrt(sum(x * x for x in av))
    bn = sqrt(sum(y * y for y in bv))
    if not an or not bn:
        return 0.0
    return dot / (an * bn)


class SafeKnowledgeRetriever:
    def __init__(self) -> None:
        root = Path(settings.DF_ASSISTANT_KNOWLEDGE_DIR)
        self._chunks: list[KnowledgeChunk] = []
        if root.exists():
            for path in sorted(root.glob("*.md")):
                self._chunks.extend(_chunk_document(path, path.read_text(encoding="utf-8")))
        self._embeddings = (
            OpenAIEmbeddings(model=settings.DF_ASSISTANT_EMBEDDING_MODEL)
            if settings.DF_ASSISTANT_EMBEDDING_MODEL and settings.OPENAI_API_KEY
            else None
        )
        self._vectors: list[list[float]] | None = None

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    async def retrieve(self, query: str) -> list[KnowledgeChunk]:
        if not self._chunks:
            return []
        top_k = max(1, min(settings.DF_ASSISTANT_RAG_TOP_K, 10))

        if self._embeddings is not None:
            if self._vectors is None:
                self._vectors = await self._embeddings.aembed_documents([c.text for c in self._chunks])
            qv = await self._embeddings.aembed_query(query)
            ranked = sorted(
                zip(self._chunks, self._vectors),
                key=lambda item: _cosine(qv, item[1]),
                reverse=True,
            )
            return [chunk for chunk, _ in ranked[:top_k]]

        q = _tokens(query)
        scored = []
        for chunk in self._chunks:
            overlap = len(q & _tokens(f"{chunk.title} {chunk.text}"))
            if overlap:
                scored.append((overlap, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [chunk for _, chunk in scored[:top_k]]
