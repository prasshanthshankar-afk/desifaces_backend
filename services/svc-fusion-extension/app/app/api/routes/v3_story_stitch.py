from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import (
    get_current_user_id,
    get_db_pool_dep as get_db_pool,
)
from app.config import settings
from app.services.sas_service import AzureBlobService
from app.services.stitch_service import (
    stitch_video_urls,
    upload_final_mp4,
)
from desifaces_shared.identity import (
    AccountContextNotFound,
    resolve_account_context,
)


router = APIRouter(
    prefix="/api/longform/v3",
    tags=["longform-v3-story-stitch"],
)


class StoryStitchIn(BaseModel):
    project_id: UUID
    workflow_id: UUID
    stage_run_id: UUID


class StoryStitchOut(BaseModel):
    media_id: UUID
    video_url: str
    scene_count: int
    reused: bool = False
    assembly_key: str


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


def _file_sha256_and_size(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0

    with open(path, "rb") as handle:
        for chunk in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)
            total += len(chunk)

    return digest.hexdigest(), total


def _sign_video(
    *,
    container: str,
    blob_name: str,
) -> str:
    sas = AzureBlobService(
        settings.AZURE_STORAGE_CONNECTION_STRING
    )

    return sas.sign_read_url(
        container,
        blob_name,
        int(
            getattr(
                settings,
                "FINAL_SAS_TTL_SECONDS",
                86400,
            )
        ),
    )


async def _resolve_account_or_401(
    conn,
    user_id: UUID,
):
    try:
        return await resolve_account_context(
            conn,
            user_id,
        )
    except AccountContextNotFound as exc:
        raise HTTPException(
            status_code=401,
            detail="account_context_not_found",
        ) from exc


def _storage_location(
    *,
    meta: dict[str, Any],
    storage_ref: str,
) -> tuple[str, str]:
    container = str(
        meta.get("storage_container")
        or getattr(
            settings,
            "AZURE_VIDEO_OUTPUT_CONTAINER",
            "",
        )
        or ""
    ).strip()

    blob_name = str(
        meta.get("storage_path")
        or meta.get("blob_name")
        or ""
    ).strip()

    ref = str(storage_ref or "").strip()

    if (
        (not container or not blob_name)
        and ref.startswith("azure://")
    ):
        remainder = ref[len("azure://"):]

        if "/" in remainder:
            inferred_container, inferred_blob = (
                remainder.split("/", 1)
            )

            container = container or inferred_container
            blob_name = blob_name or inferred_blob

    return container, blob_name


