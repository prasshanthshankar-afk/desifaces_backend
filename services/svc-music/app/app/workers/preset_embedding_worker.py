from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import List, Optional, Sequence, Tuple
from uuid import UUID

import httpx

from app.db import get_pool

log = logging.getLogger("preset_embedding_worker")


def _env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    v = os.getenv(name, default)
    if required and (v is None or v == ""):
        raise RuntimeError(f"Missing required env var: {name}")
    return v or ""


def _vec_to_pgvector(vec: Sequence[float]) -> str:
    # Store as text and cast to ::vector in SQL
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


async def _fetch_batch(tag: str, batch_size: int) -> List[Tuple[UUID, str]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, text_for_embedding
            from public.music_style_presets
            where embedding is null
              and tags @> array[$1]::text[]
              and text_for_embedding is not null
              and length(text_for_embedding) >= 20
            order by updated_at asc nulls first, created_at asc
            limit $2
            """,
            tag,
            batch_size,
        )
    return [(r["id"], r["text_for_embedding"]) for r in rows]


async def _update_embeddings(pairs: List[Tuple[UUID, str]]) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = 0
            for preset_id, vec_text in pairs:
                res = await conn.execute(
                    """
                    update public.music_style_presets
                    set embedding = $2::vector,
                        updated_at = now()
                    where id = $1
                      and embedding is null
                    """,
                    preset_id,
                    vec_text,
                )
                if res.endswith("1"):
                    updated += 1
            return updated


async def _count_remaining(tag: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            """
            select count(*)
            from public.music_style_presets
            where tags @> array[$1]::text[]
              and embedding is null
            """,
            tag,
        )
    return int(n or 0)


async def _azure_embed_texts(
    client: httpx.AsyncClient,
    endpoint: str,
    api_version: str,
    deployment: str,
    api_key: str,
    texts: List[str],
) -> List[List[float]]:
    url = f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/embeddings"
    params = {"api-version": api_version}
    headers = {"api-key": api_key, "Content-Type": "application/json"}

    # Azure accepts just {"input":[...]} since deployment is in URL
    payload = {"input": texts}

    r = await client.post(url, params=params, headers=headers, json=payload, timeout=60.0)

    if r.status_code in (429, 500, 502, 503, 504):
        raise RuntimeError(f"Transient Azure embedding error {r.status_code}: {r.text[:300]}")

    r.raise_for_status()
    data = r.json()
    return [item["embedding"] for item in data["data"]]


async def main():
    logging.basicConfig(level=_env("LOG_LEVEL", "INFO"))

    tag = _env("PRESET_EMBED_TAG", "seed_v120")
    batch_size = int(_env("PRESET_EMBED_BATCH_SIZE", "16"))
    sleep_secs = float(_env("PRESET_EMBED_SLEEP_SECS", "1.2"))
    dim_expected = int(_env("PRESET_EMBED_DIM", "1536"))

    endpoint = _env("AZURE_OPENAI_ENDPOINT", required=True)
    api_key = _env("AZURE_OPENAI_KEY", required=True)
    api_version = _env("AZURE_OPENAI_API_VERSION", required=True)
    deployment = _env("AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT", required=True)

    log.info(
        "Preset embedder starting tag=%s batch=%s dim=%s deployment=%s",
        tag, batch_size, dim_expected, deployment
    )

    async with httpx.AsyncClient() as http:
        backoff = 2.0

        while True:
            batch = await _fetch_batch(tag, batch_size)

            if not batch:
                remaining = await _count_remaining(tag)
                log.info("No work. remaining=%s. Sleeping 10s.", remaining)
                await asyncio.sleep(10.0)
                continue

            ids = [pid for pid, _ in batch]
            texts = [txt[:8000] for _, txt in batch]  # keep bounded

            try:
                vectors = await _azure_embed_texts(
                    http, endpoint, api_version, deployment, api_key, texts
                )

                for v in vectors:
                    if len(v) != dim_expected:
                        raise RuntimeError(f"Embedding dim mismatch got={len(v)} expected={dim_expected}")

                vec_texts = [_vec_to_pgvector(v) for v in vectors]
                updated = await _update_embeddings(list(zip(ids, vec_texts)))

                remaining = await _count_remaining(tag)
                log.info("Embedded=%s updated=%s remaining=%s", len(batch), updated, remaining)

                backoff = 2.0
                await asyncio.sleep(sleep_secs + random.random() * 0.25)

            except Exception as e:
                log.exception("Embed batch failed: %s", e)
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 1.8, 30.0)


if __name__ == "__main__":
    asyncio.run(main())