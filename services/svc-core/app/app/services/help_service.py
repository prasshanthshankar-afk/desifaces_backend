from __future__ import annotations

from typing import Any, List

from fastapi import HTTPException, status

from app.repos.help_repo import HelpRepo
from app.schemas.help import HelpArticleResponse, HelpCategoryResponse


def _import_get_pool():
    try:
        from app.db import get_pool  # type: ignore
        return get_pool
    except Exception:
        from app.db.postgres import get_pool  # type: ignore
        return get_pool


class HelpService:
    def __init__(self, pool: Any):
        self.pool = pool
        self.repo = HelpRepo(pool)

    async def list_categories(self) -> List[HelpCategoryResponse]:
        rows = await self.repo.list_categories()
        return [
            HelpCategoryResponse(
                key=str(row["key"]),
                title=str(row["title"]),
                description=row.get("description"),
                sort_order=int(row.get("sort_order") or 100),
            )
            for row in rows
        ]

    async def list_faq(self) -> List[HelpArticleResponse]:
        rows = await self.repo.list_faq()
        return [
            HelpArticleResponse(
                slug=str(row["slug"]),
                category_key=str(row["category_key"]),
                title=str(row["title"]),
                summary=row.get("summary"),
                body_markdown=str(row["body_markdown"]),
                keywords=list(row.get("keywords") or []),
                is_faq=bool(row.get("is_faq", False)),
                sort_order=int(row.get("sort_order") or 100),
            )
            for row in rows
        ]

    async def get_article(self, *, slug: str) -> HelpArticleResponse:
        row = await self.repo.get_article_by_slug(slug)
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Help article not found",
            )

        return HelpArticleResponse(
            slug=str(row["slug"]),
            category_key=str(row["category_key"]),
            title=str(row["title"]),
            summary=row.get("summary"),
            body_markdown=str(row["body_markdown"]),
            keywords=list(row.get("keywords") or []),
            is_faq=bool(row.get("is_faq", False)),
            sort_order=int(row.get("sort_order") or 100),
        )


async def get_help_service() -> HelpService:
    get_pool = _import_get_pool()
    pool = await get_pool()
    return HelpService(pool)