@router.post(
    "/story-stitch",
    response_model=StoryStitchOut,
)
async def stitch_story(
    body: StoryStitchIn,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> StoryStitchOut:

    try:
        canonical_user_id = UUID(str(user_id))
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail="invalid_user_identity",
        ) from exc

    async with pool.acquire() as conn:
        account = await _resolve_account_or_401(
            conn,
            canonical_user_id,
        )

        stage = await conn.fetchrow(
            """
            select
              w.workflow_id,
              w.project_id,
              w.account_id,
              w.current_stage,
              w.state as workflow_state,
              s.stage_run_id,
              s.stage_type,
              s.scope_type,
              s.state as stage_state
            from public.v3_studio_workflows w
            join public.v3_studio_stage_runs s
              on s.workflow_id=w.workflow_id
            where w.workflow_id=$1
              and w.project_id=$2
              and w.account_id=$3
              and w.current_stage='story_final'
              and w.state='active'
              and s.stage_run_id=$4
              and s.stage_type='story_final'
              and s.scope_type='story'
            """,
            body.workflow_id,
            body.project_id,
            account.account_id,
            body.stage_run_id,
        )

        if not stage:
            raise HTTPException(
                status_code=404,
                detail=(
                    "story_stitch_workflow_stage_not_found"
                ),
            )

        blocker_count = int(
            await conn.fetchval(
                """
                select count(*)
                from public.v3_studio_stage_dependencies d
                join public.v3_studio_stage_runs p
                  on p.stage_run_id=d.parent_stage_run_id
                where d.child_stage_run_id=$1
                  and p.state<>'approved'
                """,
                body.stage_run_id,
            )
            or 0
        )

        if blocker_count:
            raise HTTPException(
                status_code=409,
                detail=(
                    "story_stitch_fusion_dependencies_"
                    "not_approved"
                ),
            )

        dependency_count = int(
            await conn.fetchval(
                """
                select count(*)
                from public.v3_studio_stage_dependencies
                where child_stage_run_id=$1
                """,
                body.stage_run_id,
            )
            or 0
        )

        rows = await conn.fetch(
            """
            with candidates as (
              select
                p.stage_run_id,
                p.scene_id,
                sc.sequence_no,
                o.media_id,
                o.created_at as output_created_at,
                m.storage_ref,
                m.meta_json,
                row_number() over(
                  partition by p.stage_run_id
                  order by o.created_at desc,o.media_id
                ) as rn
              from public.v3_studio_stage_dependencies d
              join public.v3_studio_stage_runs p
                on p.stage_run_id=d.parent_stage_run_id
              join public.v3_scenes sc
                on sc.scene_id=p.scene_id
              join public.v3_studio_stage_outputs o
                on o.stage_run_id=p.stage_run_id
               and o.is_active=true
              join public.v3_studio_review_items r
                on r.stage_run_id=o.stage_run_id
               and r.media_id=o.media_id
               and r.decision='approved'
              join public.media_assets m
                on m.id=o.media_id
               and m.account_id=$2
               and m.project_id=$3
               and m.kind='video'
               and m.lifecycle_state='active'
              where d.child_stage_run_id=$1
                and p.stage_type='fusion'
                and p.scope_type='scene'
                and p.state='approved'
            )
            select *
            from candidates
            where rn=1
            order by sequence_no,scene_id
            """,
            body.stage_run_id,
            account.account_id,
            body.project_id,
        )

        if dependency_count < 2:
            raise HTTPException(
                status_code=409,
                detail=(
                    "story_stitch_requires_multiple_"
                    "scene_dependencies"
                ),
            )

        if len(rows) != dependency_count:
            raise HTTPException(
                status_code=409,
                detail=(
                    "story_stitch_approved_scene_"
                    "outputs_incomplete"
                ),
            )

        source_media_ids = [
            UUID(str(row["media_id"]))
            for row in rows
        ]

        assembly_key = hashlib.sha256(
            "|".join(
                str(value)
                for value in source_media_ids
            ).encode("utf-8")
        ).hexdigest()

        existing = await conn.fetchrow(
            """
            select id,meta_json
            from public.media_assets
            where user_id=$1
              and account_id=$2
              and project_id=$3
              and kind='video'
              and lifecycle_state='active'
              and meta_json->>'source_kind'
                    ='v3_story_stitch'
              and meta_json->>'v3_studio_stage_run_id'
                    =$4
              and meta_json->>'assembly_key'
                    =$5
            order by created_at desc
            limit 1
            """,
            canonical_user_id,
            account.account_id,
            body.project_id,
            str(body.stage_run_id),
            assembly_key,
        )

        if existing:
            meta = _as_dict(
                existing["meta_json"]
            )

            container, blob_name = (
                _storage_location(
                    meta=meta,
                    storage_ref="",
                )
            )

            if not container or not blob_name:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "story_stitch_existing_media_"
                        "storage_missing"
                    ),
                )

            return StoryStitchOut(
                media_id=UUID(
                    str(existing["id"])
                ),
                video_url=_sign_video(
                    container=container,
                    blob_name=blob_name,
                ),
                scene_count=len(rows),
                reused=True,
                assembly_key=assembly_key,
            )

        scene_urls: list[str] = []

        for row in rows:
            meta = _as_dict(row["meta_json"])

            container, blob_name = (
                _storage_location(
                    meta=meta,
                    storage_ref=str(
                        row["storage_ref"] or ""
                    ),
                )
            )

            if not container or not blob_name:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "story_stitch_scene_media_"
                        "storage_missing"
                    ),
                )

            scene_urls.append(
                _sign_video(
                    container=container,
                    blob_name=blob_name,
                )
            )

    container = str(
        getattr(
            settings,
            "AZURE_VIDEO_OUTPUT_CONTAINER",
            "",
        )
        or ""
    ).strip()

    if not container:
        raise HTTPException(
            status_code=503,
            detail=(
                "video_output_container_not_configured"
            ),
        )

    storage_path = (
        f"v3/story-final/"
        f"{body.workflow_id}/"
        f"{body.stage_run_id}/"
        f"{assembly_key}.mp4"
    )

    with tempfile.TemporaryDirectory(
        prefix="df_v3_story_stitch_"
    ) as td:
        out_mp4 = os.path.join(
            td,
            "story.mp4",
        )

        try:
            await asyncio.to_thread(
                stitch_video_urls,
                scene_urls,
                out_mp4,
            )

            sha256, byte_count = (
                await asyncio.to_thread(
                    _file_sha256_and_size,
                    out_mp4,
                )
            )

            (
                uploaded_storage_path,
                signed_url,
            ) = await asyncio.to_thread(
                upload_final_mp4,
                out_mp4,
                storage_path=storage_path,
            )

        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    "story_stitch_failed:"
                    f"{str(exc)[:1200]}"
                ),
            ) from exc

    async with pool.acquire() as conn:
        account = await _resolve_account_or_401(
            conn,
            canonical_user_id,
        )

        existing = await conn.fetchrow(
            """
            select id,meta_json
            from public.media_assets
            where user_id=$1
              and account_id=$2
              and project_id=$3
              and kind='video'
              and lifecycle_state='active'
              and meta_json->>'source_kind'
                    ='v3_story_stitch'
              and meta_json->>'v3_studio_stage_run_id'
                    =$4
              and meta_json->>'assembly_key'
                    =$5
            order by created_at desc
            limit 1
            """,
            canonical_user_id,
            account.account_id,
            body.project_id,
            str(body.stage_run_id),
            assembly_key,
        )

        if existing:
            media_id = UUID(
                str(existing["id"])
            )
            meta = _as_dict(
                existing["meta_json"]
            )
            existing_container, existing_blob = (
                _storage_location(
                    meta=meta,
                    storage_ref="",
                )
            )

            if existing_container and existing_blob:
                signed_url = _sign_video(
                    container=existing_container,
                    blob_name=existing_blob,
                )

            reused = True

        else:
            reusable = await conn.fetchrow(
                """
                select id
                from public.media_assets
                where user_id=$1
                  and sha256=$2
                  and lifecycle_state='active'
                order by created_at desc
                limit 1
                """,
                canonical_user_id,
                sha256,
            )

            canonical_sha = (
                sha256
                if reusable is None
                else None
            )

            row = await conn.fetchrow(
                """
                insert into public.media_assets(
                  user_id,
                  kind,
                  storage_ref,
                  content_type,
                  bytes,
                  sha256,
                  meta_json,
                  account_id,
                  project_id,
                  role,
                  lifecycle_state
                )
                values(
                  $1,
                  'video',
                  $2,
                  'video/mp4',
                  $3,
                  $4,
                  $5::jsonb,
                  $6,
                  $7,
                  'preview',
                  'active'
                )
                returning id
                """,
                canonical_user_id,
                (
                    f"azure://{container}/"
                    f"{uploaded_storage_path.lstrip('/')}"
                ),
                byte_count,
                canonical_sha,
                json.dumps(
                    {
                        "source":
                            "svc-fusion-extension",
                        "source_kind":
                            "v3_story_stitch",
                        "v3_workflow_id":
                            str(body.workflow_id),
                        "v3_studio_stage_run_id":
                            str(body.stage_run_id),
                        "assembly_key":
                            assembly_key,
                        "scene_count":
                            len(source_media_ids),
                        "source_scene_media_ids":
                            [
                                str(value)
                                for value
                                in source_media_ids
                            ],
                        "storage_container":
                            container,
                        "storage_path":
                            uploaded_storage_path,
                        "source_sha256":
                            sha256,
                    }
                ),
                account.account_id,
                body.project_id,
            )

            media_id = UUID(
                str(row["id"])
            )

            reused = False

            for sequence_no, source_id in enumerate(
                source_media_ids
            ):
                await conn.execute(
                    """
                    insert into public.v3_media_asset_lineage(
                      source_media_id,
                      derived_media_id,
                      relation,
                      sequence_no,
                      metadata_json
                    )
                    values(
                      $1,
                      $2,
                      'story_assembly',
                      $3,
                      '{}'::jsonb
                    )
                    on conflict(
                      source_media_id,
                      derived_media_id,
                      relation
                    )
                    do update set
                      sequence_no=excluded.sequence_no
                    """,
                    source_id,
                    media_id,
                    sequence_no,
                )

    return StoryStitchOut(
        media_id=media_id,
        video_url=signed_url,
        scene_count=len(source_media_ids),
        reused=reused,
        assembly_key=assembly_key,
    )
