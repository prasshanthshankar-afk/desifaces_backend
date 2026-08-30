from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.recent_stories import RecentStory, is_recent_story_query
from app.schemas import AssistantAction, AssistantChatIn, AssistantContextLocator
from app.security import AssistantAuthContext
from app.service import AssistantService


class FakeSessions:
    def __init__(self):
        self.rows = []

    async def history(self, **kwargs):
        return []

    async def append(self, **kwargs):
        self.rows.append(kwargs)


class FakeStories:
    async def recent(self, *, token: str, limit: int = 5):
        assert token == "token"
        return [RecentStory(
            label="Recent multi-person story 1",
            state="ready",
            updated_at="2026-08-30T20:00:00Z",
            continue_path="/app/multi-person?story=11111111-1111-4111-8111-111111111111",
        )]


class MustNotCall:
    async def answer(self, **kwargs):
        raise AssertionError("LLM must not be called for recent-story lookup")

    async def retrieve(self, *args, **kwargs):
        raise AssertionError("RAG must not be called for recent-story lookup")

    async def resolve(self, *args, **kwargs):
        raise AssertionError("generic context resolver must not be called for recent-story lookup")


def test_recent_story_intent_detection():
    assert is_recent_story_query("What was the last multi-person story I was working on?")
    assert is_recent_story_query("Can I continue my most recent story?")
    assert not is_recent_story_query("How many credits do I have?")


def test_navigation_action_is_internal_only():
    action = AssistantAction(type="continue_story", label="Continue story", href="/app/multi-person?story=x")
    assert action.href.startswith("/app/")
    with pytest.raises(ValidationError):
        AssistantAction(type="continue_story", label="Continue story", href="https://example.com")


@pytest.mark.asyncio
async def test_recent_story_answer_bypasses_llm_and_hides_story_id():
    sessions = FakeSessions()
    service = AssistantService(
        sessions=sessions,
        context_resolver=MustNotCall(),
        recent_stories=FakeStories(),
        retriever=MustNotCall(),
        llm=MustNotCall(),
    )
    auth = AssistantAuthContext(user_id=uuid4(), account_id=uuid4(), token="token")
    body = AssistantChatIn(
        message="Give me the last Story ID for the multi-person story I was working on",
        context=AssistantContextLocator(surface="web", screen="multi_person_story"),
    )
    result = await service.chat(body, auth)
    assert "Continue story" in result.answer
    assert "11111111-1111-4111-8111-111111111111" not in result.answer
    assert result.context["context_scope"] == "director_recent_story"
    assert result.suggested_actions[0].href == "/app/multi-person?story=11111111-1111-4111-8111-111111111111"
    assert result.suggested_actions[0].requires_confirmation is False
    assert sessions.rows
