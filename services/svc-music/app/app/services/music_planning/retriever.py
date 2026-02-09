from __future__ import annotations

import os
from typing import Any, Dict, List

from app.db import get_pool


def _vec_to_pgvector_literal(vec: List[float]) -> str:
    # pgvector accepts: '[0.1,0.2,...]'
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


async def search_presets(
    *,
    query_embedding: List[float],
    preset_type: str,
    k: int = 6,
    tag: str | None = None,
) -> List[Dict[str, Any]]:
    """
    Top-K by cosine distance (<=>). Requires embedding column populated.
    Filters to tag (defaults to MUSIC_PRESET_TAG or seed_v120).
    """
    tag = tag or os.getenv("MUSIC_PRESET_TAG", "seed_v120")
    pool = await get_pool()

    vec_lit = _vec_to_pgvector_literal(query_embedding)

    rows = await pool.fetch(
        """
        select id, preset_type, name, tags, content
        from public.music_style_presets
        where tags @> $1::text[]
          and preset_type = $2
          and embedding is not null
        order by embedding <=> $3::vector
        limit $4
        """,
        [tag],
        str(preset_type),
        vec_lit,
        int(k),
    )
    return [dict(r) for r in rows]