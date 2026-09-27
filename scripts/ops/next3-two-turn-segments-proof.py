#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import asyncpg
import httpx

from app.services.sas_service import AzureBlobService


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _db_dsn() -> str:
    raw = _clean(os.getenv("DATABASE_URL"))
    if raw.startswith("postgresql+asyncpg://"):
        raw = "postgresql://" + raw.split("://", 1)[1]
    if not raw:
        raise RuntimeError("DATABASE_URL_MISSING")
    return raw


def _blob_location(row: asyncpg.Record) -> tuple[str, str]:
    meta = _dict(row["meta_json"])
    container = _clean(meta.get("storage_container"))
    blob = _clean(meta.get("blob_name") or meta.get("storage_path"))
    storage_ref = _clean(row["storage_ref"])

    for raw in (blob, storage_ref):
        if not raw:
            continue
        if raw.startswith(("az://", "azure://")):
            path = raw.split("://", 1)[1].lstrip("/")
            if "/" in path:
                c, b = path.split("/", 1)
                return container or c, b
        if raw.startswith(("http://", "https://")):
            path = urlparse(raw).path.lstrip("/")
            if "/" in path:
                c, b = path.split("/", 1)
                return container or c, b
    if container and blob:
        return container, blob.lstrip("/")
    raise RuntimeError(f"MEDIA_STORAGE_LOCATION_MISSING:{row['id']}")


def _speaker_coordinates(stage_meta: dict[str, Any], participant_id: UUID) -> list[int]:
    dims = _dict(stage_meta.get("shared_scene_dimensions"))
    targets = _dict(stage_meta.get("speaker_targets"))
    target = _dict(targets.get(str(participant_id)))
    width = int(dims.get("width") or 0)
    height = int(dims.get("height") or 0)
    if width < 64 or height < 64:
        raise RuntimeError("SHARED_SCENE_DIMENSIONS_INVALID")

    point = _dict(target.get("point"))
    if point:
        x = float(point["x"])
        y = float(point["y"])
    else:
        box = _dict(target.get("box"))
        if not box:
            raise RuntimeError(f"SPEAKER_TARGET_MISSING:{participant_id}")
        x = float(box["x"]) + float(box["width"]) / 2.0
        y = float(box["y"]) + float(box["height"]) / 2.0

    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise RuntimeError(f"SPEAKER_TARGET_INVALID:{participant_id}")

    return [
        max(0, min(width - 1, int(round(x * width)))),
        max(0, min(height - 1, int(round(y * height)))),
    ]


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "COMMAND_FAILED:"
            + " ".join(cmd)
            + "\nSTDERR:\n"
            + (proc.stderr or "")[-3000:]
        )


async def _download(client: httpx.AsyncClient, url: str, path: Path) -> None:
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        with path.open("wb") as handle:
            async for chunk in response.aiter_bytes(1024 * 1024):
                handle.write(chunk)


def _probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("AUDIO_DURATION_PROBE_FAILED:" + (proc.stderr or "")[-1200:])
    duration = float((proc.stdout or "0").strip())
    if duration <= 0:
        raise RuntimeError("AUDIO_DURATION_INVALID")
    return duration


