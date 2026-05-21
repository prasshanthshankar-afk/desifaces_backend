from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row.items())
    except Exception:
        return dict(row)


class HelpRepo:
    def __init__(self, pool: Any):
        self.pool = pool

    @asynccontextmanager
    async def _conn(self, conn: Any = None) -> AsyncIterator[Any]:
        if conn is not None:
            yield conn
            return
        async with self.pool.acquire() as acquired:
            yield acquired

    async def list_categories(self, conn: Any = None) -> List[Dict[str, Any]]:
        q = """
        SELECT
            key,
            title,
            description,
            sort_order
        FROM help_categories
        WHERE is_active = TRUE
        ORDER BY sort_order ASC, key ASC
        """
        async with self._conn(conn) as c:
            rows = await c.fetch(q)
        return [_row_to_dict(r) for r in rows]

    async def list_faq(self, conn: Any = None) -> List[Dict[str, Any]]:
        q = """
        SELECT
            slug,
            category_key,
            title,
            summary,
            body_markdown,
            keywords,
            is_faq,
            sort_order
        FROM help_articles
        WHERE is_published = TRUE
          AND is_faq = TRUE
        ORDER BY sort_order ASC, created_at DESC
        """
        async with self._conn(conn) as c:
            rows = await c.fetch(q)

        out = []
        for row in rows:
            d = _row_to_dict(row)
            d["keywords"] = list(d.get("keywords") or [])
            out.append(d)
        return out

    async def get_article_by_slug(self, slug: str, conn: Any = None) -> Optional[Dict[str, Any]]:
        q = """
        SELECT
            slug,
            category_key,
            title,
            summary,
            body_markdown,
            keywords,
            is_faq,
            sort_order
        FROM help_articles
        WHERE slug = $1
          AND is_published = TRUE
        LIMIT 1
        """
        async with self._conn(conn) as c:
            row = await c.fetchrow(q, slug)
        if not row:
            return None
        d = _row_to_dict(row)
        d["keywords"] = list(d.get("keywords") or [])
        return d