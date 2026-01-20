from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple
import json

import httpx
import asyncpg

from app.config import settings


@dataclass(frozen=True)
class SyncSummary:
    speech_voices_upserted: int = 0
    translator_langs_seen: int = 0
    locales_touched: int = 0
    locales_reconciled: int = 0
    defaults_set: int = 0


class CatalogSyncService:
    """
    Global catalog sync:
      1) Azure Speech voices -> tts_voices + ensure tts_locales(locale)
      2) Azure Translator languages -> mark translate_supported by base language
      3) Reconcile locales + set 1 default voice per locale via tts_voices.is_default

    NOTE: tts_locales does NOT have a default_voice column (by your schema).
          Defaults live in tts_voices.is_default.
    """

    PROVIDER = "azure"

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @staticmethod
    def _base_lang_from_locale(locale: str) -> str:
        return (locale.split("-", 1)[0] or "").strip().lower()

    # ----------------------------
    # 1) Speech voices inventory
    # ----------------------------
    async def sync_speech_voices(self) -> int:
        if not settings.AZURE_SPEECH_KEY:
            raise RuntimeError("missing_azure_speech_key")
        if not settings.AZURE_SPEECH_REGION:
            raise RuntimeError("missing_azure_speech_region")

        url = f"https://{settings.AZURE_SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/voices/list"
        headers = {"Ocp-Apim-Subscription-Key": settings.AZURE_SPEECH_KEY}

        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(url, headers=headers)
            if r.status_code >= 400:
                raise RuntimeError(f"speech_voices_list_failed status={r.status_code} body={r.text[:500]}")
            voices = r.json()
            if not isinstance(voices, list):
                raise RuntimeError("speech_voices_list_unexpected_shape")

        upserted = 0
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for v in voices:
                    if not isinstance(v, dict):
                        continue

                    voice_name = (v.get("ShortName") or v.get("Name") or "").strip()
                    locale = (v.get("Locale") or "").strip()
                    if not voice_name or not locale:
                        continue

                    gender = v.get("Gender")
                    voice_type = v.get("VoiceType")
                    style_list = v.get("StyleList") or []
                    supports_styles = bool(style_list)

                    base_lang = self._base_lang_from_locale(locale)
                    meta_json = json.dumps(v)

                    # Ensure locale exists (keep is_enabled true)
                    await conn.execute(
                        """
                        INSERT INTO public.tts_locales
                          (locale, translator_lang, tts_supported, translate_supported, is_enabled, display_name, native_name, meta_json)
                        VALUES
                          ($1, $2, true, false, true, NULL, NULL, '{}'::jsonb)
                        ON CONFLICT (locale) DO UPDATE
                          SET translator_lang = COALESCE(public.tts_locales.translator_lang, EXCLUDED.translator_lang),
                              tts_supported   = true,
                              is_enabled      = true
                        """,
                        locale,
                        base_lang,
                    )

                    # Upsert voice
                    await conn.execute(
                        """
                        INSERT INTO public.tts_voices
                          (provider, voice_name, locale, gender, voice_type, supports_styles, meta_json)
                        VALUES
                          ($1, $2, $3, $4, $5, $6, $7::jsonb)
                        ON CONFLICT (provider, voice_name) DO UPDATE
                          SET locale          = EXCLUDED.locale,
                              gender          = EXCLUDED.gender,
                              voice_type       = EXCLUDED.voice_type,
                              supports_styles  = EXCLUDED.supports_styles,
                              meta_json        = EXCLUDED.meta_json
                        """,
                        self.PROVIDER,
                        voice_name,
                        locale,
                        gender,
                        voice_type,
                        supports_styles,
                        meta_json,
                    )
                    upserted += 1

        return upserted

    # ----------------------------
    # 2) Translator languages inventory
    # ----------------------------
    async def sync_translator_languages(self) -> Tuple[int, int]:
        """
        Translator /languages does NOT require auth.
        We use it to mark translate_supported=true for any locale whose translator_lang (base lang)
        appears in Translator's supported set.
        """
        base = settings.AZURE_TRANSLATOR_ENDPOINT.strip() or "https://api.cognitive.microsofttranslator.com"
        url = f"{base.rstrip('/')}/languages"
        params = {"api-version": "3.0", "scope": "translation"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, params=params)
            if r.status_code >= 400:
                raise RuntimeError(f"translator_languages_failed status={r.status_code} body={r.text[:500]}")
            payload = r.json()

        translation = payload.get("translation") if isinstance(payload, dict) else None
        if not isinstance(translation, dict):
            raise RuntimeError("translator_languages_unexpected_shape")

        supported_base_langs: Dict[str, Dict[str, Any]] = {}
        for base_lang, info in translation.items():
            if isinstance(info, dict):
                supported_base_langs[str(base_lang).lower()] = info

        langs_seen = len(supported_base_langs)

        # keep only the parts we actually want to store
        compact = {
            k: {"name": v.get("name"), "nativeName": v.get("nativeName")}
            for k, v in supported_base_langs.items()
        }

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                res = await conn.execute(
                    """
                    UPDATE public.tts_locales l
                       SET translate_supported = true,
                           display_name = COALESCE(l.display_name, x.name),
                           native_name  = COALESCE(l.native_name,  x.native_name),
                           meta_json    = l.meta_json || jsonb_build_object(
                               'translator',
                               jsonb_build_object('name', x.name, 'nativeName', x.native_name)
                           )
                      FROM (
                        SELECT key AS translator_lang,
                               (value->>'name') AS name,
                               (value->>'nativeName') AS native_name
                          FROM jsonb_each($1::jsonb)
                      ) x
                     WHERE l.translator_lang = x.translator_lang
                    """,
                    json.dumps(compact),
                )
                touched = int(res.split()[-1]) if res.startswith("UPDATE") else 0

        return langs_seen, touched

    # ----------------------------
    # 3) Reconcile support + defaults
    # ----------------------------
    async def reconcile_locales(self) -> Tuple[int, int]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # tts_supported = exists voice rows for locale
                res1 = await conn.execute(
                    """
                    UPDATE public.tts_locales l
                       SET tts_supported = EXISTS (
                           SELECT 1
                             FROM public.tts_voices v
                            WHERE v.locale = l.locale
                              AND v.provider = $1
                       )
                    """,
                    self.PROVIDER,
                )
                reconciled = int(res1.split()[-1]) if res1.startswith("UPDATE") else 0

                # Reset all defaults for this provider
                await conn.execute(
                    "UPDATE public.tts_voices SET is_default=false WHERE provider=$1",
                    self.PROVIDER,
                )

                # Set exactly 1 default voice per locale using a single ranked query
                # Preference: Neural > name contains Neural > others; Female first; stable voice_name
                res2 = await conn.execute(
                    """
                    WITH ranked AS (
                      SELECT DISTINCT ON (locale) id, locale
                        FROM public.tts_voices
                       WHERE provider = $1
                       ORDER BY
                         locale,
                         CASE
                           WHEN voice_type ILIKE 'Neural' THEN 0
                           WHEN voice_name ILIKE '%Neural%' THEN 1
                           ELSE 2
                         END,
                         CASE WHEN gender ILIKE 'Female' THEN 0 ELSE 1 END,
                         voice_name ASC
                    )
                    UPDATE public.tts_voices v
                       SET is_default = true
                      FROM ranked r
                     WHERE v.id = r.id
                    """,
                    self.PROVIDER,
                )
                # res2 is "UPDATE N" where N == number of locales set
                defaults_set = int(res2.split()[-1]) if res2.startswith("UPDATE") else 0

        return reconciled, defaults_set

    # ----------------------------
    # One-shot wrapper
    # ----------------------------
    async def sync_all(self) -> SyncSummary:
        speech_upserted = await self.sync_speech_voices()
        langs_seen, locales_touched = await self.sync_translator_languages()
        locales_reconciled, defaults_set = await self.reconcile_locales()

        return SyncSummary(
            speech_voices_upserted=speech_upserted,
            translator_langs_seen=langs_seen,
            locales_touched=locales_touched,
            locales_reconciled=locales_reconciled,
            defaults_set=defaults_set,
        )