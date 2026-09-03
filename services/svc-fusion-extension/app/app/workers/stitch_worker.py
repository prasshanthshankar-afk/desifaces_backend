from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from typing import List, Optional

import httpx
from azure.storage.blob import BlobServiceClient, ContentSettings

from app.config import settings
from app.db import get_db_pool
from app.services.sas_service import AzureBlobService


logger = logging.getLogger(__name__)


async def _download(url: str, path: str) -> None:
    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)


def _ffmpeg_concat(file_list_path: str, out_path: str) -> None:
    # The concat itself produces one ordered final artifact and is intentionally
    # one ffmpeg operation. Input acquisition and independent parent jobs are
    # parallelized around it.
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


def _extract_poster_frame(video_path: str, poster_path: str) -> None:
    cmd = [
        "ffmpeg", "-y", "-ss", "00:00:01", "-i", video_path,
        "-frames:v", "1", "-vf", "scale=720:-2", "-q:v", "3", poster_path,
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg poster extraction failed: {p.stderr[-2000:]}")
    if not os.path.exists(poster_path) or os.path.getsize(poster_path) == 0:
        raise RuntimeError("ffmpeg poster extraction produced an empty file")


def _upload_poster_jpeg(connection_string: str, container: str, blob_path: str, local_path: str) -> str:
    bsc = BlobServiceClient.from_connection_string(connection_string)
    bc = bsc.get_blob_client(container=container, blob=blob_path)
    with open(local_path, "rb") as f:
        bc.upload_blob(
            f,
            overwrite=True,
            content_settings=ContentSettings(content_type="image/jpeg"),
        )
    return f"https://{bsc.account_name}.blob.core.windows.net/{container}/{blob_path}"


async def _claim_stitch_jobs(conn, limit: int) -> List[dict]:
    """Atomically claim independent parent jobs for parallel stitching.

    `stitching_running` prevents another stitch worker from taking the same
    parent after the row lock is released. A stale claim can be recovered after
    20 minutes if a stitch process dies mid-job.
    """
    rows = await conn.fetch(
        """
        with cte as (
          select id
          from public.longform_jobs
          where status = 'stitching'
             or (status = 'stitching_running' and updated_at < now() - interval '20 minutes')
          order by created_at asc
          for update skip locked
          limit $1::int
        )
        update public.longform_jobs j
        set status = 'stitching_running', updated_at = now()
        where j.id in (select id from cte)
        returning j.*;
        """,
        max(1, int(limit)),
    )
    return [dict(row) for row in rows]


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


async def _download_segments_parallel(segs: List[dict], td: str) -> List[str]:
    ordered: List[tuple[int, str, str]] = []
    for s in segs:
        idx = int(s["segment_index"])
        path = os.path.join(td, f"seg_{idx:04d}.mp4")
        ordered.append((idx, str(s["segment_video_url"]), path))

    await asyncio.gather(*(_download(url, path) for _, url, path in ordered))
    ordered.sort(key=lambda item: item[0])
    return [path for _, _, path in ordered]


async def _process_stitch_job(job: dict, pool, az: AzureBlobService) -> None:
    job_id = str(job["id"])
    user_id = str(job["user_id"])

    try:
        async with pool.acquire() as conn:
            segs = await _load_segments_for_job(conn, job_id)

        if not segs:
            raise RuntimeError("No segments found for stitching")

        for s in segs:
            if (s.get("status") or "").lower() != "succeeded":
                raise RuntimeError(
                    f"Segment not succeeded: index={s['segment_index']} status={s.get('status')}"
                )
            if not s.get("segment_video_url"):
                raise RuntimeError(f"Missing segment_video_url for segment {s['segment_index']}")

        with tempfile.TemporaryDirectory(prefix="df_longform_") as td:
            # Segment files are independent remote objects. Download them in
            # parallel, then preserve segment_index order for the concat list.
            local_files = await _download_segments_parallel(segs, td)

            list_path = os.path.join(td, "concat.txt")
            with open(list_path, "w", encoding="utf-8") as f:
                for lp in local_files:
                    f.write(f"file '{lp}'\n")

            out_path = os.path.join(td, "final.mp4")
            await asyncio.to_thread(_ffmpeg_concat, list_path, out_path)

            final_blob_path = f"{user_id}/{job_id}/final.mp4"
            await asyncio.to_thread(
                _upload_final_mp4,
                settings.AZURE_STORAGE_CONNECTION_STRING,
                settings.AZURE_FINAL_VIDEO_CONTAINER,
                final_blob_path,
                out_path,
            )

            final_sas_url = az.sign_read_url(
                settings.AZURE_FINAL_VIDEO_CONTAINER,
                final_blob_path,
                settings.FINAL_SAS_TTL_SECONDS,
            )

            poster_url: Optional[str] = None
            poster_blob_path: Optional[str] = None
            try:
                poster_path = os.path.join(td, "poster.jpg")
                poster_blob_path = f"longform/posters/{job_id}.jpg"
                await asyncio.to_thread(_extract_poster_frame, out_path, poster_path)
                poster_url = await asyncio.to_thread(
                    _upload_poster_jpeg,
                    settings.AZURE_STORAGE_CONNECTION_STRING,
                    settings.AZURE_FINAL_VIDEO_CONTAINER,
                    poster_blob_path,
                    poster_path,
                )
            except Exception:
                logger.exception(
                    "Longform poster generation failed; preserving video success job_id=%s",
                    job_id,
                )
                poster_url = None
                poster_blob_path = None

        async with pool.acquire() as conn:
            await conn.execute(
                """
                update public.longform_jobs
                set status='succeeded',
                    final_storage_path=$2,
                    final_video_url=$3,
                    tags = coalesce(tags, '{}'::jsonb)
                           || jsonb_strip_nulls(
                                jsonb_build_object(
                                  'thumbnail_url', $4::text,
                                  'poster_url', $4::text,
                                  'cover_url', $4::text,
                                  'thumbnail_blob_path', $5::text
                                )
                              ),
                    updated_at=now()
                where id=$1::uuid
                """,
                job_id,
                final_blob_path,
                final_sas_url,
                poster_url,
                poster_blob_path,
            )

        logger.info(
            "longform stitch succeeded job_id=%s segments=%s",
            job_id,
            len(segs),
        )

    except Exception as e:
        logger.exception("longform stitch failed job_id=%s", job_id)
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


async def stitch_loop() -> None:
    if not settings.STITCH_WORKER_ENABLED:
        return

    pool = await get_db_pool()
    az = AzureBlobService(settings.AZURE_STORAGE_CONNECTION_STRING)
    batch_size = max(1, int(settings.STITCH_WORKER_BATCH_SIZE))
    logger.info("stitch_worker started batch_size=%s", batch_size)

    while True:
        async with pool.acquire() as conn:
            jobs = await _claim_stitch_jobs(conn, batch_size)

        if not jobs:
            await asyncio.sleep(settings.STITCH_WORKER_POLL_SECONDS)
            continue

        # Each parent has an independent final output; stitch parents in parallel.
        await asyncio.gather(*(_process_stitch_job(job, pool, az) for job in jobs))
        await asyncio.sleep(0.05)


if __name__ == "__main__":
    asyncio.run(stitch_loop())
