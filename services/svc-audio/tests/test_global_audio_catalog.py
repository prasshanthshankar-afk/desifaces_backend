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


def test_audio_country_catalog_is_db_and_capability_driven():
    pool = FakePool(
        [
            {
                "country_code": "IN",
                "display_name": "India",
                "locale_count": 13,
            }
        ]
    )

    result = asyncio.run(
        catalog.list_countries(pool=pool)
    )

    assert result == {
        "items": [
            {
                "country_code": "IN",
                "display_name": "India",
                "locale_count": 13,
            }
        ]
    }

    query, args = pool.connection.queries[0]

    assert args == ()
    assert "l.is_user_selectable = TRUE" in query
    assert "l.tts_supported = TRUE" in query
    assert "tts_voice_locale_capabilities" in query
    assert "tts_voice_model_capabilities" in query
    assert "tts_provider_models" in query
    assert "tts_providers" in query


def test_audio_target_languages_are_country_filtered():
    pool = FakePool(
        [
            {
                "locale": "ja-JP",
                "language_code": "ja",
                "country_code": "JP",
                "translator_lang": "ja",
                "display_name": "Japanese (Japan)",
                "native_name": "日本語",
                "tts_supported": True,
                "translate_supported": True,
                "is_user_selectable": True,
            }
        ]
    )

    result = asyncio.run(
        catalog.list_target_languages(
            country_code="jp",
            pool=pool,
        )
    )

    assert result["country_code"] == "JP"
    assert result["items"][0]["locale"] == "ja-JP"

    query, args = pool.connection.queries[0]

    assert args == ("JP",)
    assert "l.country_code = $1" in query
    assert "l.is_user_selectable = TRUE" in query
    assert "tts_voice_model_capabilities" in query


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
