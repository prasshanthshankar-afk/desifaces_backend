from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import UUID, uuid4

import asyncpg

from app.db import get_pool


class MediaAssetsRepo:
    @staticmethod
    def _meta_to_jsonb(meta_json: dict[str, Any] | list[Any] | str | None) -> Any:
        """
        DB column meta_json is JSONB.
        Callers may pass dict/list/str. We normalize to a JSON-serializable Python object.

        Rules:
          - dict/list -> keep as-is
          - str that looks like JSON -> json.loads
          - other str -> wrap as {"value": "<string>"}
          - None -> {}
        """
        if meta_json is None:
            return {}

        if isinstance(meta_json, (dict, list)):
            return meta_json

        if isinstance(meta_json, str):
            s = meta_json.strip()
            if not s:
                return {}
            if s.startswith("{") or s.startswith("["):
                try:
                    obj = json.loads(s)
                    # JSONB supports primitives too, but we prefer dict/list for predictable callers
                    if isinstance(obj, (dict, list)):
                        return obj
                    return {"value": obj}
                except Exception:
                    return {"value": s}
            return {"value": s}

        # last resort: coerce to string
        try:
            return {"value": str(meta_json)}
        except Exception:
            return {}

    @staticmethod
    def _as_uuid(v: Any, fallback: UUID) -> UUID:
        if isinstance(v, UUID):
            return v
        try:
            return UUID(str(v))
        except Exception:
            return fallback

    @staticmethod
    def _clamp_int(v: Any, *, min_value: int = 0) -> int:
        try:
            n = int(v)
        except Exception:
            n = min_value
        return n if n >= min_value else min_value

    @staticmethod
    def _merge_meta(existing_meta: Any, patch: dict[str, Any]) -> dict[str, Any]:
        """
        Merge patch into existing_meta when both are dicts.
        If existing_meta isn't a dict, we replace it with patch.
        NOTE: patch wins on key conflicts.
        """
        if isinstance(existing_meta, dict):
            merged = dict(existing_meta)
            merged.update(patch)
            return merged
        return dict(patch)

    @staticmethod
    def _fill_missing_meta(existing_meta: Any, patch: dict[str, Any]) -> dict[str, Any]:
        """
        Like merge, but only fills keys that are missing or blankish in existing_meta.
        """
        if not isinstance(existing_meta, dict):
            return dict(patch)

        merged = dict(existing_meta)
        for k, v in (patch or {}).items():
            if k not in merged or merged.get(k) in (None, ""):
                merged[k] = v
        return merged

    @staticmethod
    def _parse_azure_blob_url(storage_ref: str) -> tuple[Optional[str], Optional[str]]:
        """
        Parse:
          https://{acct}.blob.core.windows.net/{container}/{blob_path}?{sas}

        Returns: (container, blob_path) or (None, None) if not an Azure blob URL.
        """
        try:
            if not storage_ref:
                return (None, None)
            u = urlparse(str(storage_ref))
            host = (u.netloc or "").lower()
            if "blob.core.windows.net" not in host:
                return (None, None)

            path = (u.path or "").lstrip("/")  # "{container}/{blob_path}"
            if not path:
                return (None, None)
            parts = path.split("/", 1)
            if len(parts) != 2:
                return (None, None)

            container = parts[0].strip() or None
            blob_path = parts[1].lstrip("/") or None
            return (container, blob_path)
        except Exception:
            return (None, None)

    @classmethod
    def _ensure_storage_identity_meta(cls, storage_ref: str, meta_val: Any) -> Any:
        """
        Ensure meta_json contains:
          - container
          - storage_path
        when storage_ref is an Azure blob URL.

        Does NOT overwrite existing keys.
        """
        if not isinstance(meta_val, dict):
            return meta_val

        c, p = cls._parse_azure_blob_url(storage_ref)
        patch: dict[str, Any] = {}
        if c:
            patch["container"] = c
        if p:
            patch["storage_path"] = p

        if not patch:
            return meta_val

        return cls._fill_missing_meta(meta_val, patch)

    async def get_by_id(self, *, asset_id: UUID, user_id: UUID) -> Optional[dict]:
        pool = await get_pool()
        row = await pool.fetchrow(
            """
            select id, user_id, kind, storage_ref, content_type, bytes, sha256,
                   width, height, duration_ms, meta_json, created_at, updated_at
            from public.media_assets
            where id=$1 and user_id=$2
            limit 1
            """,
            asset_id,
            user_id,
        )
        return dict(row) if row else None

    async def get_by_user_sha256(
        self,
        *,
        user_id: UUID,
        sha256_hex: str,
    ) -> Optional[dict]:
        pool = await get_pool()
        row = await pool.fetchrow(
            """
            select id, user_id, kind, storage_ref, content_type, bytes, sha256, meta_json, created_at
            from public.media_assets
            where user_id=$1 and sha256=$2
            limit 1
            """,
            user_id,
            sha256_hex,
        )
        return dict(row) if row else None

    async def update_storage_ref(
        self,
        *,
        asset_id: UUID,
        storage_ref: str,
        meta_json: dict[str, Any] | list[Any] | str | None = None,
        merge_meta: bool = True,
    ) -> None:
        """
        Update storage_ref and optionally meta_json.

        - meta_json is JSONB.
        - If merge_meta=True and meta_json is a dict:
            fetch existing meta_json and overlay keys (patch wins).
        - If meta_json is None:
            we *still* attempt to fill {container, storage_path} from storage_ref
            when storage_ref is an Azure blob URL, without overwriting existing keys.
        """
        pool = await get_pool()

        if meta_json is None:
            # Best-effort: preserve canonical blob identity for SAS refresh later.
            c, p = self._parse_azure_blob_url(storage_ref)
            if not (c or p) or not merge_meta:
                await pool.execute(
                    """
                    update public.media_assets
                    set storage_ref=$2, updated_at=now()
                    where id=$1
                    """,
                    asset_id,
                    storage_ref,
                )
                return

            patch: dict[str, Any] = {}
            if c:
                patch["container"] = c
            if p:
                patch["storage_path"] = p

            try:
                row = await pool.fetchrow("select meta_json from public.media_assets where id=$1", asset_id)
                existing_meta = row["meta_json"] if row and row.get("meta_json") is not None else {}
            except Exception:
                existing_meta = {}

            merged = self._fill_missing_meta(existing_meta, patch)

            await pool.execute(
                """
                update public.media_assets
                set storage_ref=$2, meta_json=$3, updated_at=now()
                where id=$1
                """,
                asset_id,
                storage_ref,
                merged,
            )
            return

        # Normalize incoming meta to JSONB-ish python object
        meta_val = self._meta_to_jsonb(meta_json)
        meta_val = self._ensure_storage_identity_meta(storage_ref, meta_val)

        if merge_meta and isinstance(meta_val, dict):
            # Fetch existing meta_json for merge (patch wins)
            try:
                row = await pool.fetchrow("select meta_json from public.media_assets where id=$1", asset_id)
                existing_meta = row["meta_json"] if row and row.get("meta_json") is not None else {}
            except Exception:
                existing_meta = {}

            meta_val = self._merge_meta(existing_meta, meta_val)

        await pool.execute(
            """
            update public.media_assets
            set storage_ref=$2, meta_json=$3, updated_at=now()
            where id=$1
            """,
            asset_id,
            storage_ref,
            meta_val,
        )

    async def create_asset(
        self,
        *,
        user_id: UUID,
        kind: str,
        storage_ref: str,
        content_type: str,
        bytes_len: int,
        sha256_hex: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_ms: int | None = None,
        meta_json: dict[str, Any] | list[Any] | str | None = None,
    ) -> tuple[UUID, datetime]:
        """
        Insert a row into public.media_assets and return (id, created_at).

        Note: If uq_media_assets_user_sha256 triggers, we return the existing row.
        We also best-effort patch the existing row with the new storage_ref/meta_json so
        callers don't get stuck with an expired SAS.
        """
        aid = uuid4()
        pool = await get_pool()

        meta_val = self._meta_to_jsonb(meta_json)
        meta_val = self._ensure_storage_identity_meta(storage_ref, meta_val)

        b = self._clamp_int(bytes_len, min_value=0)
        w = None if width is None else self._clamp_int(width, min_value=0)
        h = None if height is None else self._clamp_int(height, min_value=0)
        d = None if duration_ms is None else self._clamp_int(duration_ms, min_value=0)

        try:
            row = await pool.fetchrow(
                """
                insert into public.media_assets(
                    id, user_id, kind, storage_ref, content_type, bytes, sha256,
                    width, height, duration_ms, meta_json,
                    created_at, updated_at
                )
                values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11, now(), now())
                returning id, created_at
                """,
                aid,
                user_id,
                kind,
                storage_ref,
                content_type,
                b,
                sha256_hex,
                w,
                h,
                d,
                meta_val,
            )
        except asyncpg.exceptions.UniqueViolationError:
            if not sha256_hex:
                raise
            existing = await self.get_by_user_sha256(user_id=user_id, sha256_hex=sha256_hex)
            if not existing:
                raise

            existing_id = self._as_uuid(existing["id"], aid)

            # Best-effort: refresh storage_ref + patch meta on the existing row
            try:
                await self.update_storage_ref(
                    asset_id=existing_id,
                    storage_ref=storage_ref,
                    meta_json=(meta_val if isinstance(meta_val, (dict, list, str)) else None),
                    merge_meta=True,
                )
            except Exception:
                pass

            return existing_id, existing["created_at"]

        if not row:
            raise RuntimeError("media_asset_insert_failed")

        return self._as_uuid(row["id"], aid), row["created_at"]

    async def create_asset_id_only(
        self,
        *,
        user_id: UUID,
        kind: str,
        storage_ref: str,
        content_type: str,
        bytes_len: int,
        sha256_hex: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_ms: int | None = None,
        meta_json: dict[str, Any] | list[Any] | str | None = None,
    ) -> UUID:
        asset_id, _ = await self.create_asset(
            user_id=user_id,
            kind=kind,
            storage_ref=storage_ref,
            content_type=content_type,
            bytes_len=bytes_len,
            sha256_hex=sha256_hex,
            width=width,
            height=height,
            duration_ms=duration_ms,
            meta_json=meta_json,
        )
        return asset_id