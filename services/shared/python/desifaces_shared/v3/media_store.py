from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence
from uuid import UUID, uuid4

from df_contracts.v3.domain import MediaAsset, MediaKind, MediaRole


class MediaOwnershipError(RuntimeError):
    pass


class MediaNotFoundError(RuntimeError):
    pass


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[key]
        return default if value is None else value
    except Exception:
        pass
    try:
        value = row.get(key)
        return default if value is None else value
    except Exception:
        return default


def normalize_media_kind(kind: Any, mime_type: Optional[str] = None) -> MediaKind:
    raw = str(kind or "").strip().lower()
    mime = str(mime_type or "").strip().lower()
    if raw in {"audio", "audio_master", "song_audio", "full_mix", "voice_ref", "voice_reference", "byo_audio"} or mime.startswith("audio/"):
        return MediaKind.AUDIO
    if raw in {"video", "final_video"} or mime.startswith("video/"):
        return MediaKind.VIDEO
    if raw in {"image", "face", "face_image", "upload", "source_image", "thumb", "thumbnail"} or mime.startswith("image/"):
        return MediaKind.IMAGE
    if mime.startswith("text/") or mime.startswith("application/"):
        return MediaKind.DOCUMENT
    try:
        return MediaKind(raw)
    except Exception:
        return MediaKind.OTHER


def normalize_media_role(role: Any, *, kind: Any = None, metadata: Optional[Mapping[str, Any]] = None) -> MediaRole:
    raw = str(role or "").strip().lower()
    if raw:
        try:
            return MediaRole(raw)
        except Exception:
            pass
    k = str(kind or "").strip().lower()
    md = dict(metadata or {})
    if k in {"upload", "source", "source_image", "voice_ref", "voice_reference", "byo_audio"}:
        return MediaRole.SOURCE
    if k in {"thumb", "thumbnail"} or "thumbnail" in k:
        return MediaRole.THUMBNAIL
    if "preview" in k:
        return MediaRole.PREVIEW
    if bool(md.get("is_final")) or str(md.get("final_only") or "").lower() in {"1", "true"}:
        return MediaRole.FINAL
    if k in {"face", "face_image", "image", "audio", "audio_master", "song_audio", "full_mix", "video", "final_video"}:
        return MediaRole.FINAL
    return MediaRole.INTERMEDIATE


