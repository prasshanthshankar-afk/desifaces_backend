from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import settings
from .privacy import redact_sensitive_text


@dataclass(frozen=True)
class RecentStory:
    label: str
    state: str
    updated_at: str | None
    continue_path: str


class RecentStoryResolver:
    """Fetch authenticated, user-scoped Director Story metadata.

    Director scopes the endpoint by account + owner user. Story UUIDs never enter
    LLM/model context; only the browser navigation action carries the opaque path.
    Human-readable title text is privacy-redacted before it is echoed back.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def recent(self, *, token: str, limit: int = 5) -> list[RecentStory]:
        response = await self._client.get(
            f"{settings.DF_DIRECTOR_BASE_URL}/api/director/stories/recent",
            params={"limit": max(1, min(int(limit), 10))},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, list):
            raise ValueError("director_recent_stories_invalid_response")

        stories: list[RecentStory] = []
        for index, item in enumerate(raw[:10], start=1):
            if not isinstance(item, dict):
                continue
            path = str(item.get("continue_path") or "").strip()
            if not path.startswith("/app/multi-person?story="):
                continue
            title = redact_sensitive_text(str(item.get("title") or "").strip()).text
            label = title[:120] if title else f"Recent multi-person story {index}"
            stories.append(
                RecentStory(
                    label=label,
                    state=str(item.get("state") or "unknown")[:80],
                    updated_at=str(item.get("updated_at"))[:80] if item.get("updated_at") else None,
                    continue_path=path,
                )
            )
        return stories


def is_recent_story_query(message: str) -> bool:
    text = " ".join(str(message or "").lower().replace("_", " ").split())
    story = any(cue in text for cue in ("story", "multi-person", "multi person", "multiperson"))
    recent = any(cue in text for cue in (
        "last", "latest", "most recent", "recent", "working on", "continue", "resume", "retrieve",
    ))
    return story and recent
