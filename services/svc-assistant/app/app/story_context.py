from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import asyncpg
import httpx

from .config import settings
from .context import ContextResolver
from .schemas import AssistantContextLocator


def project_safe_recent_stories(raw: Any) -> dict[str, Any]:
    """Project Director Story discovery into non-identifying Assistant context.

    The Director API is already authenticated and user-scoped.  This second
    projection is intentionally stricter because its output may enter the LLM:
    no Story/Workflow/Thread IDs, continuation paths, titles or customer prose.
    """
    items: list[dict[str, Any]] = []
    for index, story in enumerate(list(raw or ())[:10], start=1):
        if not isinstance(story, dict):
            continue
        state = str(story.get("attention_state") or story.get("workflow_state") or story.get("state") or "").strip()[:80]
        workflow_state = str(story.get("workflow_state") or "").strip()[:80]
        current_stage = str(story.get("current_stage") or "").strip()[:80]
        updated_at = str(story.get("updated_at") or "").strip()[:80]
        items.append(
            {
                "alias": f"Recent story {index}",
                "state": state,
                "workflow_state": workflow_state,
                "current_stage": current_stage,
                "updated_at": updated_at,
                "needs_review": state.lower() == "awaiting_review",
                "needs_attention": state.lower() == "failed",
            }
        )

    return {
        "items": items,
        "count": len(items),
        "needs_review_count": sum(1 for item in items if item["needs_review"]),
        "needs_attention_count": sum(1 for item in items if item["needs_attention"]),
    }


class StoryAwareContextResolver(ContextResolver):
    """Extend the existing bounded resolver with safe recent-Story discovery.

    Retrieval remains a deterministic application responsibility.  The LLM never
    receives a Director bearer token, SQL capability, raw Director response or a
    durable identifier that could be used to fetch another customer's Story.
    """

    def __init__(self, client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
        super().__init__(client, pool)
        self._story_client = client

    async def _fetch_recent_stories(self, *, token: str) -> dict[str, Any]:
        try:
            response = await self._story_client.get(
                f"{settings.DF_DIRECTOR_BASE_URL}/api/director/stories/recent",
                params={"limit": 10},
                headers={"Authorization": f"Bearer {token}"},
            )
            # Authorization failures must fail closed.  Other operational failures
            # degrade only this optional context source so Piku can still answer
            # from safe dashboard/generation context and product knowledge.
            if response.status_code in {401, 403}:
                response.raise_for_status()
            if response.status_code >= 400:
                return project_safe_recent_stories([])
            raw = response.json()
            if not isinstance(raw, list):
                return project_safe_recent_stories([])
            return project_safe_recent_stories(raw)
        except httpx.HTTPStatusError:
            raise
        except (httpx.HTTPError, ValueError):
            return project_safe_recent_stories([])

    async def resolve(
        self,
        locator: AssistantContextLocator,
        *,
        token: str,
        user_id: UUID,
    ) -> dict[str, Any]:
        live_task = super().resolve(locator, token=token, user_id=user_id)
        stories_task = self._fetch_recent_stories(token=token)
        live, recent_stories = await asyncio.gather(live_task, stories_task)

        merged = dict(live)
        merged["recent_stories"] = recent_stories
        if recent_stories.get("count"):
            merged["live_context_available"] = True
        return merged
