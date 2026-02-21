from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from app.db import get_pool

JsonDict = Dict[str, Any]


class MusicStylePresetsRepo:
    async def list_presets_for_cookbook_backfill(self) -> List[JsonDict]:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            select
              id, name,
              tags,
              scene_primary_tag, scene_secondary_tags,
              mood_tag, energy_tag,
              face_mode, grade,
              shot_cookbook_version, shot_cookbook_json
            from public.music_style_presets
            order by name asc
            """
        )
        return [dict(r) for r in rows]

    async def get_by_name(self, *, name: str) -> Optional[JsonDict]:
        pool = await get_pool()
        row = await pool.fetchrow(
            """
            select
              id, name,
              tags,
              scene_primary_tag, scene_secondary_tags,
              mood_tag, energy_tag,
              face_mode, grade,
              shot_cookbook_version, shot_cookbook_json
            from public.music_style_presets
            where name = $1
            """,
            name,
        )
        return dict(row) if row else None

    async def update_shot_cookbook(
        self,
        *,
        preset_id: UUID,
        cookbook_version: int,
        cookbook_json: JsonDict,
    ) -> None:
        pool = await get_pool()
        await pool.execute(
            """
            update public.music_style_presets
            set shot_cookbook_version = $2,
                shot_cookbook_json = $3
            where id = $1
            """,
            preset_id,
            cookbook_version,
            cookbook_json,
        )