class CanonicalMediaStore:
    """Persistence boundary for the V3 MediaAsset lifecycle.

    The store intentionally uses the existing ``public.media_assets.id`` as
    canonical ``MediaAsset.media_id``. ``storage_ref`` is the durable storage
    identity. Signed/SAS URLs are delivery concerns and must not be persisted as
    new canonical identities merely because they have a different query string.
    """

    async def create(
        self,
        conn,
        *,
        account_id: UUID,
        owner_user_id: UUID,
        kind: MediaKind,
        role: MediaRole,
        storage_uri: str,
        project_id: UUID | None = None,
        mime_type: str | None = None,
        sha256: str | None = None,
        bytes_len: int | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_ms: int | None = None,
        thumbnail_media_id: UUID | None = None,
        source_media_ids: Sequence[UUID] = (),
        parent_job_id: UUID | None = None,
        metadata: Optional[Mapping[str, Any]] = None,
        media_id: UUID | None = None,
    ) -> MediaAsset:
        if not str(storage_uri or "").strip():
            raise ValueError("storage_uri_required")

        # Existing schema has per-user content de-duplication when sha256 is
        # present. Preserve that invariant instead of creating a second asset id.
        if sha256:
            existing = await conn.fetchrow(
                """
                select id
                from public.media_assets
                where user_id = $1 and sha256 = $2
                limit 1
                """,
                owner_user_id,
                sha256,
            )
            if existing:
                existing_id = UUID(str(_row_get(existing, "id")))
                await conn.execute(
                    """
                    update public.media_assets
                    set account_id = coalesce(account_id, $2),
                        project_id = coalesce(project_id, $3),
                        updated_at = now()
                    where id = $1
                    """,
                    existing_id,
                    account_id,
                    project_id,
                )
                for sequence_no, source_id in enumerate(source_media_ids):
                    await self.link_lineage(
                        conn,
                        source_media_id=source_id,
                        derived_media_id=existing_id,
                        sequence_no=sequence_no,
                    )
                return await self.get(conn, media_id=existing_id, account_id=account_id)

        chosen_id = media_id or uuid4()
        await conn.execute(
            """
            insert into public.media_assets(
              id, user_id, account_id, project_id, kind, role, lifecycle_state,
              storage_ref, content_type, bytes, sha256, width, height, duration_ms,
              thumbnail_media_id, parent_generation_job_id, meta_json, created_at, updated_at
            )
            values(
              $1,$2,$3,$4,$5,$6,'active',$7,$8,$9,$10,$11,$12,$13,$14,$15,$16::jsonb,now(),now()
            )
            """,
            chosen_id,
            owner_user_id,
            account_id,
            project_id,
            kind.value,
            role.value,
            storage_uri,
            mime_type,
            bytes_len,
            sha256,
            width,
            height,
            duration_ms,
            thumbnail_media_id,
            parent_job_id,
            json.dumps(dict(metadata or {}), default=str),
        )

        for sequence_no, source_id in enumerate(source_media_ids):
            await self.link_lineage(
                conn,
                source_media_id=source_id,
                derived_media_id=chosen_id,
                sequence_no=sequence_no,
            )
        return await self.get(conn, media_id=chosen_id, account_id=account_id)

    async def get(self, conn, *, media_id: UUID, account_id: UUID | None = None) -> MediaAsset:
        row = await conn.fetchrow(
            """
            select * from public.v3_media_assets where media_id = $1
            """,
            media_id,
        )
        if not row:
            raise MediaNotFoundError(f"media_not_found:{media_id}")
        row_account_id = _row_get(row, "account_id")
        if account_id is not None and str(row_account_id or "") != str(account_id):
            raise MediaOwnershipError(f"media_forbidden:{media_id}")

        lineage = await conn.fetch(
            """
            select source_media_id
            from public.v3_media_asset_lineage
            where derived_media_id = $1
            order by sequence_no asc, created_at asc
            """,
            media_id,
        )
        source_ids = tuple(UUID(str(_row_get(item, "source_media_id"))) for item in lineage)

        created_at = _row_get(row, "created_at")
        if not isinstance(created_at, datetime):
            raise RuntimeError(f"media_created_at_invalid:{media_id}")
        if row_account_id is None:
            raise RuntimeError(f"media_account_context_missing:{media_id}")

        return MediaAsset(
            media_id=UUID(str(_row_get(row, "media_id"))),
            account_id=UUID(str(row_account_id)),
            owner_user_id=UUID(str(_row_get(row, "owner_user_id"))) if _row_get(row, "owner_user_id") else None,
            project_id=UUID(str(_row_get(row, "project_id"))) if _row_get(row, "project_id") else None,
            kind=normalize_media_kind(_row_get(row, "media_kind"), _row_get(row, "mime_type")),
            role=normalize_media_role(_row_get(row, "role"), metadata=_as_dict(_row_get(row, "metadata"))),
            mime_type=_row_get(row, "mime_type"),
            storage_uri=str(_row_get(row, "storage_uri")),
            thumbnail_media_id=UUID(str(_row_get(row, "thumbnail_media_id"))) if _row_get(row, "thumbnail_media_id") else None,
            source_media_ids=source_ids,
            parent_job_id=UUID(str(_row_get(row, "parent_generation_job_id"))) if _row_get(row, "parent_generation_job_id") else None,
            metadata={
                **_as_dict(_row_get(row, "metadata")),
                "lifecycle_state": _row_get(row, "lifecycle_state"),
                "sha256": _row_get(row, "sha256"),
                "bytes": _row_get(row, "bytes"),
                "width": _row_get(row, "width"),
                "height": _row_get(row, "height"),
                "duration_ms": _row_get(row, "duration_ms"),
                "retention_until": str(_row_get(row, "retention_until")) if _row_get(row, "retention_until") else None,
            },
            created_at=created_at,
        )

    async def assert_owned(self, conn, *, account_id: UUID, media_ids: Iterable[UUID]) -> tuple[UUID, ...]:
        ids = tuple(dict.fromkeys(UUID(str(value)) for value in media_ids))
        if not ids:
            return ()
        rows = await conn.fetch(
            "select id from public.media_assets where id = any($1::uuid[]) and account_id = $2 and lifecycle_state <> 'deleted'",
            list(ids),
            account_id,
        )
        found = {UUID(str(_row_get(row, "id"))) for row in rows}
        missing = [value for value in ids if value not in found]
        if missing:
            raise MediaOwnershipError("media_missing_or_forbidden:" + ",".join(map(str, missing)))
        return ids

    async def link_lineage(
        self,
        conn,
        *,
        source_media_id: UUID,
        derived_media_id: UUID,
        relation: str = "derived_from",
        sequence_no: int = 0,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if source_media_id == derived_media_id:
            raise ValueError("media_lineage_self_reference")
        await conn.execute(
            """
            insert into public.v3_media_asset_lineage(
              source_media_id, derived_media_id, relation, sequence_no, metadata_json
            )
            values($1,$2,$3,$4,$5::jsonb)
            on conflict(source_media_id, derived_media_id, relation)
            do update set sequence_no = excluded.sequence_no,
                          metadata_json = public.v3_media_asset_lineage.metadata_json || excluded.metadata_json
            """,
            source_media_id,
            derived_media_id,
            str(relation or "derived_from"),
            max(0, int(sequence_no)),
            json.dumps(dict(metadata or {}), default=str),
        )

    async def set_thumbnail(self, conn, *, account_id: UUID, media_id: UUID, thumbnail_media_id: UUID) -> None:
        await self.assert_owned(conn, account_id=account_id, media_ids=(media_id, thumbnail_media_id))
        await conn.execute(
            "update public.media_assets set thumbnail_media_id=$2, updated_at=now() where id=$1 and account_id=$3",
            media_id,
            thumbnail_media_id,
            account_id,
        )
        await self.link_lineage(
            conn,
            source_media_id=media_id,
            derived_media_id=thumbnail_media_id,
            relation="thumbnail_of",
        )

    async def archive(self, conn, *, account_id: UUID, media_id: UUID) -> None:
        status = await conn.execute(
            "update public.media_assets set lifecycle_state='archived', updated_at=now() where id=$1 and account_id=$2 and lifecycle_state='active'",
            media_id,
            account_id,
        )
        if str(status).endswith("0"):
            await self.get(conn, media_id=media_id, account_id=account_id)

    async def soft_delete(self, conn, *, account_id: UUID, media_id: UUID) -> None:
        status = await conn.execute(
            "update public.media_assets set lifecycle_state='deleted', deleted_at=coalesce(deleted_at,now()), updated_at=now() where id=$1 and account_id=$2",
            media_id,
            account_id,
        )
        if str(status).endswith("0"):
            await self.get(conn, media_id=media_id, account_id=account_id)

    async def list_final(
        self,
        conn,
        *,
        account_id: UUID,
        owner_user_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MediaAsset]:
        rows = await conn.fetch(
            """
            select media_id
            from public.v3_media_assets
            where account_id=$1
              and role='final'
              and lifecycle_state='active'
              and ($2::uuid is null or owner_user_id=$2)
            order by created_at desc, media_id desc
            limit $3 offset $4
            """,
            account_id,
            owner_user_id,
            max(1, min(int(limit), 200)),
            max(0, int(offset)),
        )
        return [await self.get(conn, media_id=UUID(str(_row_get(row, "media_id"))), account_id=account_id) for row in rows]
