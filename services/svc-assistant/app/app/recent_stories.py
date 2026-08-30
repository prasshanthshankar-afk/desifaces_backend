from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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

    The Director endpoint already scopes by account + owner user.  This resolver
    intentionally keeps the Story UUID out of LLM/model context; only the browser
    navigation path carries it.  Human-readable title text is privacy-redacted
    before it is echoed back to the same authenticated user.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def recent(self, *, token: str, limit: int = 5) -> list[RecentStory]:
        response = await self._client.get(
            f"{settings.DF_DIRECTOR_BASE_URL}/api/director/stories/recent",
            params={"limit": max(1, min(int(limit), 10))},
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code in {401, 403}:
            response.raise_for_status()
        if response.status_code >= 400:
            return []
        raw = response.json()
        if not isinstance(raw, list):
            return []

        stories: list[RecentStory] = []
        for index, item in enumerate(raw[:10], start=1):
            if not isinstance(item, dict):
                continue
            path = str(item.get("continue_path") or "").strip()
            # Never accept an arbitrary upstream URL. Navigation stays inside the
            # authenticated desifaces Multi-Person workspace.
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
