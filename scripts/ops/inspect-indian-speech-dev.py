"""Read-only DEV speech routing preflight; never synthesizes or changes billing."""
import asyncio
import hashlib
import json
import os
from pathlib import Path

import asyncpg


async def main():
    from app.services.tts_service import TTSService
    from app.services.tts_resolution_planner import TTSResolutionPlanRequest
    from app.repos.tts_catalog_repo import TTSCatalogRepository

    for key in ("SARVAM_API_KEY", "OPENAI_API_KEY", "AZURE_TRANSLATOR_KEY"):
        print(key + "=" + ("PRESENT" if os.getenv(key, "").strip() else "MISSING"))
    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2,
        server_settings={"default_transaction_read_only": "on", "statement_timeout": "15000"})
    try:
        assert await pool.fetchval("SHOW default_transaction_read_only") == "on"
        catalog = TTSCatalogRepository(pool)
        before = await catalog.get_masterdata_revision()
        service = TTSService(pool)
        for locale in ("hi-IN", "ta-IN", "te-IN", "bn-IN", "gu-IN", "kn-IN", "ml-IN", "mr-IN", "pa-IN", "od-IN", "en-US"):
            for gender in ("male", "female"):
                try:
                    plan = await service.resolution_planner.resolve(TTSResolutionPlanRequest(
                        requested_locale=locale, text_length=100, requested_gender=gender,
                        country_code="US"))
                    print("ROUTE=" + json.dumps([locale, gender, plan.provider_code,
                        plan.provider_model_id, plan.voice_name, plan.voice_gender]))
                except Exception as error:
                    print("ROUTE_ERROR=" + json.dumps([locale, gender, type(error).__name__, str(error)[:160]]))
        models = await pool.fetch("SELECT provider_code, model_code, provider_model_id, is_enabled, routing_enabled FROM public.tts_provider_models WHERE provider_code='sarvam'")
        print("SARVAM_MODELS=" + json.dumps([dict(row) for row in models], default=str))
        providers = await pool.fetch("SELECT provider_code, is_enabled, routing_enabled FROM public.tts_providers WHERE provider_code='sarvam'")
        print("SARVAM_PROVIDER=" + json.dumps([dict(row) for row in providers]))
        for table in ("v3_stories", "v3_story_stages", "media_assets", "studio_jobs", "v3_artifacts"):
            columns = await pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=$1 ORDER BY ordinal_position", table)
            print("COLUMNS=" + json.dumps([table, [row["column_name"] for row in columns]]))
        assert await catalog.get_masterdata_revision() == before
        print("READ_ONLY_SPEECH_PREFLIGHT=PASS")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
