import asyncio

import pytest
from fastapi import HTTPException

from app.api.routes import catalog


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        return self.rows


class AcquireContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, rows):
        self.connection = FakeConnection(rows)

    def acquire(self):
        return AcquireContext(self.connection)


def _assert_executable_capability_graph(query: str) -> None:
    assert "tts_voice_locale_capabilities" in query
    assert "tts_voice_model_capabilities" in query
    assert "tts_provider_models" in query
    assert "tts_providers" in query
    assert "tts_model_locale_capabilities" in query
    assert "tts_model_language_capabilities" in query
    assert "m.routing_enabled = TRUE" in query
    assert "p.routing_enabled = TRUE" in query
    assert "vm.is_approved = TRUE" in query
    assert "mlc.is_approved = TRUE" in query
    assert "mlng.is_approved = TRUE" in query


def test_audio_locale_catalog_requires_executable_capability_graph():
    pool = FakePool(
        [
            {
                "locale": "ur-PK",
                "translator_lang": "ur",
                "tts_supported": True,
                "translate_supported": True,
                "is_enabled": True,
                "display_name": "Urdu",
                "native_name": "اردو",
                "default_voice": "ur-PK-UzmaNeural",
            }
        ]
    )

    result = asyncio.run(catalog.list_locales(pool=pool))

    assert result["items"][0]["locale"] == "ur-PK"
    assert result["items"][0]["default_voice"] == "ur-PK-UzmaNeural"

    query, args = pool.connection.queries[0]
    assert args == ()
    _assert_executable_capability_graph(query)


def test_audio_country_catalog_is_db_and_capability_driven():
    pool = FakePool(
        [
            {
                "country_code": "PK",
                "display_name": "Pakistan",
                "locale_count": 1,
            }
        ]
    )

    result = asyncio.run(catalog.list_countries(pool=pool))

    assert result == {
        "items": [
            {
                "country_code": "PK",
                "display_name": "Pakistan",
                "locale_count": 1,
            }
        ]
    }

    query, args = pool.connection.queries[0]

    assert args == ()
    assert "l.is_user_selectable = TRUE" in query
    assert "l.tts_supported = TRUE" in query
    _assert_executable_capability_graph(query)


def test_audio_target_languages_are_country_filtered_and_executable():
    pool = FakePool(
        [
            {
                "locale": "ur-PK",
                "language_code": "ur",
                "country_code": "PK",
                "translator_lang": "ur",
                "display_name": "Urdu",
                "native_name": "اردو",
                "tts_supported": True,
                "translate_supported": True,
                "is_user_selectable": True,
            }
        ]
    )

    result = asyncio.run(
        catalog.list_target_languages(
            country_code="pk",
            pool=pool,
        )
    )

    assert result["country_code"] == "PK"
    assert result["items"][0]["locale"] == "ur-PK"

    query, args = pool.connection.queries[0]

    assert args == ("PK",)
    assert "l.country_code = $1" in query
    assert "l.is_user_selectable = TRUE" in query
    _assert_executable_capability_graph(query)


def test_audio_voice_catalog_requires_same_model_locale_or_language_support():
    pool = FakePool(
        [
            {
                "voice_name": "ur-PK-AsadNeural",
                "voice_id": "ur-PK-AsadNeural",
                "display_name": "Asad",
                "locale": "ur-PK",
                "gender": "Male",
                "voice_type": "Neural",
                "is_default": False,
                "supports_styles": False,
                "meta_json": {},
            }
        ]
    )

    result = asyncio.run(
        catalog.list_voices(
            locale="ur-PK",
            pool=pool,
        )
    )

    assert result["items"][0]["voice_name"] == "ur-PK-AsadNeural"

    query, args = pool.connection.queries[0]
    assert args == ("ur-PK",)
    assert "JOIN public.tts_locales l" in query
    _assert_executable_capability_graph(query)


def test_invalid_audio_country_code_rejected():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            catalog.list_target_languages(
                country_code="JPN",
                pool=FakePool([]),
            )
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "invalid_country_code"
