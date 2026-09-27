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
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import asyncpg
import httpx
from PIL import Image, ImageChops, ImageStat

from azure.storage.blob import BlobServiceClient
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


_TEXT_KEYS = (
    "text_value", "final_text",
    "spoken_text", "dialogue_text", "utterance_text", "script_text",
    "line_text", "text", "content", "source_text", "voiceover_text",
)


def _extract_spoken_text(turn_json: dict[str, Any], audio_meta: dict[str, Any]) -> tuple[str, str]:
    for source_name, source in (("turn", turn_json), ("audio", audio_meta)):
        for key in _TEXT_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), f"{source_name}.{key}"
        for nested_key in ("script", "tts", "request", "input", "metadata"):
            nested = _dict(source.get(nested_key))
            for key in _TEXT_KEYS:
                value = nested.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip(), f"{source_name}.{nested_key}.{key}"
    raise RuntimeError(
        "DIALOGUE_TEXT_NOT_FOUND:"
        f"turn_keys={sorted(turn_json.keys())}:audio_meta_keys={sorted(audio_meta.keys())}"
    )

def _json_from_model_content(content: str) -> dict[str, Any]:
    raw = _clean(content)
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\\s*", "", raw, flags=re.I)
        raw = re.sub(r"\\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"PERFORMANCE_DIRECTOR_JSON_INVALID:{raw[:1200]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("PERFORMANCE_DIRECTOR_JSON_NOT_OBJECT")
    return parsed


async def _performance_director_plan(
    *,
    scene_title: str,
    scene_summary: str,
    scene_direction: dict[str, Any],
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    system = (
        "You are desifaces Performance Director. Design a photorealistic, restrained, context-specific "
        "nonverbal performance for two people in one continuous conversation shot. "
        "Infer emotion from dialogue meaning, explicit emotion_code, scene context, prior/next turn, and speaker/listener role. "
        "Human expression must be nuanced: happy, sad, angry, anxious, relieved, proud, affectionate, skeptical, surprised, "
        "frustrated, calm and other states only when context supports them. Avoid generic smiling, constant nodding, repetitive "
        "gestures, exaggerated acting, or frozen mannequin behavior. The speaker should use natural blinks, eye focus, "
        "micro-expressions, subtle head/body motion and occasional motivated gestures. The listener must remain alive: natural "
        "blinking, gaze toward the speaker, small posture shifts and context-appropriate reactions. CRITICAL: this is a PRE-LIPSYNC "
        "motion plate. Neither person should visibly articulate speech or mouth words; keep mouths neutral apart from subtle non-speech "
        "expression. Preserve both identities, clothing, body shape, composition, background and realistic hands. Use one continuous "
        "stable two-shot with no scene cut, no identity swap, no camera jump, no morphing. Return valid JSON only."
    )
    schema = {
        "scene_emotional_arc": "short string",
        "continuous_motion_prompt": "provider-ready prompt under 2200 chars with explicit timing phases",
        "turns": [
            {
                "sequence_no": 1,
                "speaker_participant_id": "uuid",
                "speaker_name": "string",
                "primary_emotion": "string",
                "secondary_emotion": "string|null",
                "intensity": 0.0,
                "expression_trajectory": ["string", "string"],
                "speaker_gaze": "string",
                "speaker_blinks": "string",
                "speaker_head_motion": "string",
                "speaker_body_motion": "string",
                "speaker_gesture": "string",
                "speaker_microexpressions": ["string"],
                "listener_participant_id": "uuid",
                "listener_name": "string",
                "listener_emotion": "string",
                "listener_reaction": "string",
                "listener_gaze": "string",
                "listener_body_motion": "string",
                "listener_mouth": "silent neutral",
            }
        ],
        "negative_constraints": ["string"],
    }
    payload = {
        "scene_title": scene_title,
        "scene_summary": scene_summary,
        "scene_direction": scene_direction,
        "turns": turns,
        "required_schema": schema,
    }
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                "Create the two-turn performance plan. continuous_motion_prompt must contain exact turn timing in seconds and "
                "describe BOTH speaker and listener behavior for each phase while keeping mouths non-speaking.\\n\\n"
                + json.dumps(payload, ensure_ascii=False)
            ),
        },
    ]

    openai_key = _clean(os.getenv("OPENAI_API_KEY"))
    azure_key = _clean(os.getenv("AZURE_OPENAI_KEY"))
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        if openai_key:
            base = _clean(os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
            model = _clean(os.getenv("DF_PERFORMANCE_DIRECTOR_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini")
            response = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "temperature": 0.25,
                    "response_format": {"type": "json_object"},
                    "messages": messages,
                },
            )
            if response.status_code != 200:
                raise RuntimeError(f"PERFORMANCE_DIRECTOR_OPENAI_FAILED:{response.status_code}:{response.text[:1600]}")
            body = response.json()
            content = _clean(body["choices"][0]["message"]["content"])
            provider_meta = {"provider": "openai", "model": model}
        elif azure_key:
            endpoint = _clean(os.getenv("AZURE_OPENAI_ENDPOINT")).rstrip("/")
            deployment = _clean(os.getenv("AZURE_OPENAI_DEPLOYMENT"))
            api_version = _clean(os.getenv("AZURE_OPENAI_API_VERSION") or "2024-10-21")
            if not endpoint or not deployment:
                raise RuntimeError("PERFORMANCE_DIRECTOR_AZURE_CONFIG_INCOMPLETE")
            response = await client.post(
                f"{endpoint}/openai/deployments/{deployment}/chat/completions",
                params={"api-version": api_version},
                headers={"api-key": azure_key, "Content-Type": "application/json"},
                json={"temperature": 0.25, "response_format": {"type": "json_object"}, "messages": messages},
            )
            if response.status_code != 200:
                raise RuntimeError(f"PERFORMANCE_DIRECTOR_AZURE_FAILED:{response.status_code}:{response.text[:1600]}")
            body = response.json()
            content = _clean(body["choices"][0]["message"]["content"])
            provider_meta = {"provider": "azure_openai", "deployment": deployment}
        else:
            raise RuntimeError("PERFORMANCE_DIRECTOR_LLM_NOT_CONFIGURED")

    plan = _json_from_model_content(content)
    plan_turns = list(plan.get("turns") or [])
    if len(plan_turns) != len(turns):
        raise RuntimeError("PERFORMANCE_DIRECTOR_TURN_COUNT_MISMATCH")
    expected_ids = [str(item["speaker_participant_id"]) for item in turns]
    planned_ids = [str(item.get("speaker_participant_id") or "") for item in plan_turns]
    if planned_ids != expected_ids:
        raise RuntimeError(f"PERFORMANCE_DIRECTOR_SPEAKER_ORDER_MISMATCH:expected={expected_ids}:actual={planned_ids}")
    prompt = _clean(plan.get("continuous_motion_prompt"))
    if not prompt:
        raise RuntimeError("PERFORMANCE_DIRECTOR_MOTION_PROMPT_MISSING")
    if len(prompt) > 2400:
        prompt = prompt[:2400]
        plan["continuous_motion_prompt"] = prompt
    plan["llm"] = provider_meta
    return plan

def _extract_video_url(value: Any) -> str:
    if isinstance(value, str):
        s = value.strip()
        if s.startswith(("http://", "https://")):
            return s
        return ""
    if isinstance(value, list):
        for item in value:
            found = _extract_video_url(item)
            if found:
                return found
        return ""
    if isinstance(value, dict):
        for key in ("video_url", "url"):
            direct = value.get(key)
            if isinstance(direct, str) and direct.strip().startswith(("http://", "https://")):
                return direct.strip()
        for key in ("video", "videos", "output", "outputs", "data", "result", "file"):
            found = _extract_video_url(value.get(key))
            if found:
                return found
        for nested in value.values():
            found = _extract_video_url(nested)
            if found:
                return found
    return ""


