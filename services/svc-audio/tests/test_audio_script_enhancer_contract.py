import asyncio

import prompt_enhancer as enhancer
from prompt_enhancer import PromptEnhanceRequest


def audio_request() -> PromptEnhanceRequest:
    return PromptEnhanceRequest(
        studio="audio",
        mode="tts",
        user_input="This is testing phase of desifaces.ai",
        locked_fields={
            "target_locale": "ar-AE",
            "voice_style": "confident",
            "pacing": "natural",
        },
        context={},
        locale="en",
        max_alternatives=3,
    )


def all_spoken_text(response):
    return [
        response.enhanced_input,
        *[item.text for item in response.alternatives],
    ]


def assert_no_control_directives(text: str):
    lowered = text.lower()

    forbidden = (
        "write for ",
        "delivery style:",
        "pacing:",
        "target locale:",
        "voice style:",
        "tts direction:",
    )

    for marker in forbidden:
        assert marker not in lowered


def test_audio_fallback_contains_spoken_words_only():
    response = enhancer._fallback_response(
        audio_request()
    )

    assert response.fallback_used is True

    for text in all_spoken_text(response):
        assert_no_control_directives(text)

    labels = {
        item.label
        for item in response.alternatives
    }

    assert "Shorter" in labels
    assert "Premium" in labels


def test_audio_system_prompt_defines_script_enrichment():
    prompt = enhancer._llm_system_prompt(
        audio_request()
    )

    assert "actual script that a human voice will speak" in prompt
    assert "spoken English words only" in prompt
    assert "downstream translation metadata" in prompt
    assert 'alternative labeled "Shorter"' in prompt
    assert 'alternative labeled "Premium"' in prompt


def test_audio_llm_control_directive_leak_fails_safe(monkeypatch):
    async def fake_call(_req):
        return {
            "enhanced_input": (
                "This is testing phase of desifaces.ai. "
                "Write for ar-AE. Delivery style: confident."
            ),
            "alternatives": [
                {
                    "label": "Shorter",
                    "text": "desifaces.ai is in testing.",
                },
                {
                    "label": "Premium",
                    "text": "Pacing: natural. desifaces.ai is in testing.",
                },
            ],
            "tips": [],
            "why_this_is_better": "test",
            "structured": {},
        }

    monkeypatch.setattr(
        enhancer,
        "_call_openai_json",
        fake_call,
    )

    response = asyncio.run(
        enhancer.enhance_prompt(
            audio_request()
        )
    )

    assert response.fallback_used is True

    for text in all_spoken_text(response):
        assert_no_control_directives(text)


def test_valid_richer_audio_script_is_accepted(monkeypatch):
    async def fake_call(_req):
        return {
            "enhanced_input": (
                "desifaces.ai is currently in its testing phase, "
                "where the experience is being carefully reviewed "
                "and refined before reaching a wider audience."
            ),
            "alternatives": [
                {
                    "label": "Shorter",
                    "text": (
                        "desifaces.ai is currently in testing "
                        "as the experience is refined."
                    ),
                },
                {
                    "label": "Premium",
                    "text": (
                        "desifaces.ai is currently in an important "
                        "testing phase, with careful attention being "
                        "given to creating a polished and dependable "
                        "experience for a wider audience."
                    ),
                },
            ],
            "tips": [],
            "why_this_is_better": (
                "The spoken script is richer and more natural."
            ),
            "structured": {
                "source_language": "en",
                "target_locale": "ar-AE",
            },
        }

    monkeypatch.setattr(
        enhancer,
        "_call_openai_json",
        fake_call,
    )

    response = asyncio.run(
        enhancer.enhance_prompt(
            audio_request()
        )
    )

    assert response.fallback_used is False
    assert response.source == "llm"
    assert len(response.enhanced_input) > len(
        response.original_input
    )

    for text in all_spoken_text(response):
        assert_no_control_directives(text)
