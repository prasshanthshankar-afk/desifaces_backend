import os
import uuid
import tempfile
from typing import Optional, Tuple, List

from app.domain.enums import SegmentStatus, LongformJobStatus
from app.repos.longform_jobs_repo import LongformJobsRepo
from app.repos.longform_segments_repo import LongformSegmentsRepo
from app.http_clients.audio_client import create_tts_audio
from app.http_clients.fusion_client import create_video_segment, get_video_job
from app.services.stitch_service import stitch_videos, upload_final_mp4


async def process_one_segment(
    jobs: LongformJobsRepo,
    segs: LongformSegmentsRepo,
    segment_row,
    user_token: str,
    image_ref: str,
    voice_json: dict,
    output_profile: str,
) -> None:
    seg_id = str(segment_row["id"])
    job_id = str(segment_row["longform_job_id"])
    idx = int(segment_row["segment_index"])
    status = segment_row["status"]
    attempt = int(segment_row["attempt_count"] or 0)

    # 1) TTS if needed
    if status in ("queued", "tts_pending") and not segment_row["audio_url"]:
        await segs.update_segment(seg_id, SegmentStatus.tts_pending.value, attempt)
        tts = await create_tts_audio(user_token, segment_row["script_text"], voice_json)
        await segs.update_segment(
            seg_id,
            SegmentStatus.video_pending.value,
            attempt,
            tts_job_id=str(tts.get("job_id")) if tts.get("job_id") else None,
            audio_url=tts.get("audio_url"),
            audio_storage_path=tts.get("storage_path"),
        )

    # refresh row? (cheap approach: use existing values from DB on next poll)
    # 2) Create video segment if needed
    if segment_row["fusion_job_id"] is None:
        attempt += 1
        idem = f"{job_id}:{idx}:{attempt}"
        vjob = await create_video_segment(user_token, image_ref, segment_row["audio_url"], idem, output_profile)
        await segs.update_segment(
            seg_id,
            SegmentStatus.video_pending.value,
            attempt,
            fusion_job_id=str(vjob.get("job_id") or vjob.get("id") or ""),
        )

    # 3) Poll fusion job
    fusion_job_id = segment_row["fusion_job_id"]
    if not fusion_job_id:
        return

    st = await get_video_job(user_token, fusion_job_id)
    st_status = (st.get("status") or "").lower()
    if st_status in ("succeeded", "success", "done"):
        # Expect a URL in response
        video_url = st.get("video_url") or st.get("output_url") or st.get("url")
        storage_path = st.get("storage_path") or (st.get("meta") or {}).get("storage_path")
        await segs.update_segment(
            seg_id,
            SegmentStatus.succeeded.value,
            attempt,
            video_url=video_url,
            video_storage_path=storage_path,
        )
    elif st_status in ("failed", "error"):
        await segs.update_segment(seg_id, SegmentStatus.failed.value, attempt, last_error=str(st.get("error") or st))
    else:
        # still running
        await segs.update_segment(seg_id, SegmentStatus.video_pending.value, attempt)


async def stitch_if_ready(
    jobs: LongformJobsRepo,
    segs: LongformSegmentsRepo,
    job_row,
) -> None:
    job_id = str(job_row["id"])
    if job_row["status"] not in ("running", "stitching"):
        return

    if await segs.any_failed(job_id):
        await jobs.set_status(job_id, LongformJobStatus.failed.value, last_error="One or more segments failed")
        return

    done = await segs.count_done(job_id)
    total = int(job_row["segments_total"] or 0)
    await jobs.set_counts(job_id, total, done)

    if total > 0 and done == total:
        await jobs.set_status(job_id, LongformJobStatus.stitching.value)

        rows = await segs.list_by_job(job_id)
        video_urls = [r["video_url"] for r in rows]
        if any(not u for u in video_urls):
            await jobs.set_status(job_id, LongformJobStatus.failed.value, last_error="Missing segment video_url")
            return

        # Download segments locally and stitch
        import httpx
        with tempfile.TemporaryDirectory() as td:
            local_files: List[str] = []
            async with httpx.AsyncClient(timeout=300) as client:
                for i, url in enumerate(video_urls):
                    outp = os.path.join(td, f"seg_{i:04d}.mp4")
                    rr = await client.get(url)
                    rr.raise_for_status()
                    with open(outp, "wb") as f:
                        f.write(rr.content)
                    local_files.append(outp)

            final_local = os.path.join(td, "final.mp4")
            stitch_videos(local_files, final_local)
            storage_path, signed_url = upload_final_mp4(final_local)
            await jobs.set_final(job_id, storage_path, signed_url)
            await jobs.set_status(job_id, LongformJobStatus.succeeded.value)