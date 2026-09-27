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
    parser.add_argument("--sync-provider-job-id", default="")
    parser.add_argument("--motion-provider-job-id", default="")
    parser.add_argument("--motion-model", default="")
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
            or os.getenv("FAL_KLING_I2V_MODEL")
            or "fal-ai/kling-video/v3/standard/image-to-video"
        )
        motion_prompt = _clean(performance_plan["continuous_motion_prompt"])
        negative_prompt = (
            "visible speech articulation before lipsync, both people talking, repeated mouth flapping, "
            "frozen mannequin pose, identity drift, face morphing, duplicate person, warped hands, extra fingers, "
            "exaggerated gestures, constant nodding, constant smiling, camera jump, scene cut, clothing change, background change"
        )
        motion_payload = {
            "prompt": motion_prompt,
            "start_image_url": image_url,
            "duration": str(motion_duration),
            "aspect_ratio": "16:9",
            "generate_audio": False,
            "shot_type": "customize",
            "negative_prompt": negative_prompt,
        }

        print("PERFORMANCE_DIRECTOR_PLAN=" + json.dumps(performance_plan, ensure_ascii=False))
        print(f"PERFORMANCE_MOTION_MODEL={motion_model}")
        motion_job_id, motion_url, motion_result = await _fal_generate_motion(
            model_id=motion_model,
            fal_key=fal_key,
            payload=motion_payload,
            existing_request_id=args.motion_provider_job_id,
        )

        motion_video = root / "performance-motion.mp4"
        async with httpx.AsyncClient(timeout=180, follow_redirects=True) as motion_client:
            await _download(motion_client, motion_url, motion_video)

        motion_info = _probe_video_info(motion_video)
        motion_aspect = float(motion_info["width"]) / float(motion_info["height"])
        if abs(motion_aspect - (16.0 / 9.0)) > 0.08:
            raise RuntimeError(
                "PERFORMANCE_MOTION_ASPECT_RATIO_DRIFT:"
                f"{motion_info['width']}x{motion_info['height']}"
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
        base_path = f"v3/qa/shared-scene-performance/{workflow_id}/{stage_run_id}/{stamp}"
        source_blob = f"{base_path}/performance-motion.mp4"
        azure.upload_file(output_container, source_blob, str(motion_video), "video/mp4")
        source_video_url = azure.sign_read_url(output_container, source_blob, 3600)
        durable_motion_url = azure.sign_read_url(output_container, source_blob, 15 * 24 * 3600)

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
                f"coords={turn['coordinates']} "
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

        manifest = {
            "contract": "next3_shared_scene_two_turn_performance_proof_v1",
            "workflow_id": str(workflow_id),
            "stage_run_id": str(stage_run_id),
            "shared_scene_media_id": str(shared_media_id),
            "performance_director": performance_plan,
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
            "active_speaker_qc": qc,
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
        print(f"ACTIVE_SPEAKER_QC={qc['status']}")
        for result in qc["segments"]:
            print(
                "QC_SEGMENT "
                f"seq={result['sequence_no']} speaker={result['display_name']} "
                f"status={result['status']} reason={result['reason']} "
                f"target_motion={result['intended_motion_score']} "
                f"non_speaker_motion={result['max_non_speaker_motion_score']} "
                f"ratio={result['non_speaker_to_intended_ratio']} "
                f"corr={result['motion_correlation']}"
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


if __name__ == "__main__":
    asyncio.run(main())
