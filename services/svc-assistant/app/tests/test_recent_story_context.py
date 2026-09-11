import asyncio

import httpx

from app.schemas import AssistantContextLocator
from app.story_context import StoryAwareContextResolver, project_safe_recent_stories


def test_recent_story_projection_keeps_operational_state_and_drops_identifiers_and_prose():
    safe = project_safe_recent_stories(
        [
            {
                "story_id": "11111111-1111-4111-8111-111111111111",
                "thread_id": "private-thread",
                "workflow_id": "22222222-2222-4222-8222-222222222222",
                "title": "Private family story title",
                "continue_path": "/app/multi-person?story_id=private",
                "state": "ready",
                "workflow_state": "active",
                "current_stage": "audio",
                "attention_state": "awaiting_review",
                "updated_at": "2026-09-10T20:00:00Z",
            },
            {
                "story_id": "33333333-3333-4333-8333-333333333333",
                "title": "Another private title",
                "state": "failed",
                "current_stage": "fusion",
                "updated_at": "2026-09-10T19:00:00Z",
            },
        ]
    )

    assert safe["count"] == 2
    assert safe["needs_review_count"] == 1
    assert safe["needs_attention_count"] == 1
    assert safe["items"][0] == {
        "alias": "Recent story 1",
        "state": "awaiting_review",
        "workflow_state": "active",
        "current_stage": "audio",
        "updated_at": "2026-09-10T20:00:00Z",
        "needs_review": True,
        "needs_attention": False,
    }
    wire = str(safe)
    for forbidden in (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "private-thread",
        "Private family story title",
        "Another private title",
        "continue_path",
        "story_id",
        "workflow_id",
    ):
        assert forbidden not in wire


def test_recent_story_fetch_forwards_auth_and_fails_closed_on_authorization():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(403, json={"detail": "forbidden"}, request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolver = StoryAwareContextResolver(client, None)  # pool is unused by this focused method
            try:
                await resolver._fetch_recent_stories(token="opaque-bearer")
            except httpx.HTTPStatusError as exc:
                assert exc.response.status_code == 403
            else:
                raise AssertionError("authorization failure must not degrade to anonymous context")

    asyncio.run(scenario())
    assert len(seen) == 1
    assert seen[0].headers.get("Authorization") == "Bearer opaque-bearer"
    assert seen[0].url.path.endswith("/api/director/stories/recent")
    assert seen[0].url.params.get("limit") == "10"


def test_recent_story_optional_source_degrades_on_upstream_failure():
    def handler(request: httpx.Request):
        return httpx.Response(503, json={"detail": "temporarily unavailable"}, request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolver = StoryAwareContextResolver(client, None)
            return await resolver._fetch_recent_stories(token="opaque-bearer")

    assert asyncio.run(scenario()) == {
        "items": [],
        "count": 0,
        "needs_review_count": 0,
        "needs_attention_count": 0,
    }


def test_story_discovery_locator_does_not_require_a_story_identifier():
    locator = AssistantContextLocator(surface="web", screen="story_discovery")
    assert locator.story_id is None
