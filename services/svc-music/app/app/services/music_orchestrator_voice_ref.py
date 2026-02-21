from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.config import settings
from app.db import get_pool
from app.services.azure_storage_service import AzureStorageService
from app.services.music_orchestrator_common import _as_dict

JsonDict = Dict[str, Any]


def _fallback_input_container() -> str:
    return (getattr(settings, "MUSIC_INPUT_CONTAINER", None) or "music-input").strip() or "music-input"


def _fallback_output_container() -> str:
    return (getattr(settings, "MUSIC_OUTPUT_CONTAINER", None) or "music-output").strip() or "music-output"


def _extract_container_and_path_from_meta(meta_json: Any) -> Tuple[Optional[str], Optional[str]]:
    m = _as_dict(meta_json)
    c = m.get("container") or m.get("blob_container")
    p = m.get("storage_path") or m.get("path")
    c = str(c).strip() if isinstance(c, str) and c.strip() else None
    p = str(p).strip() if isinstance(p, str) and p.strip() else None
    return c, p


async def _update_media_asset_refs_best_effort(
    *,
    pool,
    asset_id: UUID,
    new_storage_ref: str,
    container: Optional[str],
    storage_path: Optional[str],
) -> None:
    existing_meta: Any = {}
    try:
        r = await pool.fetchrow("select meta_json from public.media_assets where id=$1", asset_id)
        if r and r.get("meta_json") is not None:
            existing_meta = r["meta_json"]
    except Exception:
        existing_meta = {}

    meta_obj = _as_dict(existing_meta)

    if container and not meta_obj.get("container"):
        meta_obj["container"] = container
    if storage_path and not meta_obj.get("storage_path"):
        meta_obj["storage_path"] = storage_path

    try:
        await pool.execute(
            """
            update public.media_assets
            set storage_ref=$2, meta_json=$3, updated_at=now()
            where id=$1
            """,
            asset_id,
            new_storage_ref,
            meta_obj if meta_obj else {},
        )
    except Exception:
        await pool.execute(
            """
            update public.media_assets
            set storage_ref=$2, updated_at=now()
            where id=$1
            """,
            asset_id,
            new_storage_ref,
        )


def _resolve_container_and_path(
    *, storage_ref: str | None, meta_json: Any
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    meta_container, meta_path = _extract_container_and_path_from_meta(meta_json)

    url_container, url_path = (None, None)
    if storage_ref:
        try:
            url_container, url_path = AzureStorageService.parse_blob_url(storage_ref)
        except Exception:
            url_container, url_path = (None, None)

    blob_path = meta_path or url_path
    container = meta_container or url_container
    return container, blob_path, meta_container, url_container


async def resolve_voice_ref_sas_url(*, project_id: UUID, user_id: UUID, voice_ref_asset_id: UUID | None) -> Optional[str]:
    pool = await get_pool()

    if not voice_ref_asset_id:
        rowp = await pool.fetchrow(
            "select voice_ref_asset_id from public.music_projects where id=$1 and user_id=$2",
            project_id,
            user_id,
        )
        if not rowp or not rowp["voice_ref_asset_id"]:
            return None
        voice_ref_asset_id = UUID(str(rowp["voice_ref_asset_id"]))

    asset = await pool.fetchrow(
        """
        select id, storage_ref, meta_json
        from public.media_assets
        where id=$1 and user_id=$2
        limit 1
        """,
        voice_ref_asset_id,
        user_id,
    )
    if not asset:
        return None

    storage_ref = str(asset.get("storage_ref") or "") or None
    meta_json = asset.get("meta_json")

    _, blob_path, meta_container, url_container = _resolve_container_and_path(
        storage_ref=storage_ref,
        meta_json=meta_json,
    )
    if not blob_path:
        return storage_ref or None

    candidates: List[str] = []
    for c in (meta_container, url_container, _fallback_input_container(), _fallback_output_container()):
        if c and c not in candidates:
            candidates.append(c)

    for c in candidates:
        try:
            storage = AzureStorageService(container=c)
            refreshed = storage.sas_url_for(blob_path)
            await _update_media_asset_refs_best_effort(
                pool=pool,
                asset_id=voice_ref_asset_id,
                new_storage_ref=refreshed,
                container=c,
                storage_path=blob_path,
            )
            return refreshed
        except Exception:
            continue

    return storage_ref or None


async def resolve_url_from_refs(*, user_id: UUID, media_asset_id: UUID | None, artifact_id: UUID | None) -> Optional[str]:
    pool = await get_pool()

    if media_asset_id:
        try:
            r = await pool.fetchrow(
                "select storage_ref, meta_json from public.media_assets where id=$1 and user_id=$2",
                media_asset_id,
                user_id,
            )
            if r:
                storage_ref = str(r.get("storage_ref") or "") or None
                container, blob_path, meta_container, url_container = _resolve_container_and_path(
                    storage_ref=storage_ref,
                    meta_json=r.get("meta_json"),
                )

                candidates: List[str] = []
                for c in (meta_container, url_container, container, _fallback_output_container(), _fallback_input_container()):
                    if c and c not in candidates:
                        candidates.append(c)

                if blob_path:
                    for c in candidates:
                        try:
                            storage = AzureStorageService(container=c)
                            refreshed = storage.sas_url_for(blob_path)
                            try:
                                await _update_media_asset_refs_best_effort(
                                    pool=pool,
                                    asset_id=UUID(str(media_asset_id)),
                                    new_storage_ref=refreshed,
                                    container=c,
                                    storage_path=blob_path,
                                )
                            except Exception:
                                pass
                            return refreshed
                        except Exception:
                            continue

                if storage_ref and storage_ref.startswith("http"):
                    return storage_ref
        except Exception:
            pass

    if artifact_id:
        try:
            r = await pool.fetchrow("select storage_path from public.music_artifacts where id=$1", artifact_id)
            sp = str(r["storage_path"]).strip() if r and r.get("storage_path") else None
            if sp:
                return AzureStorageService(container=_fallback_output_container()).sas_url_for(sp)
        except Exception:
            pass

    return None