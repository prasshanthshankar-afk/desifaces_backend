"""Read-only production media coverage audit; run inside the stitch worker."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit
from uuid import UUID

import asyncpg
from azure.storage.blob import BlobServiceClient


def object_value(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    try:
        async with conn.transaction(readonly=True):
            if len(sys.argv) < 2:
                rows = await conn.fetch("SELECT id,status,total_segments,completed_segments,created_at FROM public.longform_jobs ORDER BY created_at DESC LIMIT 10")
                print(json.dumps([dict(r) for r in rows], default=str, indent=2))
                print("Select a newly generated production talking-video job ID for media verification.")
                return
            job_id = str(UUID(sys.argv[1]))
            job = await conn.fetchrow("SELECT * FROM public.longform_jobs WHERE id=$1::uuid", job_id)
            rows = await conn.fetch("SELECT * FROM public.longform_segments WHERE job_id=$1::uuid ORDER BY segment_index", job_id)
    finally:
        await conn.close()
    if not job:
        raise RuntimeError("JOB_NOT_FOUND")
    tags = object_value(job["tags"])
    if tags.get("longform_profile") != "talking_video":
        raise RuntimeError("Select a talking-video job; this audit is not a conversation or cinematic timing audit")
    source = tags.get("voice_audio_url") or object_value(tags.get("selected_audio")).get("audio_url")
    if not source:
        raise RuntimeError("AUTHORITATIVE_AUDIO_REFERENCE_MISSING")
    failures = []
    if job["status"] != "succeeded":
        failures.append("job has not succeeded")
    if len(rows) != job["total_segments"] or job["completed_segments"] != job["total_segments"]:
        failures.append("segment counts do not match")
    if [r["segment_index"] for r in rows] != list(range(len(rows))):
        failures.append("segment indices are not contiguous")
    storage = BlobServiceClient.from_connection_string(os.environ["AZURE_STORAGE_CONNECTION_STRING"])
    try:
        with tempfile.TemporaryDirectory(prefix="coverage-audit-") as folder:
            def probe(reference, label):
                if not reference:
                    raise RuntimeError("MEDIA_REFERENCE_MISSING: " + label)
                path = unquote(urlsplit(reference).path).lstrip("/")
                container, blob = path.split("/", 1)
                local = Path(folder) / label
                with local.open("wb") as handle:
                    storage.get_blob_client(container, blob).download_blob().readinto(handle)
                data = json.loads(subprocess.check_output([
                    "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json", str(local)
                ], text=True, timeout=30))
                result = {s["codec_type"]: float(s["duration"]) for s in data["streams"] if s.get("duration")}
                print(label, json.dumps(result))
                return result

            original = probe(source, "original_audio")["audio"]
            sliced_sum = 0.0
            for row in rows:
                index = row["segment_index"]
                sliced = probe(row["audio_url"], f"segment_{index}_audio")["audio"]
                video = probe(row["segment_video_url"], f"segment_{index}_video")
                sliced_sum += sliced
                if min(video.get("audio", 0), video.get("video", 0)) < sliced - 0.15:
                    failures.append(f"segment {index} is materially shorter than its audio")
            reference = job["final_storage_path"]
            if not reference:
                raise RuntimeError("FINAL_STORAGE_PATH_MISSING")
            if not reference.startswith("video-output/"):
                reference = "video-output/" + reference
            final = probe(reference, "final_video")
            if sliced_sum < original - 0.15:
                failures.append("sliced audio does not cover original duration")
            if min(final.get("audio", 0), final.get("video", 0)) < original - 0.15:
                failures.append("final media is materially shorter than original audio")
            windows = object_value(object_value(tags.get("directed_plan")).get("segments_by_index"))
            cursor = 0.0
            for index in range(len(rows)):
                window = object_value(windows.get(str(index)))
                if "audio_start_sec" not in window or "audio_end_sec" not in window:
                    failures.append(f"segment {index} audio window is missing")
                    continue
                start, end = float(window["audio_start_sec"]), float(window["audio_end_sec"])
                if abs(start - cursor) > 0.01 or end <= start:
                    failures.append(f"segment {index} has a gap, overlap, or invalid window")
                cursor = end
            if cursor < original - 0.15:
                failures.append("planned windows do not cover original audio")
            from app.services.talking_video_finish import should_finish_single_person
            if should_finish_single_person(tags, len(rows)):
                receipt = object_value(tags.get("single_person_finish"))
                if not receipt or min(final.get("audio", 0), final.get("video", 0)) < original + 0.70:
                    failures.append("single-person closing hold is missing")
            print("job_id=" + job_id)
            print("segment_count=" + str(len(rows)))
            print("MEDIA_DURATION_AND_WINDOW_COVERAGE=" + ("FAIL" if failures else "PASS"))
            print("Speech content and transition quality still require playback review.")
            if failures:
                print(json.dumps(failures, indent=2))
                raise SystemExit(1)
    finally:
        storage.close()


asyncio.run(main())
