import os
import uuid
import subprocess
from typing import List

from azure.storage.blob import BlobServiceClient, ContentSettings
from app.config import settings
from app.services.sas_service import sign_final_video_url


def _run(cmd: List[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nSTDERR:\n{p.stderr}")


def stitch_videos(segment_files: List[str], out_mp4: str) -> None:
    """
    Safe stitch: re-encode to consistent MP4 (H.264/AAC).
    """
    # write concat list
    lst = out_mp4 + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for fp in segment_files:
            f.write(f"file '{fp}'\n")

    _run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", lst,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        out_mp4
    ])


def upload_final_mp4(local_path: str) -> tuple[str, str]:
    """
    Upload stitched MP4 to video-output container.
    Returns (storage_path, signed_url)
    """
    blob_service = BlobServiceClient.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)
    container = blob_service.get_container_client(settings.AZURE_VIDEO_OUTPUT_CONTAINER)
    storage_path = f"longform/{uuid.uuid4()}.mp4"
    blob = container.get_blob_client(storage_path)

    with open(local_path, "rb") as f:
        blob.upload_blob(
            f,
            overwrite=True,
            content_settings=ContentSettings(content_type="video/mp4"),
        )

    signed = sign_final_video_url(storage_path)
    return storage_path, signed