async def _fal_generate_motion(
    *,
    model_id: str,
    fal_key: str,
    payload: dict[str, Any],
    existing_request_id: str = "",
) -> tuple[str, str, dict[str, Any]]:
    base_url = _clean(os.getenv("FAL_QUEUE_BASE_URL") or "https://queue.fal.run").rstrip("/")
    headers = {"Authorization": f"Key {fal_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        request_id = _clean(existing_request_id)
        if request_id:
            status_url = f"{base_url}/{model_id}/requests/{request_id}/status"
            response_url = f"{base_url}/{model_id}/requests/{request_id}"
            print(f"PERFORMANCE_MOTION_PROVIDER_JOB_REUSE={request_id}")
        else:
            submit = await client.post(f"{base_url}/{model_id}", headers=headers, json=payload)
            if submit.status_code not in {200, 201, 202}:
                raise RuntimeError(f"PERFORMANCE_MOTION_SUBMIT_FAILED:{submit.status_code}:{submit.text[:1600]}")
            body = submit.json()
            request_id = _clean(body.get("request_id"))
            status_url = _clean(body.get("status_url"))
            response_url = _clean(body.get("response_url"))
            if not request_id or not status_url or not response_url:
                raise RuntimeError(f"PERFORMANCE_MOTION_SUBMIT_RESPONSE_INVALID:{body}")
            print(f"PERFORMANCE_MOTION_PROVIDER_JOB_ID={request_id}")

        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            status_resp = await client.get(status_url, headers=headers)
            if status_resp.status_code not in {200, 202}:
                raise RuntimeError(f"PERFORMANCE_MOTION_STATUS_FAILED:{status_resp.status_code}:{status_resp.text[:1200]}")
            status_payload = status_resp.json()
            status = _clean(status_payload.get("status")).upper()
            print(f"PERFORMANCE_MOTION_STATUS={status or 'UNKNOWN'}")
            if status in {"COMPLETED", "SUCCEEDED"}:
                result_resp = await client.get(response_url, headers=headers)
                if result_resp.status_code != 200:
                    raise RuntimeError(f"PERFORMANCE_MOTION_RESULT_FAILED:{result_resp.status_code}:{result_resp.text[:1200]}")
                result_payload = result_resp.json()
                video_url = _extract_video_url(result_payload)
                if not video_url:
                    raise RuntimeError(f"PERFORMANCE_MOTION_RESULT_VIDEO_MISSING:{json.dumps(result_payload)[:1800]}")
                return request_id, video_url, result_payload
            if status in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
                raise RuntimeError("PERFORMANCE_MOTION_PROVIDER_FAILED:" + json.dumps(status_payload, ensure_ascii=False)[:1800])
            await asyncio.sleep(8)
    raise RuntimeError("PERFORMANCE_MOTION_POLL_TIMEOUT")

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


def _parse_rate(value: str) -> float:
    raw = _clean(value)
    if not raw:
        return 0.0
    if "/" in raw:
        left, right = raw.split("/", 1)
        try:
            denom = float(right)
            return float(left) / denom if denom else 0.0
        except Exception:
            return 0.0
    try:
        return float(raw)
    except Exception:
        return 0.0


def _probe_video_info(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames:format=duration",
            "-of", "json",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("VIDEO_INFO_PROBE_FAILED:" + (proc.stderr or "")[-1200:])
    payload = json.loads(proc.stdout or "{}")
    streams = list(payload.get("streams") or [])
    if not streams:
        raise RuntimeError("VIDEO_INFO_MISSING")
    stream = streams[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    fps = _parse_rate(str(stream.get("avg_frame_rate") or "")) or _parse_rate(str(stream.get("r_frame_rate") or ""))
    duration = float(_dict(payload.get("format")).get("duration") or 0.0)
    frame_count_raw = stream.get("nb_frames")
    try:
        frame_count = int(frame_count_raw) if frame_count_raw not in (None, "", "N/A") else 0
    except Exception:
        frame_count = 0
    if frame_count <= 0 and fps > 0 and duration > 0:
        frame_count = max(1, int(round(fps * duration)))
    if width < 64 or height < 64 or fps <= 0 or duration <= 0:
        raise RuntimeError(
            f"VIDEO_INFO_INVALID:width={width}:height={height}:fps={fps}:duration={duration}"
        )
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "duration": duration,
        "frame_count": frame_count,
    }


def _speaker_coordinates_for_video(
    stage_meta: dict[str, Any],
    participant_id: UUID,
    *,
    video_width: int,
    video_height: int,
) -> list[int]:
    targets = _dict(stage_meta.get("speaker_targets"))
    target = _dict(targets.get(str(participant_id)))
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

    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        raise RuntimeError(f"SPEAKER_TARGET_INVALID:{participant_id}")

    return [
        max(0, min(video_width - 1, int(round(x * video_width)))),
        max(0, min(video_height - 1, int(round(y * video_height)))),
    ]


def _probe_video_geometry(path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("VIDEO_GEOMETRY_PROBE_FAILED:" + (proc.stderr or "")[-1200:])
    payload = json.loads(proc.stdout or "{}")
    streams = list(payload.get("streams") or [])
    if not streams:
        raise RuntimeError("VIDEO_GEOMETRY_MISSING")
    width = int(streams[0].get("width") or 0)
    height = int(streams[0].get("height") or 0)
    if width < 64 or height < 64:
        raise RuntimeError("VIDEO_GEOMETRY_INVALID")
    return width, height


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _correlation(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 4:
        return 0.0
    x = a[:n]
    y = b[:n]
    mx = _mean(x)
    my = _mean(y)
    dx = [v - mx for v in x]
    dy = [v - my for v in y]
    den = math.sqrt(sum(v * v for v in dx) * sum(v * v for v in dy))
    if den <= 1e-12:
        return 0.0
    return max(-1.0, min(1.0, sum(px * py for px, py in zip(dx, dy)) / den))


def _mouth_roi(
    coordinates: list[int],
    *,
    source_width: int,
    source_height: int,
    output_width: int,
    output_height: int,
) -> tuple[int, int, int, int]:
    sx = output_width / float(source_width)
    sy = output_height / float(source_height)
    cx = int(round(coordinates[0] * sx))
    cy = int(round(coordinates[1] * sy + 0.075 * output_height))
    half_w = max(24, int(round(0.06 * output_width)))
    half_h = max(18, int(round(0.04 * output_height)))
    return (
        max(0, cx - half_w),
        max(0, cy - half_h),
        min(output_width, cx + half_w),
        min(output_height, cy + half_h),
    )


def _roi_motion_series(
    frame_paths: list[Path],
    roi: tuple[int, int, int, int],
) -> list[float]:
    if len(frame_paths) < 2:
        return []
    out: list[float] = []
    previous = Image.open(frame_paths[0]).convert("L").crop(roi)
    for frame_path in frame_paths[1:]:
        current = Image.open(frame_path).convert("L").crop(roi)
        diff = ImageChops.difference(current, previous)
        out.append(float(ImageStat.Stat(diff).mean[0]))
        previous = current
    return out


def _active_speaker_qc(
    *,
    video_path: Path,
    proof_turns: list[dict[str, Any]],
    source_width: int,
    source_height: int,
    sample_fps: int = 10,
) -> dict[str, Any]:
    output_width, output_height = _probe_video_geometry(video_path)
    with tempfile.TemporaryDirectory(prefix="df_next3_qc_frames_") as frame_dir:
        pattern = str(Path(frame_dir) / "frame-%06d.png")
        _run(
            [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-vf", f"fps={sample_fps}",
                "-vsync", "vfr",
                pattern,
            ]
        )
        all_frames = sorted(Path(frame_dir).glob("frame-*.png"))
        if len(all_frames) < 4:
            raise RuntimeError("ACTIVE_SPEAKER_QC_INSUFFICIENT_FRAMES")

        rois = {
            turn["participant_id"]: _mouth_roi(
                turn["coordinates"],
                source_width=source_width,
                source_height=source_height,
                output_width=output_width,
                output_height=output_height,
            )
            for turn in proof_turns
        }

        segment_results: list[dict[str, Any]] = []
        overall = "PASS"
        for turn in proof_turns:
            start = max(0.0, float(turn["start_time"]) + 0.15)
            end = max(start, float(turn["end_time"]) - 0.15)
            start_index = max(0, int(math.floor(start * sample_fps)))
            end_index = min(len(all_frames), int(math.ceil(end * sample_fps)) + 1)
            frames = all_frames[start_index:end_index]
            if len(frames) < 4:
                status = "WARN"
                reason = "insufficient_segment_frames"
                intended_score = 0.0
                max_non_speaker_score = 0.0
                max_corr = 0.0
                ratio = 0.0
            else:
                series = {
                    participant_id: _roi_motion_series(frames, roi)
                    for participant_id, roi in rois.items()
                }
                intended_id = turn["participant_id"]
                intended = series[intended_id]
                intended_score = _mean(intended)

                non_speakers = [
                    (pid, values)
                    for pid, values in series.items()
                    if pid != intended_id
                ]
                non_scores = [(_mean(values), pid, values) for pid, values in non_speakers]
                max_non_speaker_score, max_non_id, max_non_series = max(
                    non_scores,
                    default=(0.0, "", []),
                )
                ratio = (
                    max_non_speaker_score / intended_score
                    if intended_score > 1e-9
                    else 999.0
                )
                max_corr = _correlation(intended, max_non_series) if max_non_series else 0.0

                if intended_score < 0.45:
                    status = "FAIL"
                    reason = "intended_speaker_motion_too_low"
                elif ratio >= 0.65:
                    status = "FAIL"
                    reason = "non_speaker_motion_too_high"
                elif ratio >= 0.30 and max_corr >= 0.55:
                    status = "FAIL"
                    reason = "non_speaker_motion_synchronized_with_intended_speaker"
                elif ratio >= 0.25 or max_corr >= 0.50:
                    status = "WARN"
                    reason = "speaker_isolation_borderline"
                else:
                    status = "PASS"
                    reason = "speaker_isolation_detected"

            if status == "FAIL":
                overall = "FAIL"
            elif status == "WARN" and overall != "FAIL":
                overall = "WARN"

            segment_results.append(
                {
                    "sequence_no": turn["sequence_no"],
                    "participant_id": turn["participant_id"],
                    "display_name": turn["display_name"],
                    "status": status,
                    "reason": reason,
                    "intended_motion_score": round(intended_score, 4),
                    "max_non_speaker_motion_score": round(max_non_speaker_score, 4),
                    "non_speaker_to_intended_ratio": round(ratio, 4),
                    "motion_correlation": round(max_corr, 4),
                    "roi": list(rois[turn["participant_id"]]),
                }
            )

        return {
            "status": overall,
            "sample_fps": sample_fps,
            "output_width": output_width,
            "output_height": output_height,
            "segments": segment_results,
            "policy": {
                "auto_accept": "PASS only",
                "warn": "requires human review",
                "fail": "blocks full scene generation",
            },
        }


def _upper_body_roi(
    coordinates: list[int],
    *,
    source_width: int,
    source_height: int,
    output_width: int,
    output_height: int,
) -> tuple[int, int, int, int]:
    sx = output_width / float(source_width)
    sy = output_height / float(source_height)
    cx = int(round(coordinates[0] * sx))
    cy = int(round(coordinates[1] * sy))
    half_w = max(40, int(round(0.11 * output_width)))
    top = max(0, cy - int(round(0.08 * output_height)))
    bottom = min(output_height, cy + int(round(0.32 * output_height)))
    return (
        max(0, cx - half_w),
        top,
        min(output_width, cx + half_w),
        bottom,
    )


def _motion_presence_qc(
    *,
    video_path: Path,
    proof_turns: list[dict[str, Any]],
    source_width: int,
    source_height: int,
    sample_fps: int = 5,
) -> dict[str, Any]:
    output_width, output_height = _probe_video_geometry(video_path)
    with tempfile.TemporaryDirectory(prefix="df_next3_motion_qc_") as frame_dir:
        pattern = str(Path(frame_dir) / "frame-%06d.png")
        _run(["ffmpeg", "-y", "-i", str(video_path), "-vf", f"fps={sample_fps}", "-vsync", "vfr", pattern])
        frames = sorted(Path(frame_dir).glob("frame-*.png"))
        if len(frames) < 5:
            return {"status": "FAIL", "reason": "insufficient_motion_frames", "participants": []}

        unique: dict[str, dict[str, Any]] = {}
        for turn in proof_turns:
            unique.setdefault(turn["participant_id"], turn)
        participants = []
        overall = "PASS"
        for participant_id, turn in unique.items():
            roi = _upper_body_roi(
                turn["coordinates"],
                source_width=source_width,
                source_height=source_height,
                output_width=output_width,
                output_height=output_height,
            )
            series = _roi_motion_series(frames, roi)
            score = _mean(series)
            if score < 0.08:
                status = "FAIL"
                reason = "upper_body_motion_near_static"
                overall = "FAIL"
            else:
                status = "PASS"
                reason = "motion_present_manual_naturalness_review_required"
            participants.append({
                "participant_id": participant_id,
                "display_name": turn["display_name"],
                "status": status,
                "reason": reason,
                "upper_body_motion_score": round(score, 4),
                "roi": list(roi),
            })
        return {
            "status": overall,
            "sample_fps": sample_fps,
            "participants": participants,
            "note": (
                "Automated gate detects near-static upper-body performance only. Blink quality, gesture anatomy, "
                "expression-context alignment, identity preservation and conversational naturalness remain mandatory human-review gates."
            ),
        }

def _frame_aligned_lipsync_attribution_qc(
    *,
    base_video_path: Path,
    output_video_path: Path,
    proof_turns: list[dict[str, Any]],
    source_width: int,
    source_height: int,
    sample_fps: int = 10,
) -> dict[str, Any]:
    base_width, base_height = _probe_video_geometry(base_video_path)
    out_width, out_height = _probe_video_geometry(output_video_path)
    if (base_width, base_height) != (out_width, out_height):
        return {
            "status": "WARN",
            "reason": "base_output_geometry_mismatch",
            "segments": [],
        }

    def extract_frames(video_path: Path, frame_dir: str) -> list[Path]:
        pattern = str(Path(frame_dir) / "frame-%06d.png")
        _run([
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", f"fps={sample_fps}", "-vsync", "vfr", pattern,
        ])
        return sorted(Path(frame_dir).glob("frame-*.png"))

    with tempfile.TemporaryDirectory(prefix="df_next3_attr_base_") as base_dir, tempfile.TemporaryDirectory(prefix="df_next3_attr_out_") as out_dir:
        base_frames = extract_frames(base_video_path, base_dir)
        out_frames = extract_frames(output_video_path, out_dir)
        frame_count = min(len(base_frames), len(out_frames))
        if frame_count < 4:
            return {"status": "FAIL", "reason": "insufficient_frame_aligned_frames", "segments": []}
        base_frames = base_frames[:frame_count]
        out_frames = out_frames[:frame_count]

        rois = {
            turn["participant_id"]: _mouth_roi(
                turn["source_image_coordinates"],
                source_width=source_width,
                source_height=source_height,
                output_width=out_width,
                output_height=out_height,
            )
            for turn in proof_turns
        }

        def aligned_diff_series(
            base_paths: list[Path],
            out_paths: list[Path],
            roi: tuple[int, int, int, int],
        ) -> list[float]:
            values: list[float] = []
            for base_path, out_path in zip(base_paths, out_paths):
                base_img = Image.open(base_path).convert("L").crop(roi)
                out_img = Image.open(out_path).convert("L").crop(roi)
                diff = ImageChops.difference(out_img, base_img)
                values.append(float(ImageStat.Stat(diff).mean[0]))
            return values

        overall = "PASS"
        segment_results: list[dict[str, Any]] = []
        for turn in proof_turns:
            start = max(0.0, float(turn["start_time"]) + 0.20)
            end = max(start, float(turn["end_time"]) - 0.20)
            start_index = max(0, int(math.floor(start * sample_fps)))
            end_index = min(frame_count, int(math.ceil(end * sample_fps)) + 1)
            base_segment = base_frames[start_index:end_index]
            out_segment = out_frames[start_index:end_index]

            metrics: list[dict[str, Any]] = []
            for participant_id, roi in rois.items():
                series = aligned_diff_series(base_segment, out_segment, roi)
                mean_diff = _mean(series)
                p90_diff = 0.0
                if series:
                    ordered = sorted(series)
                    p90_diff = ordered[min(len(ordered) - 1, int(round(0.90 * (len(ordered) - 1))))]
                metrics.append({
                    "participant_id": participant_id,
                    "mean_base_output_mouth_diff": round(mean_diff, 4),
                    "p90_base_output_mouth_diff": round(p90_diff, 4),
                    "roi": list(roi),
                })

            intended_id = turn["participant_id"]
            intended = next(item for item in metrics if item["participant_id"] == intended_id)
            others = [item for item in metrics if item["participant_id"] != intended_id]
            max_other = max(
                others,
                key=lambda item: float(item["mean_base_output_mouth_diff"]),
                default={"mean_base_output_mouth_diff": 0.0},
            )
            intended_score = float(intended["mean_base_output_mouth_diff"])
            other_score = float(max_other["mean_base_output_mouth_diff"])
            ratio = other_score / intended_score if intended_score > 1e-9 else 999.0

            if intended_score < 0.35:
                status = "WARN"
                reason = "intended_base_output_change_low"
            elif ratio >= 0.75 and other_score >= 0.50:
                status = "FAIL"
                reason = "non_speaker_base_output_change_too_high"
            elif ratio >= 0.45:
                status = "WARN"
                reason = "speaker_attribution_borderline"
            else:
                status = "PASS"
                reason = "speaker_specific_frame_aligned_change_detected"

            if status == "FAIL":
                overall = "FAIL"
            elif status == "WARN" and overall != "FAIL":
                overall = "WARN"

            segment_results.append({
                "sequence_no": turn["sequence_no"],
                "participant_id": intended_id,
                "display_name": turn["display_name"],
                "status": status,
                "reason": reason,
                "intended_mean_diff": round(intended_score, 4),
                "max_non_speaker_mean_diff": round(other_score, 4),
                "non_speaker_to_intended_ratio": round(ratio, 4),
                "participants": metrics,
            })

        return {
            "status": overall,
            "sample_fps": sample_fps,
            "method": "frame_aligned_output_minus_pre_lipsync_base_mouth_region",
            "segments": segment_results,
            "policy": {
                "pass": "frame-aligned changes are concentrated on the intended speaker",
                "warn": "attribution is inconclusive and requires human review",
                "fail": "frame-aligned changes are materially present on the non-speaker",
            },
        }


def _differential_active_speaker_qc(
    *,
    base_video_path: Path,
    output_video_path: Path,
    proof_turns: list[dict[str, Any]],
    source_width: int,
    source_height: int,
    sample_fps: int = 10,
) -> dict[str, Any]:
    base_width, base_height = _probe_video_geometry(base_video_path)
    out_width, out_height = _probe_video_geometry(output_video_path)
    if (base_width, base_height) != (out_width, out_height):
        return {
            "status": "WARN",
            "reason": "base_output_geometry_mismatch",
            "base_geometry": [base_width, base_height],
            "output_geometry": [out_width, out_height],
            "segments": [],
        }

    def extract_frames(video_path: Path, frame_dir: str) -> list[Path]:
        pattern = str(Path(frame_dir) / "frame-%06d.png")
        _run([
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", f"fps={sample_fps}", "-vsync", "vfr", pattern,
        ])
        return sorted(Path(frame_dir).glob("frame-*.png"))

    with tempfile.TemporaryDirectory(prefix="df_next3_base_qc_") as base_dir, tempfile.TemporaryDirectory(prefix="df_next3_out_qc_") as out_dir:
        base_frames = extract_frames(base_video_path, base_dir)
        out_frames = extract_frames(output_video_path, out_dir)
        frame_count = min(len(base_frames), len(out_frames))
        if frame_count < 4:
            return {"status": "FAIL", "reason": "insufficient_differential_frames", "segments": []}
        base_frames = base_frames[:frame_count]
        out_frames = out_frames[:frame_count]

        rois = {
            turn["participant_id"]: _mouth_roi(
                turn["source_image_coordinates"],
                source_width=source_width,
                source_height=source_height,
                output_width=out_width,
                output_height=out_height,
            )
            for turn in proof_turns
        }

        overall = "PASS"
        segment_results: list[dict[str, Any]] = []
        for turn in proof_turns:
            start = max(0.0, float(turn["start_time"]) + 0.20)
            end = max(start, float(turn["end_time"]) - 0.20)
            start_index = max(0, int(math.floor(start * sample_fps)))
            end_index = min(frame_count, int(math.ceil(end * sample_fps)) + 1)
            base_segment = base_frames[start_index:end_index]
            out_segment = out_frames[start_index:end_index]

            if len(base_segment) < 4 or len(out_segment) < 4:
                status = "WARN"
                reason = "insufficient_segment_frames"
                participant_metrics = []
            else:
                participant_metrics = []
                for participant_id, roi in rois.items():
                    base_score = _mean(_roi_motion_series(base_segment, roi))
                    out_score = _mean(_roi_motion_series(out_segment, roi))
                    delta = out_score - base_score
                    participant_metrics.append({
                        "participant_id": participant_id,
                        "base_motion_score": round(base_score, 4),
                        "output_motion_score": round(out_score, 4),
                        "lipsync_added_motion": round(delta, 4),
                        "roi": list(roi),
                    })

                intended_id = turn["participant_id"]
                intended = next(item for item in participant_metrics if item["participant_id"] == intended_id)
                others = [item for item in participant_metrics if item["participant_id"] != intended_id]
                max_other = max(
                    others,
                    key=lambda item: float(item["lipsync_added_motion"]),
                    default={"lipsync_added_motion": 0.0},
                )
                intended_delta = float(intended["lipsync_added_motion"])
                non_speaker_delta = max(0.0, float(max_other["lipsync_added_motion"]))

                if intended_delta < 0.25:
                    status = "WARN"
                    reason = "intended_lipsync_delta_low"
                elif non_speaker_delta > max(0.35, intended_delta * 0.45):
                    status = "FAIL"
                    reason = "non_speaker_received_excess_lipsync_motion"
                else:
                    status = "PASS"
                    reason = "speaker_specific_lipsync_delta_detected"

            if status == "FAIL":
                overall = "FAIL"
            elif status == "WARN" and overall != "FAIL":
                overall = "WARN"

            segment_results.append({
                "sequence_no": turn["sequence_no"],
                "participant_id": turn["participant_id"],
                "display_name": turn["display_name"],
                "status": status,
                "reason": reason,
                "participants": participant_metrics,
            })

        return {
            "status": overall,
            "sample_fps": sample_fps,
            "method": "output_mouth_motion_minus_pre_lipsync_motion_baseline",
            "segments": segment_results,
            "policy": {
                "pass": "intended speaker gains lip-sync-specific mouth motion while listeners remain near their natural-motion baseline",
                "warn": "automated evidence is insufficient; human review required",
                "fail": "non-speaker gains material mouth motion attributable to lip-sync",
            },
        }


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



def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except Exception:
        number = float(default)
    return max(low, min(high, number))


async def _full_scene_performance_plan(
    *,
    scene_title: str,
    scene_summary: str,
    scene_direction: dict[str, Any],
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    system = (
        "You are desifaces Performance Director for a multi-person shared-scene conversation. "
        "Plan one continuous, photorealistic human performance across the entire conversation. "
        "Interpret emotion from the actual dialogue, explicit emotion_code, scene context, prior/next turns, and relationship dynamics. "
        "Expressions must be context-specific and restrained: happy, sad, angry, anxious, relieved, proud, affectionate, skeptical, "
        "surprised, frustrated, calm and all other human emotions must only appear when the dialogue supports them. "
        "The current speaker should have natural gaze, blinking, facial micro-expression, subtle head/body movement and occasional motivated gestures. "
        "Every listener must remain alive and reactive without appearing to speak. Avoid mechanical blink counts, exact choreographed gesture timestamps, "
        "generic smiles, constant nodding, repetitive hand motion, exaggerated acting, frozen poses, identity drift, warped hands or camera jumps. "
        "This plan drives a PRE-LIPSYNC motion plate, so mouths must remain non-speaking apart from subtle non-speech expression. "
        "Preserve identity, clothing, body shape, seating, background and left-right continuity. Return valid JSON only."
    )
    required_schema = {
        "scene_emotional_arc": "string",
        "opening_seconds": "0.3-1.2",
        "closing_seconds": "0.5-1.5",
        "turns": [
            {
                "sequence_no": 1,
                "speaker_participant_id": "uuid",
                "speaker_name": "string",
                "primary_emotion": "string",
                "secondary_emotion": "string|null",
                "intensity": "0.0-1.0",
                "expression_trajectory": ["string"],
                "speaker_gaze": "string",
                "speaker_blinks": "natural intent, not an exact count",
                "speaker_head_motion": "string",
                "speaker_body_motion": "string",
                "speaker_gesture": "string",
                "speaker_microexpressions": ["string"],
                "listener_emotion": "string",
                "listener_reaction": "string",
                "listener_gaze": "string",
                "listener_body_motion": "string",
                "listener_mouth": "silent neutral",
                "handoff_pause_seconds": "0.25-1.0"
            }
        ],
        "negative_constraints": ["string"],
    }
    payload = {
        "scene_title": scene_title,
        "scene_summary": scene_summary,
        "scene_direction": scene_direction,
        "turns": turns,
        "required_schema": required_schema,
    }
    user_message = (
        "Create the full-scene performance plan. Keep the acting natural and context-specific. "
        "Choose a short opening presence, a context-aware handoff pause after each dialogue turn, and a closing reaction tail. "
        "Do not change dialogue order, speaker identity, or spoken text. Return JSON only.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_message}]

    openai_key = _clean(os.getenv("OPENAI_API_KEY"))
    azure_key = _clean(os.getenv("AZURE_OPENAI_KEY"))
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        if openai_key:
            base = _clean(os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
            model = _clean(os.getenv("DF_PERFORMANCE_DIRECTOR_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini")
            response = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                    "messages": messages,
                },
            )
            if response.status_code != 200:
                raise RuntimeError(f"FULL_PERFORMANCE_DIRECTOR_OPENAI_FAILED:{response.status_code}:{response.text[:1600]}")
            body = response.json()
            content = _clean(body["choices"][0]["message"]["content"])
            provider_meta = {"provider": "openai", "model": model}
        elif azure_key:
            endpoint = _clean(os.getenv("AZURE_OPENAI_ENDPOINT")).rstrip("/")
            deployment = _clean(os.getenv("AZURE_OPENAI_DEPLOYMENT"))
            api_version = _clean(os.getenv("AZURE_OPENAI_API_VERSION") or "2024-10-21")
            if not endpoint or not deployment:
                raise RuntimeError("FULL_PERFORMANCE_DIRECTOR_AZURE_CONFIG_INCOMPLETE")
            response = await client.post(
                f"{endpoint}/openai/deployments/{deployment}/chat/completions",
                params={"api-version": api_version},
                headers={"api-key": azure_key, "Content-Type": "application/json"},
                json={
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                    "messages": messages,
                },
            )
            if response.status_code != 200:
                raise RuntimeError(f"FULL_PERFORMANCE_DIRECTOR_AZURE_FAILED:{response.status_code}:{response.text[:1600]}")
            body = response.json()
            content = _clean(body["choices"][0]["message"]["content"])
            provider_meta = {"provider": "azure_openai", "deployment": deployment}
        else:
            raise RuntimeError("FULL_PERFORMANCE_DIRECTOR_LLM_NOT_CONFIGURED")

    plan = _json_from_model_content(content)
    plan_turns = list(plan.get("turns") or [])
    if len(plan_turns) != len(turns):
        raise RuntimeError(
            f"FULL_PERFORMANCE_DIRECTOR_TURN_COUNT_MISMATCH:expected={len(turns)}:actual={len(plan_turns)}"
        )

    expected = [(int(item["sequence_no"]), str(item["speaker_participant_id"])) for item in turns]
    actual = [
        (int(item.get("sequence_no") or 0), str(item.get("speaker_participant_id") or ""))
        for item in plan_turns
    ]
    if actual != expected:
        raise RuntimeError(f"FULL_PERFORMANCE_DIRECTOR_LINEAGE_MISMATCH:expected={expected}:actual={actual}")

    plan["opening_seconds"] = round(_clamp(plan.get("opening_seconds"), 0.3, 1.2, 0.55), 3)
    plan["closing_seconds"] = round(_clamp(plan.get("closing_seconds"), 0.5, 1.5, 0.8), 3)
    normalized_turns: list[dict[str, Any]] = []
    for source, directed in zip(turns, plan_turns):
        item = dict(directed)
        item["sequence_no"] = int(source["sequence_no"])
        item["speaker_participant_id"] = str(source["speaker_participant_id"])
        item["speaker_name"] = source["speaker_name"]
        item["handoff_pause_seconds"] = round(
            _clamp(item.get("handoff_pause_seconds"), 0.25, 1.0, 0.45),
            3,
        )
        normalized_turns.append(item)
    plan["turns"] = normalized_turns
    plan["llm"] = provider_meta
    return plan


def _build_full_timeline(
    *,
    turns: list[dict[str, Any]],
    durations: list[float],
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], float]:
    if len(turns) != len(durations):
        raise RuntimeError("FULL_TIMELINE_DURATION_COUNT_MISMATCH")
    directed = list(plan.get("turns") or [])
    if len(directed) != len(turns):
        raise RuntimeError("FULL_TIMELINE_PLAN_COUNT_MISMATCH")

    cursor = float(plan["opening_seconds"])
    timeline: list[dict[str, Any]] = []
    for idx, (turn, duration, direction) in enumerate(zip(turns, durations, directed)):
        start = round(cursor, 3)
        end = round(start + float(duration), 3)
        pause_after = (
            float(plan["closing_seconds"])
            if idx == len(turns) - 1
            else float(direction["handoff_pause_seconds"])
        )
        pause_end = round(end + pause_after, 3)
        timeline.append(
            {
                **turn,
                "duration_seconds": round(float(duration), 3),
                "start_time": start,
                "end_time": end,
                "pause_after_seconds": round(pause_after, 3),
                "pause_end_time": pause_end,
                "performance_direction": direction,
            }
        )
        cursor = pause_end
    return timeline, round(cursor, 3)


def _pack_motion_chunks(
    timeline: list[dict[str, Any]],
    *,
    total_duration: float,
    max_chunk_seconds: float = 14.0,
) -> list[dict[str, Any]]:
    if not timeline:
        raise RuntimeError("FULL_PERFORMANCE_NO_TIMELINE")

    total = float(total_duration)
    max_len = max(3.0, float(max_chunk_seconds))
    chunks: list[dict[str, Any]] = []
    cursor = 0.0

    natural_boundaries = sorted(
        {
            round(float(item["pause_end_time"]), 3)
            for item in timeline
            if 0.0 < float(item["pause_end_time"]) < total
        }
    )

    while cursor < total - 0.001:
        hard_end = min(total, cursor + max_len)

        # Prefer a conversational handoff boundary when one exists near the end
        # of the provider window. If a single dialogue turn itself is longer than
        # the provider window, fall back to a deterministic mid-turn slice.
        candidates = [
            boundary
            for boundary in natural_boundaries
            if cursor + 3.0 <= boundary <= hard_end + 1e-6
        ]
        if candidates:
            chunk_end = max(candidates)
            # Avoid creating a tiny trailing chunk solely because a natural
            # boundary happened just before the end of the scene.
            if total - chunk_end < 1.0 and hard_end >= total - 1e-6:
                chunk_end = total
        else:
            chunk_end = hard_end

        if chunk_end <= cursor + 0.05:
            raise RuntimeError(
                f"FULL_PERFORMANCE_CHUNKING_STALLED:start={cursor}:end={chunk_end}:total={total}"
            )

        slices: list[dict[str, Any]] = []
        for turn in timeline:
            turn_window_start = float(turn["start_time"])
            turn_window_end = float(turn["pause_end_time"])
            overlap_start = max(cursor, turn_window_start)
            overlap_end = min(chunk_end, turn_window_end)
            if overlap_end <= overlap_start + 1e-6:
                continue

            speech_start = max(overlap_start, float(turn["start_time"]))
            speech_end = min(overlap_end, float(turn["end_time"]))
            handoff_start = max(overlap_start, float(turn["end_time"]))
            handoff_end = min(overlap_end, float(turn["pause_end_time"]))

            item = dict(turn)
            item.update(
                {
                    "slice_start_time": round(overlap_start, 3),
                    "slice_end_time": round(overlap_end, 3),
                    "speech_slice_start_time": round(speech_start, 3),
                    "speech_slice_end_time": round(max(speech_start, speech_end), 3),
                    "handoff_slice_start_time": round(handoff_start, 3),
                    "handoff_slice_end_time": round(max(handoff_start, handoff_end), 3),
                    "continues_from_previous_chunk": bool(float(turn["start_time"]) < cursor - 1e-6),
                    "continues_into_next_chunk": bool(float(turn["pause_end_time"]) > chunk_end + 1e-6),
                }
            )
            slices.append(item)

        chunks.append(
            {
                "chunk_no": len(chunks) + 1,
                "start_time": round(cursor, 3),
                "end_time": round(chunk_end, 3),
                "duration_seconds": round(chunk_end - cursor, 3),
                "turns": slices,
            }
        )
        cursor = chunk_end

    if not chunks:
        raise RuntimeError("FULL_PERFORMANCE_NO_MOTION_CHUNKS")

    covered = sum(float(item["duration_seconds"]) for item in chunks)
    if abs(covered - total) > 0.05:
        raise RuntimeError(
            f"FULL_PERFORMANCE_CHUNK_COVERAGE_MISMATCH:covered={covered}:total={total}"
        )
    if any(float(item["duration_seconds"]) > max_len + 0.05 for item in chunks):
        raise RuntimeError("FULL_PERFORMANCE_CHUNK_EXCEEDS_PROVIDER_LIMIT")
    return chunks


def _chunk_motion_prompt(
    *,
    scene_title: str,
    chunk: dict[str, Any],
    plan: dict[str, Any],
) -> str:
    start = float(chunk["start_time"])
    end = float(chunk["end_time"])
    lines = [
        f"Continuous photorealistic two-person conversation performance for scene '{scene_title}'.",
        "PRE-LIPSYNC MOTION PLATE: neither person visibly articulates words; mouths remain naturally non-speaking.",
        "Preserve both identities, seating, wardrobe, hands, background, lighting and left-right screen position.",
        "Use subtle natural blinking, gaze, facial micro-expressions, head motion, body posture changes and motivated gestures.",
        "The listener stays alive and contextually reactive without appearing to speak.",
        "No camera cuts, no morphing, no identity swap, no generic smiling, no repetitive nodding, no exaggerated gestures.",
    ]

    opening_seconds = float(plan.get("opening_seconds") or 0.0)
    opening_overlap_start = max(start, 0.0)
    opening_overlap_end = min(end, opening_seconds)
    if opening_overlap_end > opening_overlap_start + 0.05:
        lines.append(
            f"{opening_overlap_start - start:.2f}-{opening_overlap_end - start:.2f}s opening presence: "
            "natural settling, eye contact and subtle breathing; no speech articulation."
        )

    for turn in chunk["turns"]:
        d = dict(turn["performance_direction"])
        speech_abs_start = float(turn["speech_slice_start_time"])
        speech_abs_end = float(turn["speech_slice_end_time"])
        if speech_abs_end > speech_abs_start + 0.05:
            rel_start = speech_abs_start - start
            rel_end = speech_abs_end - start
            continuity = (
                " This is a continuation of the same speaker performance from the previous motion chunk; "
                "preserve pose, gaze, expression and gesture continuity."
                if turn.get("continues_from_previous_chunk")
                else ""
            )
            lines.append(
                f"{rel_start:.2f}-{rel_end:.2f}s speaker {turn['speaker_name']}: "
                f"emotion {d.get('primary_emotion')}"
                + (f" with {d.get('secondary_emotion')}" if d.get("secondary_emotion") else "")
                + f", intensity {d.get('intensity')}. "
                f"Gaze: {d.get('speaker_gaze')}. Blinking: {d.get('speaker_blinks')}. "
                f"Head: {d.get('speaker_head_motion')}. Body: {d.get('speaker_body_motion')}. "
                f"Gesture: {d.get('speaker_gesture')}. Microexpressions: {d.get('speaker_microexpressions')}. "
                f"Listener: {d.get('listener_emotion')}; reaction {d.get('listener_reaction')}; "
                f"gaze {d.get('listener_gaze')}; body {d.get('listener_body_motion')}; mouth silent."
                + continuity
            )

        handoff_abs_start = float(turn["handoff_slice_start_time"])
        handoff_abs_end = float(turn["handoff_slice_end_time"])
        if handoff_abs_end > handoff_abs_start + 0.05:
            lines.append(
                f"{handoff_abs_start - start:.2f}-{handoff_abs_end - start:.2f}s conversational handoff: "
                "both remain naturally present; the prior speaker relaxes after the line while the listener reacts "
                "before the next turn. No speech articulation."
            )

        if turn.get("continues_into_next_chunk"):
            lines.append(
                "At the end of this chunk, hold a natural in-motion continuity pose suitable for seamless continuation "
                "from this exact frame in the next chunk; do not reset expression, seating or body orientation."
            )

    return " ".join(lines)[:3600]


def _concat_h264_segments(segment_paths: list[Path], output_path: Path) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
        list_path = Path(handle.name)
        for path in segment_paths:
            escaped = str(path.resolve()).replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")
    try:
        _run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_path), "-c", "copy", str(output_path),
        ])
    finally:
        list_path.unlink(missing_ok=True)


def _extract_last_frame(video_path: Path, output_path: Path) -> None:
    _run([
        "ffmpeg", "-y", "-sseof", "-0.08", "-i", str(video_path),
        "-frames:v", "1", str(output_path),
    ])


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--stage-run-id", required=True)
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--sync-provider-job-id", default="")
    parser.add_argument("--motion-provider-job-id", default="")
    parser.add_argument("--motion-model", default="")
    parser.add_argument("--reuse-latest-motion-blob", action="store_true")
    args = parser.parse_args()

    workflow_id = UUID(args.workflow_id)
    stage_run_id = UUID(args.stage_run_id)
    fps = max(24, min(30, int(args.fps)))

    sync_key = _clean(os.getenv("SYNC_API_KEY"))
    sync_base = _clean(os.getenv("SYNC_API_BASE_URL") or "https://api.sync.so").rstrip("/")
    azure_conn = _clean(os.getenv("AZURE_STORAGE_CONNECTION_STRING"))
    output_container = _clean(os.getenv("AZURE_VIDEO_OUTPUT_CONTAINER") or "video-output")
    fal_key = _clean(os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY"))
    if not sync_key:
        raise RuntimeError("SYNC_API_KEY_MISSING")
    if not azure_conn:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING_MISSING")
    if not fal_key and not args.reuse_latest_motion_blob:
        raise RuntimeError("FAL_KEY_MISSING")

    conn = await asyncpg.connect(_db_dsn())
    try:
        stage = await conn.fetchrow(
            """
            select s.stage_run_id,s.scene_id,s.metadata_json,
                   w.workflow_id,w.account_id,w.owner_user_id,w.project_id,
                   sc.title as scene_title,sc.summary as scene_summary,sc.direction_json as scene_direction
            from public.v3_studio_stage_runs s
            join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
            join public.v3_scenes sc on sc.scene_id=s.scene_id
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
                   dt.emotion_code,to_jsonb(dt) as turn_json,
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
        dialogue_context: list[dict[str, Any]] = []
        for row in selected:
            turn_json = _dict(row["turn_json"])
            audio_meta = _dict(row["meta_json"])
            spoken_text, text_source = _extract_spoken_text(turn_json, audio_meta)
            dialogue_context.append(
                {
                    "sequence_no": int(row["sequence_no"]),
                    "speaker_participant_id": str(row["speaker_participant_id"]),
                    "speaker_name": _clean(row["display_name"]),
                    "emotion_code": _clean(row["emotion_code"]) or None,
                    "spoken_text": spoken_text,
                    "text_source": text_source,
                }
            )
    finally:
        await conn.close()

    azure = AzureBlobService(azure_conn)
    image_container, image_blob = _blob_location(image_row)
    image_url = azure.sign_read_url(image_container, image_blob, 3600)

    manifest_turns: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="df_next3_performance_proof_") as td:
        root = Path(td)
        image_path = root / "source-image"
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as download_client:
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
        motion_duration = int(math.ceil(total_duration))
        if motion_duration < 3 or motion_duration > 15:
            raise RuntimeError(
                f"PERFORMANCE_PROOF_DURATION_UNSUPPORTED:{total_duration}:requires_3_to_15_seconds"
            )

        cursor = 0.0
        timed_dialogue_context: list[dict[str, Any]] = []
        for item, duration in zip(dialogue_context, durations):
            start = round(cursor, 3)
            end = round(cursor + duration, 3)
            timed_dialogue_context.append({**item, "start_time": start, "end_time": end})
            cursor = end

        performance_plan = await _performance_director_plan(
            scene_title=_clean(stage["scene_title"]),
            scene_summary=_clean(stage["scene_summary"]),
            scene_direction=_dict(stage["scene_direction"]),
            turns=timed_dialogue_context,
        )

        motion_model = _clean(
            args.motion_model
            or os.getenv("DF_NEXT3_PERFORMANCE_MODEL")
            or (
                "fal-ai/kling-video/v3/pro/image-to-video"
                if args.quality_profile == "premium"
                else os.getenv("FAL_KLING_I2V_MODEL")
            )
            or "fal-ai/kling-video/v3/standard/image-to-video"
        )
        motion_prompt = _clean(performance_plan["continuous_motion_prompt"])
        negative_prompt = (
            "visible speech articulation before lipsync, both people talking, repeated mouth flapping, "
            "frozen mannequin pose, identity drift, face morphing, duplicate person, warped hands, extra fingers, "
            "age progression, aging, de-aging, older face, younger face, new wrinkles, facial proportion drift, "
            "skin texture drift, temporal flicker, visual noise, film grain, compression noise, crawling texture, "
            "exaggerated gestures, constant nodding, constant smiling, camera jump, scene cut, clothing change, background change"
        )
        motion_payload = {
            "prompt": motion_prompt,
            "start_image_url": image_url,
            "duration": str(motion_duration),
            "generate_audio": False,
            "shot_type": "customize",
            "negative_prompt": negative_prompt,
        }

        print("PERFORMANCE_DIRECTOR_PLAN=" + json.dumps(performance_plan, ensure_ascii=False))
        print(f"PERFORMANCE_MOTION_MODEL={motion_model}")

        motion_video = root / "performance-motion.mp4"
        if args.reuse_latest_motion_blob:
            prefix = f"v3/qa/shared-scene-performance/{workflow_id}/{stage_run_id}/"
            bsc = BlobServiceClient.from_connection_string(azure_conn)
            cc = bsc.get_container_client(output_container)
            candidates = [
                blob for blob in cc.list_blobs(name_starts_with=prefix)
                if str(blob.name).endswith("/performance-motion.mp4")
            ]
            if not candidates:
                raise RuntimeError("NO_EXISTING_PERFORMANCE_MOTION_BLOB")
            candidates.sort(key=lambda blob: blob.last_modified, reverse=True)
            selected_motion_blob = candidates[0]
            motion_video.write_bytes(cc.download_blob(selected_motion_blob.name).readall())
            source_blob = str(selected_motion_blob.name)
            source_video_url = azure.sign_read_url(output_container, source_blob, 3600)
            durable_motion_url = azure.sign_read_url(output_container, source_blob, 15 * 24 * 3600)
            motion_job_id = "azure-reuse:" + source_blob
            motion_result = {
                "reused_existing_motion_blob": source_blob,
                "last_modified": selected_motion_blob.last_modified.isoformat(),
            }
            print(f"PERFORMANCE_MOTION_BLOB_REUSE={source_blob}")
        else:
            motion_job_id, motion_url, motion_result = await _fal_generate_motion(
                model_id=motion_model,
                fal_key=fal_key,
                payload=motion_payload,
                existing_request_id=args.motion_provider_job_id,
            )
            async with httpx.AsyncClient(timeout=180, follow_redirects=True) as motion_client:
                await _download(motion_client, motion_url, motion_video)

            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            base_path = f"v3/qa/shared-scene-performance/{workflow_id}/{stage_run_id}/{stamp}"
            source_blob = f"{base_path}/performance-motion.mp4"
            azure.upload_file(output_container, source_blob, str(motion_video), "video/mp4")
            source_video_url = azure.sign_read_url(output_container, source_blob, 3600)
            durable_motion_url = azure.sign_read_url(output_container, source_blob, 15 * 24 * 3600)

        motion_info = _probe_video_info(motion_video)
        dims_for_ratio = _dict(stage_meta.get("shared_scene_dimensions"))
        source_ratio = float(dims_for_ratio["width"]) / float(dims_for_ratio["height"])
        motion_aspect = float(motion_info["width"]) / float(motion_info["height"])
        if abs(motion_aspect - source_ratio) > 0.08:
            raise RuntimeError(
                "PERFORMANCE_MOTION_ASPECT_RATIO_DRIFT:"
                f"source_ratio={source_ratio:.6f}:"
                f"video={motion_info['width']}x{motion_info['height']}"
            )
        if float(motion_info["duration"]) + 0.10 < total_duration:
            raise RuntimeError(
                "PERFORMANCE_MOTION_TOO_SHORT:"
                f"duration={motion_info['duration']}:required={total_duration}"
            )
        print(
            "PERFORMANCE_MOTION_VIDEO_INFO="
            f"{motion_info['width']}x{motion_info['height']}"
            f" fps={motion_info['fps']:.6f}"
            f" duration={motion_info['duration']:.3f}"
            f" frames={motion_info['frame_count']}"
        )

        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        base_path = f"v3/qa/shared-scene-performance-sync/{workflow_id}/{stage_run_id}/{stamp}"

        segments: list[dict[str, Any]] = []
        inputs: list[dict[str, Any]] = [{"type": "video", "url": source_video_url}]
        cursor = 0.0
        for index, (row, audio_url, duration) in enumerate(
            zip(selected, audio_urls, durations), start=1
        ):
            ref_id = f"audio_{index}"
            source_coordinates = _speaker_coordinates(
                stage_meta,
                UUID(str(row["speaker_participant_id"])),
            )
            coordinates = _speaker_coordinates_for_video(
                stage_meta,
                UUID(str(row["speaker_participant_id"])),
                video_width=int(motion_info["width"]),
                video_height=int(motion_info["height"]),
            )
            start_time = round(cursor, 3)
            end_time = round(cursor + duration, 3)
            reference_time = min(
                max(start_time + 0.15, start_time),
                max(start_time, end_time - 0.05),
            )
            frame_number = max(
                0,
                min(
                    int(motion_info["frame_count"]) - 1,
                    int(round(reference_time * float(motion_info["fps"]))),
                ),
            )
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
                    "reference_time_seconds": round(reference_time, 3),
                    "source_image_coordinates": source_coordinates,
                    "coordinates": coordinates,
                    "spoken_text": dialogue_context[index - 1]["spoken_text"],
                    "emotion_code": dialogue_context[index - 1]["emotion_code"],
                    "performance_direction": performance_plan["turns"][index - 1],
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
        print(" NEXT3 TWO-TURN PERFORMANCE + SEGMENTS PROOF")
        print(" mutation=performance_provider_generation_plus_sync_generation_and_qa_blob_only")
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
                f"source_coords={turn['source_image_coordinates']} "
                f"video_coords={turn['coordinates']} "
                f"frame={turn['frame_number']} "
                f"start={turn['start_time']} end={turn['end_time']}"
            )
        print("============================================================")

        headers = {
            "x-api-key": sync_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            provider_job_id = _clean(args.sync_provider_job_id)
            if provider_job_id:
                print(f"SYNC_SEGMENTS_PROVIDER_JOB_REUSE={provider_job_id}")
            else:
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
            first_poll = True
            while time.monotonic() < deadline:
                if not first_poll:
                    await asyncio.sleep(8)
                first_poll = False
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

            raw_output_path = root / "segments-output-raw.mp4"
            await _download(client, output_url, raw_output_path)

        output_path = root / "segments-output.mp4"
        _run(
            [
                "ffmpeg", "-y", "-i", str(raw_output_path),
                "-t", f"{total_duration:.3f}",
                "-c", "copy",
                str(output_path),
            ]
        )

        output_blob = f"{base_path}/segments-output.mp4"
        azure.upload_file(output_container, output_blob, str(output_path), "video/mp4")
        durable_output_url = azure.sign_read_url(output_container, output_blob, 15 * 24 * 3600)

        dims = _dict(stage_meta.get("shared_scene_dimensions"))
        motion_qc = _motion_presence_qc(
            video_path=motion_video,
            proof_turns=manifest_turns,
            source_width=int(dims["width"]),
            source_height=int(dims["height"]),
        )
        qc = _active_speaker_qc(
            video_path=output_path,
            proof_turns=manifest_turns,
            source_width=int(dims["width"]),
            source_height=int(dims["height"]),
        )
        differential_qc = _differential_active_speaker_qc(
            base_video_path=motion_video,
            output_video_path=output_path,
            proof_turns=manifest_turns,
            source_width=int(dims["width"]),
            source_height=int(dims["height"]),
        )
        attribution_qc = _frame_aligned_lipsync_attribution_qc(
            base_video_path=motion_video,
            output_video_path=output_path,
            proof_turns=manifest_turns,
            source_width=int(dims["width"]),
            source_height=int(dims["height"]),
        )

        manifest = {
            "contract": "next3_shared_scene_two_turn_performance_proof_v1",
            "workflow_id": str(workflow_id),
            "stage_run_id": str(stage_run_id),
            "shared_scene_media_id": str(shared_media_id),
            "performance_director": performance_plan,
            "identity_references": identity_refs,
            "quality_profile": args.quality_profile,
            "max_motion_chunk_seconds": max(5.0, min(14.0, float(args.max_motion_chunk_seconds))),
            "performance_motion_provider": "fal",
            "performance_motion_model": motion_model,
            "performance_motion_provider_job_id": motion_job_id,
            "performance_motion_request": motion_payload,
            "performance_motion_result": motion_result,
            "performance_motion_qc": motion_qc,
            "performance_motion_storage_path": source_blob,
            "performance_motion_review_url": durable_motion_url,
            "provider": "sync3",
            "provider_model": "sync-3",
            "provider_job_id": provider_job_id,
            "fps": fps,
            "total_duration_seconds": total_duration,
            "turns": manifest_turns,
            "active_speaker_qc_raw_motion": qc,
            "active_speaker_qc_temporal_delta": differential_qc,
            "active_speaker_qc": attribution_qc,
            "human_proof_review": "REQUIRED_FOR_EXPRESSION_BLINKS_GAZE_GESTURES_HANDS_IDENTITY_AND_CONVERSATIONAL_NATURALNESS",
            "qa_storage_path": output_blob,
        }
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        manifest_blob = f"{base_path}/manifest.json"
        azure.upload_file(output_container, manifest_blob, str(manifest_path), "application/json")
        manifest_url = azure.sign_read_url(output_container, manifest_blob, 15 * 24 * 3600)

        print("============================================================")
        print("NEXT3_TWO_TURN_PERFORMANCE_PROVIDER=PASS")
        print(f"PERFORMANCE_MOTION_QC={motion_qc['status']}")
        for result in motion_qc["participants"]:
            print(
                "MOTION_QC "
                f"speaker={result['display_name']} status={result['status']} "
                f"score={result['upper_body_motion_score']} reason={result['reason']}"
            )
        print(f"ACTIVE_SPEAKER_RAW_MOTION_QC={qc['status']}")
        print(f"ACTIVE_SPEAKER_TEMPORAL_DELTA_QC={differential_qc['status']}")
        print(f"ACTIVE_SPEAKER_QC={attribution_qc['status']}")
        for result in attribution_qc["segments"]:
            print(
                "FRAME_ALIGNED_QC_SEGMENT "
                f"seq={result['sequence_no']} speaker={result['display_name']} "
                f"status={result['status']} reason={result['reason']} "
                f"target_diff={result['intended_mean_diff']} "
                f"non_speaker_diff={result['max_non_speaker_mean_diff']} "
                f"ratio={result['non_speaker_to_intended_ratio']} "
                f"participants={json.dumps(result['participants'], separators=(',', ':'))}"
            )
        print("HUMAN_QUALITY_REVIEW=REQUIRED")
        print("HUMAN_REVIEW_DIMENSIONS=context_specific_expression,blinks,gaze,head_motion,body_motion,gestures,hands,listener_reaction,identity,temporal_continuity")
        print(f"MOTION_BASE_URL={durable_motion_url}")
        print(f"OUTPUT_URL={durable_output_url}")
        print(f"MANIFEST_URL={manifest_url}")
        print("FULL_SEVEN_TURN_GENERATION=BLOCKED_UNTIL_ACTIVE_SPEAKER_QC_AND_PERFORMANCE_REVIEW_PASS")
        print("DATABASE_WRITE=NONE")
        print("PRODUCTION_TOUCH=NONE")
        print("============================================================")




async def _download_to_path(url: str, path: Path) -> None:
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        await _download(client, url, path)


def _identity_crop_box(
    *,
    stage_meta: dict[str, Any],
    participant_id: UUID,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    targets = _dict(stage_meta.get("speaker_targets"))
    target = _dict(targets.get(str(participant_id)))
    point = _dict(target.get("point"))
    box = _dict(target.get("box"))
    if box:
        cx = (float(box["x"]) + float(box["width"]) / 2.0) * width
        cy = (float(box["y"]) + float(box["height"]) / 2.0) * height
        half_w = max(float(box["width"]) * width * 0.85, width * 0.10)
        half_h = max(float(box["height"]) * height * 0.95, height * 0.15)
    elif point:
        cx = float(point["x"]) * width
        cy = float(point["y"]) * height
        half_w = width * 0.11
        half_h = height * 0.17
    else:
        raise RuntimeError(f"IDENTITY_REFERENCE_TARGET_MISSING:{participant_id}")

    left = max(0, int(round(cx - half_w)))
    top = max(0, int(round(cy - half_h)))
    right = min(width, int(round(cx + half_w)))
    bottom = min(height, int(round(cy + half_h)))
    if right - left < 96 or bottom - top < 96:
        raise RuntimeError(f"IDENTITY_REFERENCE_CROP_TOO_SMALL:{participant_id}")
    return left, top, right, bottom


async def _build_identity_elements(
    *,
    source_image_url: str,
    stage_meta: dict[str, Any],
    timeline: list[dict[str, Any]],
    azure: AzureBlobService,
    output_container: str,
    base_path: str,
    root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_path = root / "approved-group-photo.png"
    await _download_to_path(source_image_url, source_path)
    with Image.open(source_path) as source_image:
        source_rgb = source_image.convert("RGB")
        width, height = source_rgb.size
        ordered: list[tuple[str, str]] = []
        seen: set[str] = set()
        for turn in timeline:
            participant_id = str(turn["speaker_participant_id"])
            if participant_id not in seen:
                seen.add(participant_id)
                ordered.append((participant_id, str(turn["speaker_name"])))

        elements: list[dict[str, Any]] = []
        refs: dict[str, Any] = {}
        for index, (participant_id, display_name) in enumerate(ordered, start=1):
            crop_box = _identity_crop_box(
                stage_meta=stage_meta,
                participant_id=UUID(participant_id),
                width=width,
                height=height,
            )
            left, top, right, bottom = crop_box

            # Tight frontal crop anchors facial identity/apparent age.
            frontal_crop = source_rgb.crop(crop_box)
            frontal_path = root / f"identity-{index}-frontal.jpg"
            frontal_crop.save(frontal_path, format="JPEG", quality=96, subsampling=0)
            frontal_blob = f"{base_path}/identity/participant-{index}-frontal.jpg"
            azure.upload_file(output_container, frontal_blob, str(frontal_path), "image/jpeg")
            frontal_url = azure.sign_read_url(output_container, frontal_blob, 6 * 3600)

            # A wider same-source reference is required by Kling's image-set
            # element contract and also anchors hairstyle/shoulders/clothing
            # without introducing a second synthetic identity source.
            face_w = right - left
            face_h = bottom - top
            wide_box = (
                max(0, int(round(left - 0.45 * face_w))),
                max(0, int(round(top - 0.25 * face_h))),
                min(width, int(round(right + 0.45 * face_w))),
                min(height, int(round(bottom + 0.90 * face_h))),
            )
            wide_crop = source_rgb.crop(wide_box)
            reference_path = root / f"identity-{index}-reference.jpg"
            wide_crop.save(reference_path, format="JPEG", quality=96, subsampling=0)
            reference_blob = f"{base_path}/identity/participant-{index}-reference.jpg"
            azure.upload_file(output_container, reference_blob, str(reference_path), "image/jpeg")
            reference_url = azure.sign_read_url(output_container, reference_blob, 6 * 3600)

            elements.append({
                "frontal_image_url": frontal_url,
                "reference_image_urls": [reference_url],
            })
            refs[participant_id] = {
                "element_index": index,
                "display_name": display_name,
                "frontal_crop_box": list(crop_box),
                "reference_crop_box": list(wide_box),
                "frontal_storage_path": frontal_blob,
                "reference_storage_path": reference_blob,
            }
    return elements, refs


def _identity_prompt_prefix(identity_refs: dict[str, Any]) -> str:
    ordered = sorted(identity_refs.values(), key=lambda item: int(item["element_index"]))
    parts = []
    for item in ordered:
        parts.append(
            f"@Element{item['element_index']} is {item['display_name']}; preserve this person's exact identity, "
            "apparent age, facial proportions, skin texture, hairstyle and gender presentation throughout"
        )
    return ". ".join(parts) + "."


def _clean_motion_video(input_path: Path, output_path: Path, *, fps: int) -> None:
    # Conservative cleanup: temporal/spatial denoise plus light detail restoration.
    # No motion interpolation here: generated hands/faces are safer without synthetic optical-flow frames.
    _run([
        "ffmpeg", "-y", "-i", str(input_path),
        "-vf",
        f"hqdn3d=1.25:1.25:4.5:4.5,unsharp=5:5:0.22:3:3:0.0,fps={int(fps)}",
        "-an",
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "16",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ])


async def full_scene_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--stage-run-id", required=True)
    parser.add_argument("--phase", choices=["motion"], default="motion")
    parser.add_argument("--expected-turn-count", type=int, default=7)
    parser.add_argument("--motion-model", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--reuse-plan-run-id", default="")
    parser.add_argument("--max-motion-chunk-seconds", type=float, default=12.0)
    parser.add_argument("--quality-profile", choices=["standard", "premium"], default="premium")
    args = parser.parse_args()

    workflow_id = UUID(args.workflow_id)
    stage_run_id = UUID(args.stage_run_id)
    expected_turn_count = max(1, int(args.expected_turn_count))

    azure_conn = _clean(os.getenv("AZURE_STORAGE_CONNECTION_STRING"))
    output_container = _clean(os.getenv("AZURE_VIDEO_OUTPUT_CONTAINER") or "video-output")
    fal_key = _clean(os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY"))
    if not azure_conn:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING_MISSING")
    if not fal_key:
        raise RuntimeError("FAL_KEY_MISSING")

    conn = await asyncpg.connect(_db_dsn())
    try:
        stage = await conn.fetchrow(
            """
            select s.stage_run_id,s.scene_id,s.metadata_json,
                   w.workflow_id,w.account_id,w.owner_user_id,w.project_id,
                   sc.title as scene_title,sc.summary as scene_summary,sc.direction_json as scene_direction
            from public.v3_studio_stage_runs s
            join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
            join public.v3_scenes sc on sc.scene_id=s.scene_id
            where s.stage_run_id=$1 and w.workflow_id=$2
              and s.stage_type='fusion' and s.scope_type='scene'
            """,
            stage_run_id,
            workflow_id,
        )
        if not stage:
            raise RuntimeError("FULL_SCENE_STAGE_NOT_FOUND")
        stage_meta = _dict(stage["metadata_json"])
        if _clean(stage_meta.get("conversation_mode")).lower() != "shared_scene":
            raise RuntimeError("FULL_SCENE_REQUIRES_SHARED_SCENE_MODE")

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
            raise RuntimeError("FULL_SCENE_SHARED_IMAGE_NOT_ACTIVE")

        rows = await conn.fetch(
            """
            select dt.turn_id,dt.sequence_no,dt.speaker_participant_id,p.display_name,
                   dt.emotion_code,to_jsonb(dt) as turn_json,
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
        if len(rows) != expected_turn_count:
            raise RuntimeError(
                f"FULL_SCENE_APPROVED_TURN_COUNT_MISMATCH:expected={expected_turn_count}:actual={len(rows)}"
            )

        turns: list[dict[str, Any]] = []
        for row in rows:
            spoken_text, text_source = _extract_spoken_text(
                _dict(row["turn_json"]),
                _dict(row["meta_json"]),
            )
            turns.append(
                {
                    "sequence_no": int(row["sequence_no"]),
                    "dialogue_turn_id": str(row["turn_id"]),
                    "speaker_participant_id": str(row["speaker_participant_id"]),
                    "speaker_name": _clean(row["display_name"]),
                    "emotion_code": _clean(row["emotion_code"]) or None,
                    "spoken_text": spoken_text,
                    "text_source": text_source,
                    "audio_media_id": str(row["audio_media_id"]),
                    "_row": row,
                }
            )
    finally:
        await conn.close()

    azure = AzureBlobService(azure_conn)
    bsc = BlobServiceClient.from_connection_string(azure_conn)
    cc = bsc.get_container_client(output_container)
    image_container, image_blob = _blob_location(image_row)
    image_url = azure.sign_read_url(image_container, image_blob, 4 * 3600)

    run_id = _clean(args.run_id) or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base_path = f"v3/qa/shared-scene-full-performance/{workflow_id}/{stage_run_id}/{run_id}"
    plan_blob = f"{base_path}/plan.json"

    print("============================================================")
    print(" NEXT3 FULL-SCENE PERFORMANCE PIPELINE")
    print(" phase=motion")
    print(" environment=DEV_ONLY")
    print(" database_write=NONE")
    print(" production_touch=NONE")
    print(f"workflow_id={workflow_id}")
    print(f"stage_run_id={stage_run_id}")
    print(f"shared_scene_media_id={shared_media_id}")
    print(f"turn_count={len(turns)}")
    print(f"run_id={run_id}")
    print("============================================================")

    with tempfile.TemporaryDirectory(prefix="df_next3_full_scene_") as td:
        root = Path(td)

        durations: list[float] = []
        audio_urls: list[str] = []
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            for index, turn in enumerate(turns, start=1):
                row = turn["_row"]
                container, blob = _blob_location(row)
                url = azure.sign_read_url(container, blob, 4 * 3600)
                audio_urls.append(url)
                duration = (
                    float(row["duration_ms"]) / 1000.0
                    if row["duration_ms"] is not None and int(row["duration_ms"]) > 0
                    else 0.0
                )
                if duration <= 0:
                    local_audio = root / f"audio-{index}.bin"
                    await _download(client, url, local_audio)
                    duration = _probe_duration(local_audio)
                durations.append(max(0.25, round(float(duration), 3)))

        reusable_plan = None
        plan_client = cc.get_blob_client(plan_blob)
        source_plan_blob = plan_blob
        if args.reuse_plan_run_id:
            source_plan_blob = (
                f"v3/qa/shared-scene-full-performance/{workflow_id}/{stage_run_id}/"
                f"{_clean(args.reuse_plan_run_id)}/plan.json"
            )
        source_plan_client = cc.get_blob_client(source_plan_blob)

        if (args.run_id and plan_client.exists()) or (args.reuse_plan_run_id and source_plan_client.exists()):
            selected_plan_client = plan_client if (args.run_id and plan_client.exists()) else source_plan_client
            selected_plan_blob = plan_blob if selected_plan_client is plan_client else source_plan_blob
            reusable_plan = json.loads(selected_plan_client.download_blob().readall().decode("utf-8"))
            existing_lineage = [
                (int(item["sequence_no"]), str(item["dialogue_turn_id"]))
                for item in list(reusable_plan.get("timeline") or [])
            ]
            current_lineage = [
                (int(item["sequence_no"]), str(item["dialogue_turn_id"]))
                for item in turns
            ]
            if existing_lineage != current_lineage:
                raise RuntimeError("FULL_SCENE_RESUME_LINEAGE_MISMATCH")
            performance_plan = dict(reusable_plan["performance_director"])
            timeline = list(reusable_plan["timeline"])
            total_duration = float(reusable_plan["planned_duration_seconds"])
            chunks = _pack_motion_chunks(
                timeline,
                total_duration=total_duration,
                max_chunk_seconds=max(5.0, min(14.0, float(args.max_motion_chunk_seconds))),
            )
            if selected_plan_blob != plan_blob:
                copied_plan = dict(reusable_plan)
                copied_plan["motion_chunks"] = chunks
                copied_plan["quality_profile"] = args.quality_profile
                copied_plan["reused_from_plan_blob"] = selected_plan_blob
                plan_local = root / "plan.json"
                plan_local.write_text(json.dumps(copied_plan, indent=2), encoding="utf-8")
                azure.upload_file(output_container, plan_blob, str(plan_local), "application/json")
            print(f"PERFORMANCE_PLAN_REUSE={selected_plan_blob}")
        else:
            performance_plan = await _full_scene_performance_plan(
                scene_title=_clean(stage["scene_title"]),
                scene_summary=_clean(stage["scene_summary"]),
                scene_direction=_dict(stage["scene_direction"]),
                turns=[
                    {
                        "sequence_no": item["sequence_no"],
                        "speaker_participant_id": item["speaker_participant_id"],
                        "speaker_name": item["speaker_name"],
                        "emotion_code": item["emotion_code"],
                        "spoken_text": item["spoken_text"],
                        "duration_seconds": duration,
                    }
                    for item, duration in zip(turns, durations)
                ],
            )
            timeline, total_duration = _build_full_timeline(
                turns=[
                    {k: v for k, v in item.items() if k != "_row"}
                    for item in turns
                ],
                durations=durations,
                plan=performance_plan,
            )
            chunks = _pack_motion_chunks(
                timeline,
                total_duration=total_duration,
                max_chunk_seconds=max(5.0, min(14.0, float(args.max_motion_chunk_seconds))),
            )
            plan_payload = {
                "contract": "next3_shared_scene_full_performance_plan_v1",
                "workflow_id": str(workflow_id),
                "stage_run_id": str(stage_run_id),
                "shared_scene_media_id": str(shared_media_id),
                "performance_director": performance_plan,
                "timeline": timeline,
                "planned_duration_seconds": total_duration,
                "motion_chunks": chunks,
            }
            plan_local = root / "plan.json"
            plan_local.write_text(json.dumps(plan_payload, indent=2), encoding="utf-8")
            azure.upload_file(output_container, plan_blob, str(plan_local), "application/json")
            print(f"PERFORMANCE_PLAN_CREATED={plan_blob}")

        print(
            "PERFORMANCE_TIMELINE="
            + json.dumps(
                [
                    {
                        "seq": item["sequence_no"],
                        "speaker": item["speaker_name"],
                        "emotion": item["emotion_code"],
                        "start": item["start_time"],
                        "end": item["end_time"],
                        "pause_after": item["pause_after_seconds"],
                    }
                    for item in timeline
                ],
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )
        print(
            "MOTION_CHUNKS="
            + json.dumps(
                [
                    {
                        "chunk_no": item["chunk_no"],
                        "start": item["start_time"],
                        "end": item["end_time"],
                        "duration": item["duration_seconds"],
                        "turns": [t["sequence_no"] for t in item["turns"]],
                    }
                    for item in chunks
                ],
                separators=(",", ":"),
            )
        )

        motion_model = _clean(
            args.motion_model
            or os.getenv("DF_NEXT3_PERFORMANCE_MODEL")
            or (
                "fal-ai/kling-video/v3/pro/image-to-video"
                if args.quality_profile == "premium"
                else os.getenv("FAL_KLING_I2V_MODEL")
            )
            or "fal-ai/kling-video/v3/standard/image-to-video"
        )
        negative_prompt = (
            "visible speech articulation before lipsync, both people talking, repeated mouth flapping, "
            "frozen mannequin pose, identity drift, face morphing, duplicate person, warped hands, extra fingers, "
            "age progression, aging, de-aging, older face, younger face, new wrinkles, facial proportion drift, "
            "skin texture drift, temporal flicker, visual noise, film grain, compression noise, crawling texture, "
            "exaggerated gestures, constant nodding, constant smiling, camera jump, scene cut, clothing change, background change"
        )

        print(f"QUALITY_PROFILE={args.quality_profile}")
        print(f"PERFORMANCE_MOTION_MODEL={motion_model}")

        identity_elements, identity_refs = await _build_identity_elements(
            source_image_url=image_url,
            stage_meta=stage_meta,
            timeline=timeline,
            azure=azure,
            output_container=output_container,
            base_path=base_path,
            root=root,
        )
        identity_prefix = _identity_prompt_prefix(identity_refs)
        print("IDENTITY_REFERENCES=" + json.dumps(identity_refs, separators=(",", ":")))

        normalized_paths: list[Path] = []
        chunk_records: list[dict[str, Any]] = []
        current_start_url = image_url
        target_width = 0
        target_height = 0
        target_fps = 0

        for chunk in chunks:
            chunk_no = int(chunk["chunk_no"])
            planned = float(chunk["duration_seconds"])
            requested_duration = max(3, min(15, int(math.ceil(planned))))
            chunk_blob = f"{base_path}/motion-chunks/chunk-{chunk_no:02d}.mp4"
            last_frame_blob = f"{base_path}/motion-chunks/chunk-{chunk_no:02d}-last.png"
            local_normalized = root / f"chunk-{chunk_no:02d}.mp4"
            chunk_client = cc.get_blob_client(chunk_blob)

            prompt = (
                identity_prefix
                + " "
                + _chunk_motion_prompt(
                    scene_title=_clean(stage["scene_title"]),
                    chunk=chunk,
                    plan=performance_plan,
                )
            )[:3600]
            motion_payload = {
                "prompt": prompt,
                "start_image_url": current_start_url,
                "duration": str(requested_duration),
                "generate_audio": False,
                "elements": identity_elements,
                "cfg_scale": 0.55,
                "shot_type": "customize",
                "negative_prompt": negative_prompt,
            }

            if args.run_id and chunk_client.exists():
                local_normalized.write_bytes(chunk_client.download_blob().readall())
                provider_job_id = "azure-reuse:" + chunk_blob
                print(f"MOTION_CHUNK_REUSE={chunk_no}:{chunk_blob}")
            else:
                provider_job_id, provider_url, _ = await _fal_generate_motion(
                    model_id=motion_model,
                    fal_key=fal_key,
                    payload=motion_payload,
                )
                raw_path = root / f"chunk-{chunk_no:02d}-raw.mp4"
                async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
                    await _download(client, provider_url, raw_path)
                raw_info = _probe_video_info(raw_path)
                if target_width <= 0:
                    target_width = int(raw_info["width"])
                    target_height = int(raw_info["height"])
                    target_fps = max(20, min(30, int(round(float(raw_info["fps"])))))
                filter_value = (
                    f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                    f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
                )
                _run([
                    "ffmpeg", "-y", "-i", str(raw_path),
                    "-t", f"{planned:.3f}",
                    "-vf", filter_value,
                    "-r", str(target_fps),
                    "-an",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-pix_fmt", "yuv420p",
                    str(local_normalized),
                ])
                azure.upload_file(output_container, chunk_blob, str(local_normalized), "video/mp4")
                print(f"MOTION_CHUNK_GENERATED={chunk_no}:{provider_job_id}")

            info = _probe_video_info(local_normalized)
            source_dims_for_ratio = _dict(stage_meta.get("shared_scene_dimensions"))
            source_ratio = float(source_dims_for_ratio["width"]) / float(source_dims_for_ratio["height"])
            chunk_ratio = float(info["width"]) / float(info["height"])
            if abs(chunk_ratio - source_ratio) > 0.08:
                raise RuntimeError(
                    "MOTION_CHUNK_ASPECT_RATIO_DRIFT:"
                    f"chunk={chunk_no}:source_ratio={source_ratio:.6f}:"
                    f"video={info['width']}x{info['height']}"
                )
            if target_width <= 0:
                target_width = int(info["width"])
                target_height = int(info["height"])
                target_fps = max(20, min(30, int(round(float(info["fps"])))))
            if abs(float(info["duration"]) - planned) > 0.35:
                raise RuntimeError(
                    f"MOTION_CHUNK_DURATION_MISMATCH:chunk={chunk_no}:planned={planned}:actual={info['duration']}"
                )

            last_frame_path = root / f"chunk-{chunk_no:02d}-last.png"
            last_client = cc.get_blob_client(last_frame_blob)
            if args.run_id and last_client.exists():
                last_frame_path.write_bytes(last_client.download_blob().readall())
            else:
                _extract_last_frame(local_normalized, last_frame_path)
                azure.upload_file(output_container, last_frame_blob, str(last_frame_path), "image/png")
            current_start_url = azure.sign_read_url(output_container, last_frame_blob, 4 * 3600)

            normalized_paths.append(local_normalized)
            chunk_records.append(
                {
                    "chunk_no": chunk_no,
                    "planned_start_time": float(chunk["start_time"]),
                    "planned_end_time": float(chunk["end_time"]),
                    "planned_duration_seconds": planned,
                    "provider_job_id": provider_job_id,
                    "storage_path": chunk_blob,
                    "last_frame_storage_path": last_frame_blob,
                    "video_info": info,
                    "turn_sequence_numbers": [int(t["sequence_no"]) for t in chunk["turns"]],
                    "performance_prompt": prompt,
                }
            )

        full_motion_path = root / "full-performance-motion.mp4"
        _concat_h264_segments(normalized_paths, full_motion_path)
        full_info = _probe_video_info(full_motion_path)
        full_ratio = float(full_info["width"]) / float(full_info["height"])
        source_dims_for_ratio = _dict(stage_meta.get("shared_scene_dimensions"))
        source_ratio = float(source_dims_for_ratio["width"]) / float(source_dims_for_ratio["height"])
        if abs(full_ratio - source_ratio) > 0.08:
            raise RuntimeError(
                "FULL_PERFORMANCE_ASPECT_RATIO_DRIFT:"
                f"source_ratio={source_ratio:.6f}:"
                f"video={full_info['width']}x{full_info['height']}"
            )
        if abs(float(full_info["duration"]) - float(total_duration)) > 0.6:
            raise RuntimeError(
                "FULL_PERFORMANCE_DURATION_MISMATCH:"
                f"planned={total_duration}:actual={full_info['duration']}"
            )

        raw_full_motion_blob = f"{base_path}/full-performance-motion-raw.mp4"
        azure.upload_file(output_container, raw_full_motion_blob, str(full_motion_path), "video/mp4")
        raw_motion_review_url = azure.sign_read_url(output_container, raw_full_motion_blob, 15 * 24 * 3600)

        clean_motion_path = root / "full-performance-motion-clean.mp4"
        _clean_motion_video(
            full_motion_path,
            clean_motion_path,
            fps=max(20, min(30, int(round(float(full_info["fps"]))))),
        )
        clean_info = _probe_video_info(clean_motion_path)
        clean_motion_blob = f"{base_path}/full-performance-motion-clean.mp4"
        azure.upload_file(output_container, clean_motion_blob, str(clean_motion_path), "video/mp4")
        motion_review_url = azure.sign_read_url(output_container, clean_motion_blob, 15 * 24 * 3600)

        source_dims = _dict(stage_meta.get("shared_scene_dimensions"))
        qc_turns: list[dict[str, Any]] = []
        for turn in timeline:
            coords = _speaker_coordinates(
                stage_meta,
                UUID(str(turn["speaker_participant_id"])),
            )
            qc_turns.append(
                {
                    "sequence_no": turn["sequence_no"],
                    "participant_id": turn["speaker_participant_id"],
                    "display_name": turn["speaker_name"],
                    "coordinates": coords,
                    "start_time": turn["start_time"],
                    "end_time": turn["end_time"],
                }
            )

        motion_qc = _motion_presence_qc(
            video_path=clean_motion_path,
            proof_turns=qc_turns,
            source_width=int(source_dims["width"]),
            source_height=int(source_dims["height"]),
        )

        quality_gate = {
            "status": "REVIEW_REQUIRED" if motion_qc["status"] != "FAIL" else "FAIL",
            "performance_motion": {
                "status": motion_qc["status"],
                "automated_check": "upper_body_motion_presence",
            },
            "speaker_isolation": {"status": "PENDING", "reason": "sync_phase_not_started"},
            "identity_age_stability": {
                "status": "PENDING",
                "reason": "human_review_required_after_identity_anchored_generation",
            },
            "visual_noise": {
                "status": "PENDING",
                "cleanup_applied": True,
                "filter": "hqdn3d+light_unsharp",
            },
            "transition_smoothness": {
                "status": "PENDING",
                "reason": "human_review_required_across_motion_chunk_boundaries",
            },
            "human_performance_review": {
                "status": "PENDING",
                "required_dimensions": [
                    "context_specific_expression",
                    "blinks",
                    "gaze",
                    "head_motion",
                    "body_motion",
                    "gestures",
                    "hands",
                    "listener_reaction",
                    "identity",
                    "apparent_age_stability",
                    "visual_noise",
                    "temporal_continuity",
                    "transition_smoothness",
                    "conversation_pacing",
                ],
            },
        }

        final_manifest = {
            "contract": "next3_shared_scene_full_performance_motion_v1",
            "phase": "motion",
            "workflow_id": str(workflow_id),
            "stage_run_id": str(stage_run_id),
            "shared_scene_media_id": str(shared_media_id),
            "run_id": run_id,
            "turn_count": len(timeline),
            "planned_duration_seconds": total_duration,
            "actual_duration_seconds": float(clean_info["duration"]),
            "media": {
                "width": int(clean_info["width"]),
                "height": int(clean_info["height"]),
                "fps": float(clean_info["fps"]),
                "frame_count": int(clean_info["frame_count"]),
                "storage_path": clean_motion_blob,
                "raw_storage_path": raw_full_motion_blob,
                "cleaned": True,
            },
            "performance_director": performance_plan,
            "identity_references": identity_refs,
            "quality_profile": args.quality_profile,
            "motion_model": motion_model,
            "max_motion_chunk_seconds": max(5.0, min(14.0, float(args.max_motion_chunk_seconds))),
            "timeline": timeline,
            "motion_chunks": chunk_records,
            "quality_gate": quality_gate,
            "diagnostics": {
                "performance_motion_qc": motion_qc,
                "raw_motion_review_url": raw_motion_review_url,
                "clean_motion_review_url": motion_review_url,
                "cleanup_filter": "hqdn3d+light_unsharp+fps_normalization",
            },
            "database_write": "NONE",
            "production_touch": "NONE",
        }

        manifest_path = root / "full-performance-manifest.json"
        manifest_path.write_text(json.dumps(final_manifest, indent=2), encoding="utf-8")
        manifest_blob = f"{base_path}/full-performance-manifest.json"
        azure.upload_file(output_container, manifest_blob, str(manifest_path), "application/json")
        manifest_url = azure.sign_read_url(output_container, manifest_blob, 15 * 24 * 3600)

        print("============================================================")
        print("NEXT3_FULL_SCENE_MOTION_PHASE=PASS")
        print(f"QUALITY_GATE={quality_gate['status']}")
        print(f"PERFORMANCE_MOTION_QC={motion_qc['status']}")
        for item in motion_qc["participants"]:
            print(
                "MOTION_QC "
                f"participant={item['display_name']} "
                f"status={item['status']} "
                f"score={item['upper_body_motion_score']}"
            )
        print(f"TURN_COUNT={len(timeline)}")
        print(f"PLANNED_DURATION_SECONDS={total_duration}")
        print(f"ACTUAL_DURATION_SECONDS={float(clean_info['duration']):.3f}")
        print(f"RAW_FULL_MOTION_URL={raw_motion_review_url}")
        print(f"FULL_MOTION_BASE_URL={motion_review_url}")
        print("AUDIO_PRESENT=NO")
        print("AUDIO_REASON=PRE_LIPSYNC_MOTION_REVIEW_PHASE")
        print(f"MANIFEST_URL={manifest_url}")
        print(f"RUN_ID={run_id}")
        print("SYNC_PHASE=BLOCKED_PENDING_HUMAN_PERFORMANCE_REVIEW")
        print("DATABASE_WRITE=NONE")
        print("PRODUCTION_TOUCH=NONE")
        print("============================================================")


if __name__ == "__main__":
    asyncio.run(full_scene_main())
