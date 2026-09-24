from __future__ import annotations

import asyncio
import json
import os
import struct
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any

import asyncpg
import fal_client

from app.services.artifact_service import ArtifactService
from app.services.providers.base import ProviderPrepareInput
from app.services.providers.omnihuman_adapter import OmniHumanAdapter


def _dict(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value or {})
    except Exception:
        return {}


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def make_vertical_speaker_mask(*, width: int, height: int, target_x: int, all_x: list[int]) -> bytes:
    if width < 64 or height < 64:
        raise RuntimeError("invalid_shared_scene_dimensions")
    xs = sorted({max(0, min(width - 1, int(x))) for x in all_x})
    if not xs:
        xs = [max(0, min(width - 1, target_x))]
    target = min(xs, key=lambda x: abs(x - target_x))
    idx = xs.index(target)
    left = 0 if idx == 0 else int(round((xs[idx - 1] + target) / 2.0))
    right = width if idx == len(xs) - 1 else int(round((target + xs[idx + 1]) / 2.0))

    # Pull boundaries slightly toward the active speaker to avoid selecting a
    # neighboring person while still covering the speaker's full body column.
    margin = max(2, int(round(width * 0.01)))
    if left > 0:
        left += margin
    if right < width:
        right -= margin
    left = max(0, min(width - 1, left))
    right = max(left + 1, min(width, right))

    row = bytes([0]) + bytes([255 if left <= x < right else 0 for x in range(width)])
    raw = row * height
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw, level=9))
        + _png_chunk(b"IEND", b"")
    )


