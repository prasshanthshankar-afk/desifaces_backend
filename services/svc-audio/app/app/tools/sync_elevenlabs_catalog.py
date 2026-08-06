from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

import asyncpg
import httpx


API = os.getenv("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io").rstrip("/")


def _db_url() -> str:
    return os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )


async def _fetch_catalog(key: str):
    headers = {"xi-api-key": key}

    async with httpx.AsyncClient(
        base_url=API, headers=headers, timeout=30.0
    ) as client:
        # Models are versioned DB masterdata. A restricted production
        # key only needs Voices Read + Text-to-Speech Access.
        models = []

        voices = []
        token = None

        while True:
            params = {
                "page_size": 100,
                "include_total_count": "false",
            }
            if token:
                params["next_page_token"] = token

            r = await client.get("/v2/voices", params=params)
            r.raise_for_status()
            page = r.json()

            voices.extend(page.get("voices") or [])

            if not page.get("has_more"):
                break

            token = page.get("next_page_token")
            if not token:
                raise RuntimeError("elevenlabs_pagination_token_missing")

    return models, voices


def _eligible_models(voice, configured):
    preferred = set(voice.get("high_quality_base_model_ids") or [])
    if not preferred:
        return configured

    matched = [
        m for m in configured
        if m["provider_model_id"] in preferred
    ]
    return matched or configured


def _voice_meta(item):
    labels = item.get("labels") or {}
    return {
        "provider_voice_id": item.get("voice_id"),
        "display_name": item.get("name"),
        "category": item.get("category"),
        "description": item.get("description"),
        "preview_url": item.get("preview_url"),
        "labels": labels,
        "high_quality_base_model_ids":
            item.get("high_quality_base_model_ids") or [],
        "verified_languages":
            item.get("verified_languages") or [],
        "catalog_source": "elevenlabs_api",
    }


async def _sync(models, voices):
    live_models = {
        m["model_id"]: m
        for m in models
        if m.get("model_id")
        and m.get("can_do_text_to_speech")
    }

    pool = await asyncpg.create_pool(
        _db_url(), min_size=1, max_size=2
    )

    try:
        async with pool.acquire() as conn:
            configured = await conn.fetch(
                """
                SELECT model_code,provider_model_id
                FROM public.tts_provider_models
                WHERE provider_code='elevenlabs'
                  AND is_enabled=true
                """
            )

            configured = [dict(r) for r in configured]

            if not configured:
                raise RuntimeError(
                    "no_live_configured_elevenlabs_models"
                )

            locales_by_model = {}
            for m in configured:
                rows = await conn.fetch(
                    """
                    SELECT locale,
                           COALESCE(accent_code,'') AS accent_code
                    FROM public.tts_model_locale_capabilities
                    WHERE provider_code='elevenlabs'
                      AND model_code=$1
                      AND is_enabled=true
                      AND is_approved=true
                    """,
                    m["model_code"],
                )
                locales_by_model[m["model_code"]] = [
                    dict(r) for r in rows
                ]

            return await _write_catalog(
                conn,
                live_models,
                configured,
                locales_by_model,
                voices,
            )

    finally:
        await pool.close()


