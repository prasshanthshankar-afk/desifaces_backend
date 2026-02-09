from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from typing import List, Optional, Tuple

import httpx
from azure.storage.blob import BlobServiceClient, ContentSettings

from app.config import settings
from app.db import get_db_pool
from app.services.sas_service import parse_blob_path_from_sas_url
from app.services.sas_service import AzureBlobService  # you already use this in routes


async def _download(url: str, path: str) -> None:
    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)


def _ffmpeg_concat(file_list_path: str, out_path: str) -> None:
    # -safe 0 allows absolute paths in concat list file
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", file_list_path,
        "-c", "copy",
        out_path,
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {p.stderr[-2000:]}")


def _upload_final_mp4(connection_string: str, container: str, blob_path: str, local_path: str) -> None:
    bsc = BlobServiceClient.from_connection_string(connection_string)
    bc = bsc.get_blob_client(container=container, blob=blob_path)

    with open(local_path, "rb") as f:
        bc.upload_blob(
            f,
            overwrite=True,
            content_settings=ContentSettings(content_type="video/mp4"),
        )


async def _claim_one_stitch_job(conn) -> Optional[dict]:
    # Claim one job in stitching state (avoid double stitch)
    row = await conn.fetchrow(
        """
        with cte as (
          select id
          from public.longform_jobs
          where status = 'stitching'
          order by created_at asc
          for update skip locked
          limit 1
        )
        update public.longform_jobs j
        set updated_at = now()
        where j.id in (select id from cte)
        returning j.*;
        """
    )
    return dict(row) if row else None


async def _load_segments_for_job(conn, job_id: str) -> List[dict]:
    rows = await conn.fetch(
        """
        select segment_index, status, segment_video_url, segment_storage_path
        from public.longform_segments
        where job_id = $1::uuid
        order by segment_index asc
        """,
        job_id,
    )
    return [dict(r) for r in rows]


async def stitch_loop() -> None:
    if not settings.STITCH_WORKER_ENABLED:
        return

    pool = await get_db_pool()
    az = AzureBlobService(settings.AZURE_STORAGE_CONNECTION_STRING)

    while True:
        async with pool.acquire() as conn:
            job = await _claim_one_stitch_job(conn)

        if not job:
            await asyncio.sleep(settings.STITCH_WORKER_POLL_SECONDS)
            continue

        job_id = str(job["id"])
        user_id = str(job["user_id"])

        try:
            async with pool.acquire() as conn:
                segs = await _load_segments_for_job(conn, job_id)

            if not segs:
                raise RuntimeError("No segments found for stitching")

            # Ensure all succeeded and have urls
            for s in segs:
                if (s.get("status") or "").lower() != "succeeded":
                    raise RuntimeError(f"Segment not succeeded: index={s['segment_index']} status={s.get('status')}")
                if not s.get("segment_video_url"):
                    raise RuntimeError(f"Missing segment_video_url for segment {s['segment_index']}")

            with tempfile.TemporaryDirectory(prefix="df_longform_") as td:
                # Download segment files
                local_files: List[str] = []
                for s in segs:
                    lp = os.path.join(td, f"seg_{int(s['segment_index']):04d}.mp4")
                    await _download(s["segment_video_url"], lp)
                    local_files.append(lp)

                # Build concat list
                list_path = os.path.join(td, "concat.txt")
                with open(list_path, "w", encoding="utf-8") as f:
                    for lp in local_files:
                        f.write(f"file '{lp}'\n")

                out_path = os.path.join(td, "final.mp4")
                _ffmpeg_concat(list_path, out_path)

                # Upload final
                final_blob_path = f"{user_id}/{job_id}/final.mp4"
                _upload_final_mp4(
                    settings.AZURE_STORAGE_CONNECTION_STRING,
                    settings.AZURE_FINAL_VIDEO_CONTAINER,
                    final_blob_path,
                    out_path,
                )

                # Mint final SAS for response caching
                final_sas_url = az.sign_read_url(
                    settings.AZURE_FINAL_VIDEO_CONTAINER,
                    final_blob_path,
                    settings.FINAL_SAS_TTL_SECONDS,
                )

            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update public.longform_jobs
                    set status='succeeded',
                        final_storage_path=$2,
                        final_video_url=$3,
                        updated_at=now()
                    where id=$1::uuid
                    """,
                    job_id,
                    final_blob_path,
                    final_sas_url,
                )

        except Exception as e:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update public.longform_jobs
                    set status='failed', error_code='STITCH_FAILED', error_message=$2, updated_at=now()
                    where id=$1::uuid
                    """,
                    job_id,
                    str(e),
                )

        await asyncio.sleep(0.1)


if __name__ == "__main__":
    asyncio.run(stitch_loop())