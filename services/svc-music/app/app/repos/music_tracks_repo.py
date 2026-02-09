from __future__ import annotations

import json
from typing import Any, Dict, Optional
from uuid import UUID

from app.db import get_pool


def _jsonb_object_param(x: Any) -> str:
    """
    Always return JSON text representing an OBJECT, suitable for binding into $N::jsonb.

    Goals:
      - Never store jsonb scalars (string/number/etc) that can break jsonb_set(...).
      - If caller passes a JSON string, accept only if it parses to an object; otherwise wrap.
      - If caller passes list/scalar, wrap under {"items": ...} or {"value": ...}.
      - Be resilient to UUID/datetime/etc by using default=str.
    """
    if x is None:
        return "{}"

    if isinstance(x, dict):
        return json.dumps(x, ensure_ascii=False, default=str)

    if isinstance(x, list):
        return json.dumps({"items": x}, ensure_ascii=False, default=str)

    if isinstance(x, str):
        s = x.strip()
        if not s:
            return "{}"
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return json.dumps(parsed, ensure_ascii=False, default=str)
            return json.dumps({"value": parsed}, ensure_ascii=False, default=str)
        except Exception:
            return json.dumps({"value": s}, ensure_ascii=False, default=str)

    return json.dumps({"value": x}, ensure_ascii=False, default=str)


class MusicTracksRepo:
    async def upsert_track(
        self,
        *,
        project_id: UUID,
        track_type: str,   # music_track_type enum (lowercase)
        duration_ms: int,  # NOT NULL in schema
        artifact_id: UUID | None,
        media_asset_id: UUID | None,
        meta_json: Optional[Dict[str, Any]] = None,  # durable BYO URL + misc metadata
    ) -> UUID:
        # Normalize in case caller passes Enum or mixed-case
        tt = str(getattr(track_type, "value", track_type)).strip().lower()

        d = int(duration_ms or 0)
        if d < 1:
            d = 1

        pool = await get_pool()
        row = await pool.fetchrow(
            """
            insert into music_tracks(
              id, project_id, track_type, duration_ms, artifact_id, media_asset_id, meta_json
            )
            values(
              gen_random_uuid(),
              $1,
              $2::music_track_type,
              $3,
              $4,
              $5,
              $6::jsonb
            )
            on conflict (project_id, track_type)
            do update set
              duration_ms = excluded.duration_ms,
              artifact_id = excluded.artifact_id,
              media_asset_id = excluded.media_asset_id,

              -- Hardened merge:
              --  - If existing meta_json is an object => keep it
              --  - If existing meta_json is a jsonb string that contains JSON text => parse it (best-effort)
              --  - Otherwise => treat as {}
              --  - Merge in excluded meta_json (only if it's an object)
              meta_json =
                (
                  case
                    when music_tracks.meta_json is null then '{}'::jsonb
                    when jsonb_typeof(music_tracks.meta_json) = 'object' then music_tracks.meta_json
                    when jsonb_typeof(music_tracks.meta_json) = 'string'
                         and left(btrim(music_tracks.meta_json #>> '{}'), 1) = '{'
                      then (music_tracks.meta_json #>> '{}')::jsonb
                    else '{}'::jsonb
                  end
                )
                ||
                (
                  case
                    when excluded.meta_json is null then '{}'::jsonb
                    when jsonb_typeof(excluded.meta_json) = 'object' then excluded.meta_json
                    else '{}'::jsonb
                  end
                ),

              updated_at = now()
            returning id
            """,
            project_id,
            tt,
            d,
            artifact_id,
            media_asset_id,
            _jsonb_object_param(meta_json),
        )
        return row["id"]

    async def list_by_project(self, *, project_id: UUID) -> list[dict]:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            select
              id, project_id, track_type, artifact_id, media_asset_id,
              duration_ms, meta_json, created_at, updated_at
            from music_tracks
            where project_id=$1
            order by updated_at desc, created_at desc
            """,
            project_id,
        )
        return [dict(r) for r in rows]