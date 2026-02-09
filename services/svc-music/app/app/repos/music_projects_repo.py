from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid4

from app.db import get_pool

_SCHEMA = "public"
_TABLE_PROJECTS = f"{_SCHEMA}.music_projects"
_TABLE_PERFORMERS = f"{_SCHEMA}.music_performers"
_TABLE_ALIGNMENT = f"{_SCHEMA}.music_alignment"
_TABLE_MEDIA_ASSETS = f"{_SCHEMA}.media_assets"

_VALID_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

_ALLOWED_VOICE_MODE = {"uploaded", "generated", "none"}


class MusicProjectsRepo:
    async def create(
        self,
        *,
        user_id: UUID,
        title: str,
        mode: str,
        duet_layout: str,
        language_hint: str | None,
    ) -> UUID:
        pid = uuid4()
        pool = await get_pool()
        await pool.execute(
            f"""
            insert into {_TABLE_PROJECTS}(id, user_id, title, mode, duet_layout, language_hint)
            values($1,$2,$3,$4,$5,$6)
            """,
            pid,
            user_id,
            title,
            mode,
            duet_layout,
            language_hint,
        )
        return pid

    async def get(self, *, project_id: UUID, user_id: UUID) -> dict | None:
        pool = await get_pool()
        row = await pool.fetchrow(
            f"select * from {_TABLE_PROJECTS} where id=$1 and user_id=$2",
            project_id,
            user_id,
        )
        return dict(row) if row else None

    async def set_status(self, *, project_id: UUID, user_id: UUID, status: str) -> None:
        pool = await get_pool()
        await pool.execute(
            f"update {_TABLE_PROJECTS} set status=$3, updated_at=now() where id=$1 and user_id=$2",
            project_id,
            user_id,
            status,
        )

    async def update_style(
        self,
        *,
        project_id: UUID,
        user_id: UUID,
        scene_pack_id: str | None,
        camera_edit: str | None,
        band_pack: list[str] | None,
    ) -> None:
        """
        Updates the style fields on music_projects.

        Note: band_pack column is NOT NULL text[] with default ARRAY[]::text[].
        We coerce None -> [] to avoid NOT NULL violations.
        """
        pool = await get_pool()
        await pool.execute(
            f"""
            update {_TABLE_PROJECTS}
               set scene_pack_id=$3,
                   camera_edit=$4,
                   band_pack=$5,
                   updated_at=now()
             where id=$1 and user_id=$2
            """,
            project_id,
            user_id,
            scene_pack_id,
            camera_edit,
            band_pack or [],  # avoid NULL into NOT NULL text[]
        )

    async def upsert_performer(
        self,
        *,
        project_id: UUID,
        role: str,
        image_asset_id: UUID,
        voice_mode: str = "uploaded",  # uploaded|generated|none
        user_is_owner: bool,
    ) -> None:
        """
        Upsert into music_performers.

        DB columns:
          id, project_id, role(enum), image_asset_id(not null), voice_mode(not null), user_is_owner, created_at
        """
        if voice_mode not in _ALLOWED_VOICE_MODE:
            raise ValueError(f"Invalid voice_mode: {voice_mode} (allowed: {_ALLOWED_VOICE_MODE})")

        pool = await get_pool()
        pid = uuid4()
        await pool.execute(
            f"""
            insert into {_TABLE_PERFORMERS}(id, project_id, role, image_asset_id, voice_mode, user_is_owner)
            values($1,$2,$3,$4,$5,$6)
            on conflict(project_id, role)
            do update set
              image_asset_id=excluded.image_asset_id,
              voice_mode=excluded.voice_mode,
              user_is_owner=excluded.user_is_owner
            """,
            pid,
            project_id,
            role,
            image_asset_id,
            voice_mode,
            user_is_owner,
        )

    async def upsert_lyrics(self, *, project_id: UUID, lyrics_text: str) -> None:
        pool = await get_pool()
        await pool.execute(
            f"""
            insert into {_TABLE_ALIGNMENT}(project_id, lyrics_text, created_at, updated_at)
            values($1,$2,now(),now())
            on conflict(project_id)
            do update set lyrics_text=excluded.lyrics_text, updated_at=now()
            """,
            project_id,
            lyrics_text,
        )

    async def get_lyrics(self, *, project_id: UUID) -> dict | None:
        pool = await get_pool()
        row = await pool.fetchrow(
            f"""
            select project_id, lyrics_text, created_at, updated_at
              from {_TABLE_ALIGNMENT}
             where project_id=$1
            """,
            project_id,
        )
        return dict(row) if row else None

    async def get_performers(self, *, project_id: UUID) -> list[dict]:
        pool = await get_pool()
        rows = await pool.fetch(
            f"""
            select p.*,
                   m.storage_ref as image_url,
                   m.content_type as image_content_type
              from {_TABLE_PERFORMERS} p
              join {_TABLE_MEDIA_ASSETS} m on m.id = p.image_asset_id
             where p.project_id=$1
             order by p.role asc
            """,
            project_id,
        )
        return [dict(r) for r in rows]

    async def get_user_id(self, *, project_id: UUID) -> UUID | None:
        pool = await get_pool()
        row = await pool.fetchrow(f"select user_id from {_TABLE_PROJECTS} where id=$1", project_id)
        return row["user_id"] if row else None

    async def update(self, *, project_id: UUID, user_id: UUID, **fields: Any) -> None:
        """
        Patch-update music_projects columns safely.

        - Only updates keys that are actual columns in public.music_projects
        - Ignores unknown keys (so callers won't crash if schema drifted)
        - Always bumps updated_at when present
        """
        if not fields:
            return

        pool = await get_pool()

        col_rows = await pool.fetch(
            """
            select column_name
              from information_schema.columns
             where table_schema=$1
               and table_name='music_projects'
            """,
            _SCHEMA,
        )
        existing_cols = {r["column_name"] for r in col_rows}

        for k in ("id", "user_id", "created_at"):
            existing_cols.discard(k)

        patch = {k: v for k, v in fields.items() if k in existing_cols}
        if not patch:
            return

        for k in patch.keys():
            if not _VALID_IDENT.match(k):
                raise ValueError(f"Invalid column name for update(): {k}")

        sets: list[str] = []
        args: list[Any] = [project_id, user_id]
        idx = 3

        for k, v in patch.items():
            sets.append(f'"{k}"=${idx}')
            args.append(v)
            idx += 1

        if "updated_at" in existing_cols and "updated_at" not in patch:
            sets.append("updated_at=now()")

        sql = f"""
        update {_TABLE_PROJECTS}
           set {", ".join(sets)}
         where id=$1 and user_id=$2
        """
        await pool.execute(sql, *args)