async def refresh_url(artifact_service: ArtifactService, url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    if ".blob.core.windows.net/" not in value:
        return value
    try:
        return await artifact_service.mint_read_sas_for_url(value, ttl_hours=8)
    except Exception:
        return value


async def main() -> None:
    database_url = str(os.getenv("DATABASE_URL") or "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL missing")
    if not str(os.getenv("FAL_KEY") or "").strip():
        raise SystemExit("FAL_KEY missing")

    conn = await asyncpg.connect(database_url)
    artifact_service = ArtifactService()
    try:
        rows = await conn.fetch(
            """
            select id::text as job_id, user_id::text as user_id, payload_json, updated_at
            from public.studio_jobs
            where studio_type='fusion'
              and payload_json->>'provider'='sync3'
              and payload_json #>> '{tags,conversation_mode}'='shared_scene'
              and status='succeeded'
            order by updated_at desc
            limit 20
            """
        )
        if not rows:
            raise RuntimeError("no_succeeded_shared_scene_sync3_jobs_found")

        latest_payload = _dict(rows[0]["payload_json"])
        stage_run_id = str((_dict(latest_payload.get("tags"))).get("stage_run_id") or "").strip()
        if not stage_run_id:
            raise RuntimeError("latest_sync3_job_missing_stage_run_id")

        siblings = await conn.fetch(
            """
            select id::text as job_id, user_id::text as user_id, status, payload_json, updated_at
            from public.studio_jobs
            where studio_type='fusion'
              and payload_json->>'provider'='sync3'
              and payload_json #>> '{tags,conversation_mode}'='shared_scene'
              and payload_json #>> '{tags,stage_run_id}'=$1
            order by updated_at desc
            """,
            stage_run_id,
        )
        if not siblings:
            raise RuntimeError("shared_scene_children_not_found")

        succeeded = []
        turn_ids: list[str] = []
        for row in siblings:
            payload = _dict(row["payload_json"])
            if str(row["status"] or "") != "succeeded":
                continue
            turn_id = str((_dict(payload.get("tags"))).get("dialogue_turn_id") or "").strip()
            if turn_id:
                turn_ids.append(turn_id)
            succeeded.append((row, payload, turn_id))
        if not succeeded:
            raise RuntimeError("no_succeeded_sync3_child_available_for_bakeoff")

        duration_by_turn: dict[str, float] = {}
        if turn_ids:
            duration_rows = await conn.fetch(
                """
                select turn_id::text as turn_id, duration_hint_ms
                from public.v3_dialogue_turns
                where turn_id::text = any($1::text[])
                """,
                sorted(set(turn_ids)),
            )
            for duration_row in duration_rows:
                try:
                    duration_by_turn[str(duration_row["turn_id"])] = max(
                        0.0,
                        float(duration_row["duration_hint_ms"] or 0) / 1000.0,
                    )
                except Exception:
                    pass

        candidates = []
        for row, payload, turn_id in succeeded:
            duration = duration_by_turn.get(turn_id, 0.0)
            if duration <= 0:
                try:
                    duration = float(_dict(payload.get("video")).get("duration_sec") or 0.0)
                except Exception:
                    duration = 0.0
            candidates.append((duration, row, payload, turn_id))

        # Prefer the longest successful turn so the final bakeoff gives the motion
        # model enough time to execute a visible sequence of body/hand actions.
        _, selected_row, payload, selected_turn_id = max(candidates, key=lambda item: item[0])
        job_id = str(selected_row["job_id"])
        user_id = str(selected_row["user_id"])

        artifact = await conn.fetchrow(
            """
            select url
            from public.artifacts
            where job_id=$1::uuid and kind='video' and url is not null
            order by created_at desc, id desc
            limit 1
            """,
            job_id,
        )
        if not artifact:
            raise RuntimeError("sync_baseline_video_artifact_missing")

        stage = await conn.fetchrow(
            """
            select metadata_json
            from public.v3_studio_stage_runs
            where stage_run_id=$1::uuid
            limit 1
            """,
            stage_run_id,
        )
        if not stage:
            raise RuntimeError("shared_scene_stage_not_found")

        stage_meta = _dict(stage["metadata_json"])
        dims = _dict(stage_meta.get("shared_scene_dimensions"))
        width = int(dims.get("width") or 0)
        height = int(dims.get("height") or 0)
        if width < 64 or height < 64:
            raise RuntimeError("shared_scene_dimensions_missing")

        provider_options = _dict(payload.get("provider_options"))
        coords = provider_options.get("active_speaker_coordinates")
        if not isinstance(coords, (list, tuple)) or len(coords) != 2:
            raise RuntimeError("selected_child_missing_active_speaker_coordinates")
        target_x, target_y = int(coords[0]), int(coords[1])

        all_x: list[int] = []
        for row in siblings:
            sibling_payload = _dict(row["payload_json"])
            sibling_options = _dict(sibling_payload.get("provider_options"))
            sibling_coords = sibling_options.get("active_speaker_coordinates")
            if isinstance(sibling_coords, (list, tuple)) and len(sibling_coords) == 2:
                try:
                    all_x.append(int(sibling_coords[0]))
                except Exception:
                    pass

        image_url = str(payload.get("face_image_url") or "").strip()
        voice_audio = _dict(payload.get("voice_audio"))
        audio_url = str(voice_audio.get("audio_url") or "").strip()
        if not image_url or not audio_url:
            raise RuntimeError("selected_child_missing_image_or_audio_url")

        image_url = await refresh_url(artifact_service, image_url)
        audio_url = await refresh_url(artifact_service, audio_url)
        baseline_url = await refresh_url(artifact_service, str(artifact["url"]))

        mask_bytes = make_vertical_speaker_mask(
            width=width,
            height=height,
            target_x=target_x,
            all_x=all_x,
        )
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(mask_bytes)
            mask_path = tmp.name
        try:
            mask_url = await asyncio.to_thread(fal_client.upload_file, mask_path)
        finally:
            try:
                Path(mask_path).unlink(missing_ok=True)
            except Exception:
                pass

        duration_sec = duration_by_turn.get(selected_turn_id, 0.0)
        if duration_sec <= 0:
            try:
                duration_sec = float(_dict(payload.get("video")).get("duration_sec") or 0.0)
            except Exception:
                duration_sec = 0.0

        motion_prompt = (
            "Static medium-wide camera. Natural two-person seated conversation. Animate only the active speaker "
            "selected by the mask; the listener stays stable except for tiny natural breathing and attentive eye focus. "
            "Use a clear sequential performance rather than only facial animation. Start from the source pose. "
            "During the opening phrase, the active speaker makes one visible conversational gesture with the nearest "
            "visible hand: lift it naturally from its resting position, open the palm slightly toward the listener, "
            "then lower it partway. During the middle phrase, lean the torso forward slightly, shift the shoulders, "
            "and nod once while continuing realistic lip-sync. During the next phrase, make a second smaller hand "
            "gesture near the body and then relax the arm. During the closing phrase, ease the torso back toward the "
            "original seated posture. Maintain realistic breathing, small posture adjustments, expressive eyes and "
            "micro-expressions throughout. Preserve face identity, clothing, hand anatomy, the listener, furniture, "
            "lighting and room background. No camera movement, no cuts, no reframing, no exaggerated waving, no extra "
            "fingers, no body warping, and no background deformation. End close to the original pose."
        )

        adapter = OmniHumanAdapter()
        prepared = await adapter.prepare(
            ProviderPrepareInput(
                job_id=f"bakeoff-{job_id}",
                user_id=user_id,
                request_payload={
                    "video": {"duration_sec": duration_sec} if duration_sec > 0 else {},
                    "provider_options": {
                        "mask_url": str(mask_url),
                        "resolution": "720p",
                        "turbo_mode": False,
                        "prompt": motion_prompt,
                    },
                    "tags": {
                        "conversation_mode": "shared_scene",
                        "bakeoff_source_job_id": job_id,
                        "stage_run_id": stage_run_id,
                    },
                },
                resolved_face_url=image_url,
                resolved_audio_url=audio_url,
            )
        )

        submitted = await adapter.submit(
            prepared.request_json,
            f"desifaces-motion-bakeoff-{job_id}-{int(time.time())}",
        )
        provider_job_id = submitted.provider_job_id

        deadline = time.monotonic() + 900
        poll = None
        while time.monotonic() < deadline:
            poll = await adapter.poll(provider_job_id)
            print(f"OMNIHUMAN_STATUS={poll.status}", flush=True)
            if poll.status == "succeeded" and poll.video_url:
                break
            if poll.status in {"failed", "canceled"}:
                raise RuntimeError(f"omnihuman_bakeoff_failed:{poll.error_message or poll.status}")
            await asyncio.sleep(5)
        else:
            raise RuntimeError("omnihuman_bakeoff_timeout")

        candidate_url = await artifact_service.persist_video_artifact(
            str(poll.video_url),
            user_id=user_id,
            job_id=f"bakeoff-{stage_run_id}",
            provider_job_id=provider_job_id,
            ttl_hours=48,
        )

        print("============================================================")
        print(" SHARED-SCENE MOTION BAKEOFF READY")
        print("============================================================")
        print(f"STAGE_RUN_ID={stage_run_id}")
        print(f"SOURCE_SYNC_JOB_ID={job_id}")
        print(f"SPEAKER_COORDINATES={target_x},{target_y}")
        print(f"IMAGE_DIMENSIONS={width}x{height}")
        print(f"DIALOGUE_TURN_ID={selected_turn_id}")
        print(f"DURATION_SEC={duration_sec}")
        print("MOTION_PROMPT_PROFILE=explicit_sequential_hand_torso_v2")
        print("SYNC3_BASELINE_VIDEO_URL=" + baseline_url)
        print("OMNIHUMAN_MASK_VIDEO_URL=" + candidate_url)
        print("MASK_URL=" + str(mask_url))
        print("PRODUCTION_TOUCH=NONE")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