def _active_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("activeGenerations", "active_generations", "count", "total"):
            value = payload.get(key)
            if isinstance(value, (int, float)):
                return max(0, int(value))
        for key in ("data", "items", "results", "generations"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
    return 0


async def _wait_capacity(client: httpx.AsyncClient, headers: dict[str, str], base_url: str) -> None:
    deadline = time.monotonic() + 900
    while True:
        response = await client.get(f"{base_url}/v2/generations?status=PROCESSING", headers=headers)
        if response.status_code == 200 and _active_count(response.json()) < 1:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("SYNC3_CAPACITY_WAIT_TIMEOUT")
        await asyncio.sleep(5)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--stage-run-id", required=True)
    parser.add_argument("--fps", type=int, default=25)
    args = parser.parse_args()

    workflow_id = UUID(args.workflow_id)
    stage_run_id = UUID(args.stage_run_id)
    fps = max(24, min(30, int(args.fps)))

    sync_key = _clean(os.getenv("SYNC_API_KEY"))
    sync_base = _clean(os.getenv("SYNC_API_BASE_URL") or "https://api.sync.so").rstrip("/")
    azure_conn = _clean(os.getenv("AZURE_STORAGE_CONNECTION_STRING"))
    output_container = _clean(os.getenv("AZURE_VIDEO_OUTPUT_CONTAINER") or "video-output")
    if not sync_key:
        raise RuntimeError("SYNC_API_KEY_MISSING")
    if not azure_conn:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING_MISSING")

    conn = await asyncpg.connect(_db_dsn())
    try:
        stage = await conn.fetchrow(
            """
            select s.stage_run_id,s.scene_id,s.metadata_json,
                   w.workflow_id,w.account_id,w.owner_user_id,w.project_id
            from public.v3_studio_stage_runs s
            join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
            where s.stage_run_id=$1 and w.workflow_id=$2
              and s.stage_type='fusion' and s.scope_type='scene'
            """,
            stage_run_id,
            workflow_id,
        )
        if not stage:
            raise RuntimeError("TARGET_SHARED_SCENE_STAGE_NOT_FOUND")

        stage_meta = _dict(stage["metadata_json"])
        if _clean(stage_meta.get("conversation_mode")).lower() != "shared_scene":
            raise RuntimeError("TARGET_STAGE_NOT_SHARED_SCENE")

        shared_media_id = UUID(_clean(stage_meta.get("shared_scene_media_id")))
        image_row = await conn.fetchrow(
            """
            select id,storage_ref,meta_json,width,height,lifecycle_state
            from public.media_assets
            where id=$1 and lifecycle_state='active'
            """,
            shared_media_id,
        )
        if not image_row:
            raise RuntimeError("SHARED_SCENE_MEDIA_NOT_ACTIVE")

        rows = await conn.fetch(
            """
            select dt.turn_id,dt.sequence_no,dt.speaker_participant_id,p.display_name,
                   ao.media_id as audio_media_id,
                   ma.storage_ref,ma.meta_json,ma.duration_ms,ma.lifecycle_state
            from public.v3_dialogue_turns dt
            join public.v3_participants p on p.participant_id=dt.speaker_participant_id
            join public.v3_studio_stage_runs a
              on a.workflow_id=$1 and a.stage_type='audio'
             and a.scope_type='dialogue_turn' and a.dialogue_turn_id=dt.turn_id
             and a.state='approved'
            join public.v3_studio_stage_outputs ao
              on ao.stage_run_id=a.stage_run_id and ao.is_active=true
            join public.v3_studio_review_items ar
              on ar.stage_run_id=a.stage_run_id and ar.media_id=ao.media_id
             and ar.decision='approved'
            join public.media_assets ma on ma.id=ao.media_id
            where dt.scene_id=$2 and dt.turn_kind='speech'
              and ma.lifecycle_state='active'
            order by dt.sequence_no,dt.turn_id
            """,
            workflow_id,
            stage["scene_id"],
        )
        if len(rows) < 2:
            raise RuntimeError("TWO_APPROVED_AUDIO_TURNS_REQUIRED")

        first = rows[0]
        second = next(
            (row for row in rows[1:] if row["speaker_participant_id"] != first["speaker_participant_id"]),
            None,
        )
        if second is None:
            raise RuntimeError("TWO_DISTINCT_SPEAKERS_REQUIRED")
        selected = [first, second]
    finally:
        await conn.close()

    azure = AzureBlobService(azure_conn)
    image_container, image_blob = _blob_location(image_row)
    image_url = azure.sign_read_url(image_container, image_blob, 3600)

    manifest_turns: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="df_next3_segments_proof_") as td:
        root = Path(td)
        image_path = root / "source-image"
        async with httpx.AsyncClient(timeout=120) as download_client:
            await _download(download_client, image_url, image_path)

            audio_paths: list[Path] = []
            audio_urls: list[str] = []
            durations: list[float] = []
            for index, row in enumerate(selected, start=1):
                container, blob = _blob_location(row)
                url = azure.sign_read_url(container, blob, 3600)
                path = root / f"audio-{index}.bin"
                await _download(download_client, url, path)
                duration = (
                    float(row["duration_ms"]) / 1000.0
                    if row["duration_ms"] is not None and int(row["duration_ms"]) > 0
                    else _probe_duration(path)
                )
                duration = max(0.25, round(duration, 3))
                audio_paths.append(path)
                audio_urls.append(url)
                durations.append(duration)

        total_duration = round(sum(durations), 3)
        source_video = root / "static-source.mp4"
        _run(
            [
                "ffmpeg", "-y",
                "-loop", "1",
                "-i", str(image_path),
                "-t", f"{total_duration:.3f}",
                "-r", str(fps),
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1",
                "-an",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "18",
                "-pix_fmt", "yuv420p",
                str(source_video),
            ]
        )

        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        base_path = f"v3/qa/shared-scene-segments/{workflow_id}/{stage_run_id}/{stamp}"
        source_blob = f"{base_path}/static-source.mp4"
        azure.upload_file(output_container, source_blob, str(source_video), "video/mp4")
        source_video_url = azure.sign_read_url(output_container, source_blob, 3600)

        segments: list[dict[str, Any]] = []
        inputs: list[dict[str, Any]] = [{"type": "video", "url": source_video_url}]
        cursor = 0.0
        for index, (row, audio_url, duration) in enumerate(
            zip(selected, audio_urls, durations), start=1
        ):
            ref_id = f"audio_{index}"
            coordinates = _speaker_coordinates(stage_meta, UUID(str(row["speaker_participant_id"])))
            start_time = round(cursor, 3)
            end_time = round(cursor + duration, 3)
            frame_number = max(0, int(round(start_time * fps)))
            inputs.append({"type": "audio", "url": audio_url, "refId": ref_id})
            segments.append(
                {
                    "startTime": start_time,
                    "endTime": end_time,
                    "audioInput": {"refId": ref_id},
                    "optionsOverride": {
                        "active_speaker_detection": {
                            "auto_detect": False,
                            "frame_number": frame_number,
                            "coordinates": coordinates,
                        }
                    },
                }
            )
            manifest_turns.append(
                {
                    "sequence_no": int(row["sequence_no"]),
                    "dialogue_turn_id": str(row["turn_id"]),
                    "participant_id": str(row["speaker_participant_id"]),
                    "display_name": _clean(row["display_name"]),
                    "audio_media_id": str(row["audio_media_id"]),
                    "duration_seconds": duration,
                    "start_time": start_time,
                    "end_time": end_time,
                    "frame_number": frame_number,
                    "coordinates": coordinates,
                }
            )
            cursor = end_time

        request_json = {
            "model": "sync-3",
            "input": inputs,
            "segments": segments,
            "outputFileName": re.sub(r"[^A-Za-z0-9_-]", "_", f"next3_segments_{stage_run_id}")[:120],
        }

        print("============================================================")
        print(" NEXT3 TWO-TURN SEGMENTS PROOF")
        print(" mutation=provider_generation_and_qa_blob_only")
        print(" db_write=NONE")
        print(" production_touch=NONE")
        print(f"workflow_id={workflow_id}")
        print(f"stage_run_id={stage_run_id}")
        print(f"shared_scene_media_id={shared_media_id}")
        print(f"fps={fps}")
        for turn in manifest_turns:
            print(
                "TURN "
                f"seq={turn['sequence_no']} speaker={turn['display_name']} "
                f"coords={turn['coordinates']} "
                f"start={turn['start_time']} end={turn['end_time']}"
            )
        print("============================================================")

        headers = {
            "x-api-key": sync_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            await _wait_capacity(client, headers, sync_base)
            response = await client.post(
                f"{sync_base}/v2/generate",
                headers=headers,
                json=request_json,
            )
            if response.status_code not in {200, 201, 202}:
                raise RuntimeError(
                    f"SYNC_SEGMENTS_SUBMIT_FAILED:{response.status_code}:{response.text[:2000]}"
                )
            payload = response.json()
            provider_job_id = _clean(payload.get("id"))
            if not provider_job_id:
                raise RuntimeError("SYNC_SEGMENTS_JOB_ID_MISSING")
            print(f"SYNC_SEGMENTS_PROVIDER_JOB_ID={provider_job_id}")

            deadline = time.monotonic() + 1800
            output_url = ""
            while time.monotonic() < deadline:
                await asyncio.sleep(8)
                status_response = await client.get(
                    f"{sync_base}/v2/generate/{provider_job_id}",
                    headers=headers,
                )
                if status_response.status_code != 200:
                    raise RuntimeError(
                        f"SYNC_SEGMENTS_STATUS_FAILED:{status_response.status_code}:{status_response.text[:1200]}"
                    )
                status_payload = status_response.json()
                status = _clean(status_payload.get("status")).upper()
                print(f"SYNC_SEGMENTS_STATUS={status}")
                if status == "COMPLETED":
                    output_url = _clean(
                        status_payload.get("outputUrl")
                        or status_payload.get("segmentOutputUrl")
                    )
                    if not output_url:
                        raise RuntimeError("SYNC_SEGMENTS_COMPLETED_WITHOUT_OUTPUT_URL")
                    break
                if status in {"FAILED", "REJECTED", "CANCELED"}:
                    raise RuntimeError(
                        "SYNC_SEGMENTS_PROVIDER_FAILED:"
                        + _clean(status_payload.get("error") or status_payload.get("errorCode") or status)
                    )
            if not output_url:
                raise RuntimeError("SYNC_SEGMENTS_POLL_TIMEOUT")

            output_path = root / "segments-output.mp4"
            await _download(client, output_url, output_path)

        output_blob = f"{base_path}/segments-output.mp4"
        azure.upload_file(output_container, output_blob, str(output_path), "video/mp4")
        durable_output_url = azure.sign_read_url(output_container, output_blob, 15 * 24 * 3600)

        manifest = {
            "contract": "next3_shared_scene_two_turn_segments_proof_v1",
            "workflow_id": str(workflow_id),
            "stage_run_id": str(stage_run_id),
            "shared_scene_media_id": str(shared_media_id),
            "provider": "sync3",
            "provider_model": "sync-3",
            "provider_job_id": provider_job_id,
            "fps": fps,
            "total_duration_seconds": total_duration,
            "turns": manifest_turns,
            "active_speaker_qc": {
                "status": "MANUAL_REVIEW_REQUIRED",
                "reason": "Two-turn provider proof must be visually approved before full-scene generation. Automated active-speaker QC is the next gate and is intentionally not bypassed.",
            },
            "qa_storage_path": output_blob,
        }
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        manifest_blob = f"{base_path}/manifest.json"
        azure.upload_file(output_container, manifest_blob, str(manifest_path), "application/json")
        manifest_url = azure.sign_read_url(output_container, manifest_blob, 15 * 24 * 3600)

        print("============================================================")
        print("NEXT3_TWO_TURN_SEGMENTS_PROVIDER=PASS")
        print("ACTIVE_SPEAKER_QC=MANUAL_REVIEW_REQUIRED")
        print(f"OUTPUT_URL={durable_output_url}")
        print(f"MANIFEST_URL={manifest_url}")
        print("FULL_SEVEN_TURN_GENERATION=BLOCKED_UNTIL_QC_PASS")
        print("DATABASE_WRITE=NONE")
        print("PRODUCTION_TOUCH=NONE")
        print("============================================================")


if __name__ == "__main__":
    asyncio.run(main())
