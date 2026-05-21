from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field

class HelpCategoryResponse(BaseModel):
    key: str
    title: str
    description: Optional[str] = None
    sort_order: int

class HelpArticleResponse(BaseModel):
    slug: str
    category_key: str
    title: str
    summary: Optional[str] = None
    body_markdown: str
    keywords: List[str] = Field(default_factory=list)
    is_faq: bool = False
    sort_order: int = 100