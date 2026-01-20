from fastapi import APIRouter, Depends, HTTPException
import asyncpg

from app.db import get_pool
from app.api.deps import get_current_claims
from app.services.catalog_sync_service import CatalogSyncService

router = APIRouter(prefix="/api/audio/catalog", tags=["audio-catalog-admin"])

@router.post("/sync")
async def sync_catalog(
    _claims: dict = Depends(get_current_claims),
    pool: asyncpg.Pool = Depends(get_pool),
):
    svc = CatalogSyncService(pool)
    try:
        voices_upserted = await svc.sync_speech_voices()
        langs_seen, locales_touched = await svc.sync_translator_languages()
        reconciled, defaults_set = await svc.reconcile_locales()

        return {
            "speech_voices_upserted": voices_upserted,
            "translator_langs_seen": langs_seen,
            "locales_touched": locales_touched,
            "locales_reconciled": reconciled,
            "defaults_set": defaults_set,
        }
    except Exception as e:
        # Always JSON
        raise HTTPException(status_code=400, detail=str(e))