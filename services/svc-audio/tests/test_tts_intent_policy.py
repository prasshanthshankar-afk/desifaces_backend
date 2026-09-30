from app.services.tts_intent_policy import requires_provider_native_style


def test_empty_style_does_not_require_provider_native_style():
    assert requires_provider_native_style(None) is False
    assert requires_provider_native_style("") is False


def test_conversational_and_narration_are_product_intents():
    assert requires_provider_native_style("Conversational") is False
    assert requires_provider_native_style(" conversational ") is False
    assert requires_provider_native_style("Narration") is False
    assert requires_provider_native_style("narration") is False


def test_character_still_requires_provider_native_style():
    assert requires_provider_native_style("Character") is True


def test_unknown_explicit_style_fails_safe_as_native_requirement():
    assert requires_provider_native_style("cheerful") is True