async def _write_catalog(
    conn, live_models, configured,
    locales_by_model, voices,
):
    seen = 0

    async with conn.transaction():
        # Fail closed for stale provider voices.
        await conn.execute(
            """
            UPDATE public.tts_voice_model_capabilities
            SET is_enabled=false,updated_at=now()
            WHERE provider_code='elevenlabs'
            """
        )

        await conn.execute(
            """
            UPDATE public.tts_voice_locale_capabilities vl
            SET is_enabled=false,updated_at=now()
            FROM public.tts_voices v
            WHERE vl.voice_id=v.id
              AND v.provider='elevenlabs'
            """
        )

        for item in voices:
            external_id = str(
                item.get("voice_id") or ""
            ).strip()
            if not external_id:
                continue

            meta = _voice_meta(item)
            labels = item.get("labels") or {}

            gender = str(
                labels.get("gender") or ""
            ).lower()
            if gender not in {
                "male", "female", "neutral"
            }:
                gender = None

            row = await conn.fetchrow(
                """
                UPDATE public.tts_voices
                SET gender=$2,
                    voice_type=$3,
                    meta_json=COALESCE(meta_json,'{}'::jsonb)
                              || $4::jsonb,
                    updated_at=now()
                WHERE provider='elevenlabs'
                  AND voice_name=$1
                RETURNING id
                """,
                external_id,
                gender,
                item.get("category"),
                json.dumps(meta),
            )

            if not row:
                row = await conn.fetchrow(
                    """
                    INSERT INTO public.tts_voices(
                      provider,voice_name,locale,gender,
                      voice_type,is_default,
                      supports_styles,meta_json
                    )
                    VALUES(
                      'elevenlabs',$1,NULL,$2,$3,
                      false,false,$4::jsonb
                    )
                    RETURNING id
                    """,
                    external_id,
                    gender,
                    item.get("category"),
                    json.dumps(meta),
                )

            voice_id = row["id"]
            eligible = _eligible_models(
                item, configured
            )

            locale_map = {}

            for model in eligible:
                provider_model = (
                    model["provider_model_id"]
                )
                live = live_models.get(provider_model) or {}

                await conn.execute(
                    """
                    INSERT INTO public.tts_voice_model_capabilities(
                      provider_code,voice_id,model_code,
                      is_enabled,is_approved,
                      supports_styles,
                      source,source_version,
                      discovered_at,last_seen_at,meta_json
                    )
                    VALUES(
                      'elevenlabs',$1,$2,true,true,$3,
                      'provider_catalog','live',
                      now(),now(),$4::jsonb
                    )
                    ON CONFLICT(
                      provider_code,voice_id,model_code
                    )
                    DO UPDATE SET
                      is_enabled=true,
                      is_approved=true,
                      supports_styles=EXCLUDED.supports_styles,
                      source='provider_catalog',
                      source_version='live',
                      last_seen_at=now(),
                      meta_json=EXCLUDED.meta_json,
                      updated_at=now()
                    """,
                    voice_id,
                    model["model_code"],
                    bool(live.get("can_use_style")),
                    json.dumps(meta),
                )

                for loc in locales_by_model[
                    model["model_code"]
                ]:
                    locale_map[
                        (
                            loc["locale"],
                            loc["accent_code"],
                        )
                    ] = loc

            verified = {
                str(x.get("locale") or "").lower()
                for x in (
                    item.get("verified_languages")
                    or []
                )
                if x.get("locale")
            }

            for locale, accent in locale_map:
                recommended = (
                    locale.lower() in verified
                )

                updated = await conn.execute(
                    """
                    UPDATE public.tts_voice_locale_capabilities
                    SET is_enabled=true,
                        is_approved=true,
                        is_native_fit=$4,
                        is_recommended=$4,
                        quality_score=$5,
                        source='provider_catalog',
                        source_version='live',
                        last_seen_at=now(),
                        meta_json=$6::jsonb,
                        updated_at=now()
                    WHERE voice_id=$1
                      AND locale=$2
                      AND accent_code=$3
                    """,
                    voice_id,
                    locale,
                    accent,
                    recommended,
                    0.95 if recommended else 0.80,
                    json.dumps(meta),
                )

                if updated == "UPDATE 0":
                    await conn.execute(
                        """
                        INSERT INTO public.tts_voice_locale_capabilities(
                          voice_id,locale,accent_code,
                          is_native_fit,is_recommended,
                          is_enabled,is_approved,
                          quality_score,selection_priority,
                          source,source_version,
                          last_seen_at,meta_json
                        )
                        VALUES(
                          $1,$2,$3,$4,$4,
                          true,true,$5,0,
                          'provider_catalog','live',
                          now(),$6::jsonb
                        )
                        """,
                        voice_id,
                        locale,
                        accent,
                        recommended,
                        0.95 if recommended else 0.80,
                        json.dumps(meta),
                    )

            seen += 1

        await conn.execute(
            """
            UPDATE public.masterdata_revision
            SET revision=revision+1
            WHERE domain='tts'
            """
        )

    return seen


async def main():
    key = os.getenv(
        "ELEVENLABS_API_KEY", ""
    ).strip()

    if not key:
        raise RuntimeError(
            "ELEVENLABS_API_KEY is required"
        )

    models, voices = await _fetch_catalog(key)
    seen = await _sync(models, voices)

    print("elevenlabs_voices =", seen)
    print("ELEVENLABS_CATALOG_SYNC = PASS")


if __name__ == "__main__":
    asyncio.run(main())
