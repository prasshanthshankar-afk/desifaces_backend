import asyncio

import pytest

from app.services.providers.image_provider import ImageBytesResult, ImageProviderRouter


class _Safety:
    def __init__(self, *, allow: bool, reason: str = ""):
        self.allow = allow
        self.reason = reason
        self.calls = 0

    async def validate_image(self, image_bytes, **kwargs):
        self.calls += 1
        assert image_bytes == b"generated-image"
        assert kwargs["fail_open"] is False
        return self.allow, self.reason


def _router_with_safety(safety):
    router = object.__new__(ImageProviderRouter)
    router._safety = safety
    return router


def _result():
    return ImageBytesResult(
        bytes=b"generated-image",
        content_type="image/png",
        provider="openai",
        meta={"mode": "t2i"},
    )


def test_generated_output_safety_allows_safe_image():
    safety = _Safety(allow=True)
    router = _router_with_safety(safety)

    result = asyncio.run(router._enforce_generated_output_safety(_result()))

    assert result.bytes == b"generated-image"
    assert safety.calls == 1


def test_generated_output_safety_blocks_unsafe_image():
    safety = _Safety(
        allow=False,
        reason="PROMPT_POLICY_BLOCKED: Image needs changes. Blocked category: violence.",
    )
    router = _router_with_safety(safety)

    with pytest.raises(RuntimeError, match="generated_image_policy_blocked"):
        asyncio.run(router._enforce_generated_output_safety(_result()))

    assert safety.calls == 1


class _UnavailableSafety:
    async def validate_image(self, image_bytes, **kwargs):
        raise RuntimeError("moderation unavailable")


def test_generated_output_safety_fails_closed_when_moderation_is_unavailable():
    router = _router_with_safety(_UnavailableSafety())

    with pytest.raises(RuntimeError, match="moderation unavailable"):
        asyncio.run(router._enforce_generated_output_safety(_result()))


class _PromptSafety:
    def __init__(self, allow: bool, reason: str = ""):
        self.allow = allow
        self.reason = reason

    def check_keywords(self, prompt):
        return self.allow, self.reason


def test_final_provider_prompt_blocks_firearm_before_generation():
    router = _router_with_safety(
        _PromptSafety(
            allow=False,
            reason="PROMPT_POLICY_BLOCKED: Prompt needs changes. Blocked category: weapons.",
        )
    )

    with pytest.raises(RuntimeError, match="generation_prompt_policy_blocked"):
        router._enforce_final_prompt_safety("woman with gun and arms")


def test_final_provider_prompt_allows_benign_arms_language():
    router = _router_with_safety(_PromptSafety(allow=True))

    router._enforce_final_prompt_safety(
        "woman with arms crossed, professional portrait, realistic lighting"
    )
