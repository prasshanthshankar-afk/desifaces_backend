from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional, Tuple

from app.db import get_pool


def _vec_to_pgvector_literal(vec: List[float]) -> str:
    """
    pgvector accepts a text literal: '[0.1,0.2,...]'

    Guardrails:
      - replace NaN/inf with 0.0 to avoid cast errors
      - format to fixed precision for stable query strings
    """
    cleaned: List[float] = []
    for x in (vec or []):
        try:
            fx = float(x)
            if math.isnan(fx) or math.isinf(fx):
                fx = 0.0
        except Exception:
            fx = 0.0
        cleaned.append(fx)

    return "[" + ",".join(f"{x:.6f}" for x in cleaned) + "]"


def _candidate_tags(tag: Optional[str]) -> List[Optional[str]]:
    """
    Try env tag first, then known seed tag, then no-tag fallback.
    """
    out: List[Optional[str]] = []
    seen = set()

    def add(x: Optional[str]) -> None:
        k = (x or "").strip() or None
        if k in seen:
            return
        seen.add(k)
        out.append(k)

    add(tag or os.getenv("MUSIC_PRESET_TAG", "seed_v120"))
    add("seed_v120")
    add(None)  # no tag filter
    return out


async def _query_presets(
    *,
    query_embedding: List[float],
    preset_type: str,
    k: int,
    tag: Optional[str],
) -> List[Dict[str, Any]]:
    pool = await get_pool()
    vec_lit = _vec_to_pgvector_literal(query_embedding)

    if tag:
        rows = await pool.fetch(
            """
            select
              id,
              preset_type,
              name,
              tags,
              content,
              (embedding <=> $3::vector) as distance
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
    else:
        # no tag constraint fallback
        rows = await pool.fetch(
            """
            select
              id,
              preset_type,
              name,
              tags,
              content,
              (embedding <=> $2::vector) as distance
            from public.music_style_presets
            where preset_type = $1
              and embedding is not null
            order by embedding <=> $2::vector
            limit $3
            """,
            str(preset_type),
            vec_lit,
            int(k),
        )

    out = [dict(r) for r in (rows or [])]
    # annotate tag used for debugging/UI
    for r in out:
        r["tag_used"] = tag
    return out


async def search_presets(
    *,
    query_embedding: List[float],
    preset_type: str,
    k: int = 6,
    tag: str | None = None,
) -> List[Dict[str, Any]]:
    """
    Top-K by cosine distance (<=>). Requires embedding column populated.

    Reliability:
      - tries (tag or MUSIC_PRESET_TAG) -> seed_v120 -> no-tag
      - returns `distance` + `tag_used` for observability
    """
    if not query_embedding:
        return []

    k = max(1, int(k or 6))
    preset_type = str(preset_type or "").strip()
    if not preset_type:
        return []

    last_nonempty: List[Dict[str, Any]] = []
    for candidate in _candidate_tags(tag):
        try:
            rows = await _query_presets(
                query_embedding=query_embedding,
                preset_type=preset_type,
                k=k,
                tag=candidate,
            )
            if rows:
                return rows
            last_nonempty = rows
        except Exception:
            # continue trying fallbacks
            continue

    return last_nonempty