#!/usr/bin/env bash
set -euo pipefail

# V3 Fusion single-presenter repair candidate.
#
# Purpose:
#   - Operate ONLY on the already-successful 28 child Fusion videos.
#   - Detect vertically stacked duplicate-presenter child videos.
#   - Crop ONLY detected duplicate clips to one presenter (upper half).
#   - Re-stitch locally using the canonical V3 stitch implementation.
#   - Upload a review-only candidate to the existing Azure video-output container.
#
# Non-goals / hard safety rules:
#   - NO provider submission.
#   - NO new Fusion attempt.
#   - NO pricing reserve/commit/release.
#   - NO mutation of V3 workflow/stage/review/output rows.
#   - NO replacement of the currently active Fusion output.
#
# The candidate is intentionally review-only. Promotion happens only after a human
# reviews the generated SAS URL and explicitly accepts the visual correction.

readonly WORKFLOW_ID="06c5d43e-7bbc-4cb4-aef3-9df36886da3b"
readonly FUSION_STAGE_ID="4038a526-308a-49ba-959a-7e40f512c3b3"
readonly EXPECTED_ATTEMPT_NO="4"
readonly EXPECTED_CHILDREN="28"
readonly EXPECTED_CREDITS="-560"
readonly EXPECTED_CONTAINER="video-output"

readonly STATE_DIR="${HOME}/.local/state/desifaces-v3/fusion-single-presenter-repair"
readonly LOG_FILE="${STATE_DIR}/repair.log"
readonly PID_FILE="${STATE_DIR}/repair.pid"

mkdir -p "$STATE_DIR"

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

status() {
  echo "============================================================"
  echo " V3 FUSION SINGLE-PRESENTER REPAIR STATUS"
  echo "============================================================"
  if is_running; then
    echo "VISUAL_REPAIR_PROCESS=RUNNING"
    echo "REPAIR_PID=$(cat "$PID_FILE")"
  else
    echo "VISUAL_REPAIR_PROCESS=NOT_RUNNING"
  fi
  echo "LOG_FILE=$LOG_FILE"
  if [[ -f "$LOG_FILE" ]]; then
    echo "---------------- LAST 80 LOG LINES ----------------"
    tail -n 80 "$LOG_FILE"
  fi
}

start() {
  if is_running; then
    echo "VISUAL_REPAIR_ALREADY_RUNNING=YES"
    echo "REPAIR_PID=$(cat "$PID_FILE")"
    echo "STATUS_COMMAND=bash scripts/v3-fusion-single-presenter-repair.sh status"
    return 0
  fi

  : > "$LOG_FILE"
  nohup bash "$0" __run >"$LOG_FILE" 2>&1 </dev/null &
  local pid=$!
  echo "$pid" > "$PID_FILE"

  # Prove the detached worker survived initial exec before returning control.
  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "VISUAL_REPAIR_LAUNCH_FAILED=YES" >&2
    tail -n 80 "$LOG_FILE" >&2 || true
    return 1
  fi

  echo "VISUAL_REPAIR_LAUNCHED=YES"
  echo "REPAIR_PID=$pid"
  echo "LOG_FILE=$LOG_FILE"
  echo "STATUS_COMMAND=bash scripts/v3-fusion-single-presenter-repair.sh status"
}

run_repair() {
  trap 'rm -f "$PID_FILE"' EXIT

  echo "$(date -Is) VISUAL_REPAIR_STARTED=YES"
  echo "WORKFLOW_ID=$WORKFLOW_ID"
  echo "FUSION_STAGE_ID=$FUSION_STAGE_ID"
  echo "PROVIDER_RERENDER_ALLOWED=NO"
  echo "WORKFLOW_DB_MUTATION_ALLOWED=NO"
  echo "CROP_POLICY=upper_half_single_presenter"

  [[ "$(git branch --show-current)" == "feature/v3-multiperson-core-20260818" ]] || {
    echo "ERROR: wrong branch" >&2
    exit 1
  }

  docker inspect df-v3-svc-fusion-extension >/dev/null 2>&1 || {
    echo "ERROR: df-v3-svc-fusion-extension container is not available" >&2
    exit 1
  }

  docker exec \
    -e DF_REPAIR_WORKFLOW_ID="$WORKFLOW_ID" \
    -e DF_REPAIR_STAGE_ID="$FUSION_STAGE_ID" \
    -e DF_REPAIR_EXPECTED_ATTEMPT_NO="$EXPECTED_ATTEMPT_NO" \
    -e DF_REPAIR_EXPECTED_CHILDREN="$EXPECTED_CHILDREN" \
    -e DF_REPAIR_EXPECTED_CREDITS="$EXPECTED_CREDITS" \
    -e DF_REPAIR_EXPECTED_CONTAINER="$EXPECTED_CONTAINER" \
    -i df-v3-svc-fusion-extension \
    python - <<'PY'
from __future__ import annotations

import asyncio
import collections
import json
import math
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import asyncpg
from azure.storage.blob import BlobServiceClient, ContentSettings

from app.config import settings
from app.services.sas_service import AzureBlobService
from app.services.stitch_service import stitch_videos


WORKFLOW_ID = os.environ["DF_REPAIR_WORKFLOW_ID"]
STAGE_ID = os.environ["DF_REPAIR_STAGE_ID"]
EXPECTED_ATTEMPT_NO = int(os.environ["DF_REPAIR_EXPECTED_ATTEMPT_NO"])
EXPECTED_CHILDREN = int(os.environ["DF_REPAIR_EXPECTED_CHILDREN"])
EXPECTED_CREDITS = int(os.environ["DF_REPAIR_EXPECTED_CREDITS"])
EXPECTED_CONTAINER = os.environ["DF_REPAIR_EXPECTED_CONTAINER"]

# Conservative detector:
#   A stacked 2-up presenter output has:
#     1) a strong horizontal discontinuity exactly at the half-frame boundary, and
#     2) visually similar upper/lower panels.
#   Requiring BOTH avoids classifying an ordinary portrait as a duplicate.
MIN_TALL_ASPECT = 1.20
MIN_CENTER_SEAM_RATIO = 2.0
MIN_DHASH_SIMILARITY = 0.60
MIN_DUPLICATE_SAMPLE_VOTES = 2
DUPLICATE_SAMPLE_FRACTIONS = (0.25, 0.50, 0.75)


def fail(message: str) -> None:
    raise RuntimeError(message)


def as_dict(value) -> dict[str, object]:
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


def run(cmd: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "command_failed rc=%s cmd=%s stderr=%s"
            % (proc.returncode, " ".join(cmd), (proc.stderr or "")[-1600:])
        )
    return proc


def probe(path: str) -> tuple[int, int, float]:
    proc = run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json",
            path,
        ],
        timeout=60,
    )
    payload = json.loads(proc.stdout or "{}")
    streams = list(payload.get("streams") or [])
    if not streams:
        fail(f"video_stream_missing:{path}")
    width = int(streams[0].get("width") or 0)
    height = int(streams[0].get("height") or 0)
    duration = float((payload.get("format") or {}).get("duration") or 0.0)
    if width <= 0 or height <= 0 or duration <= 0:
        fail(f"invalid_video_probe:{path}:{width}x{height}:{duration}")
    return width, height, duration


def _dhash_bits(image, *, hash_size: int = 16):
    import numpy as np
    from PIL import Image

    gray = image.convert("L").resize(
        (hash_size + 1, hash_size),
        Image.Resampling.LANCZOS,
    )
    values = np.asarray(gray, dtype=np.int16)
    return (values[:, 1:] > values[:, :-1]).reshape(-1)


def duplicate_frame_metrics(frame_path: str) -> tuple[float, float]:
    import numpy as np
    from PIL import Image

    image = Image.open(frame_path).convert("RGB")
    rgb = np.asarray(image, dtype=np.int16)
    height, width = rgb.shape[:2]
    if height < 4 or width < 4:
        fail(f"invalid_sample_frame:{frame_path}")

    gray = (
        rgb[:, :, 0] * 299
        + rgb[:, :, 1] * 587
        + rgb[:, :, 2] * 114
    ) // 1000

    center = height // 2
    seam_delta = float(
        np.mean(
            np.abs(
                gray[center, :].astype(np.int32)
                - gray[center - 1, :].astype(np.int32)
            )
        )
    )
    adjacent = np.mean(
        np.abs(
            gray[1:, :].astype(np.int32)
            - gray[:-1, :].astype(np.int32)
        ),
        axis=1,
    )
    baseline = float(np.median(adjacent))
    seam_ratio = seam_delta / max(0.5, baseline)

    top = image.crop((0, 0, width, center))
    bottom = image.crop((0, center, width, height))
    top_bits = _dhash_bits(top)
    bottom_bits = _dhash_bits(bottom)
    if len(top_bits) != len(bottom_bits):
        fail(f"dhash_length_mismatch:{frame_path}")
    dhash_similarity = 1.0 - (
        float(np.count_nonzero(top_bits != bottom_bits))
        / float(len(top_bits))
    )
    return seam_ratio, dhash_similarity


def stacked_duplicate_metrics(
    path: str,
    *,
    duration: float,
    width: int,
    height: int,
    scratch_dir: str,
) -> tuple[bool, float, float, int]:
    tall_aspect = height / float(width)
    if tall_aspect < MIN_TALL_ASPECT:
        return False, 0.0, 0.0, 0

    import numpy as np

    seam_ratios: list[float] = []
    dhash_similarities: list[float] = []
    votes = 0

    for sample_index, fraction in enumerate(DUPLICATE_SAMPLE_FRACTIONS):
        sample_time = max(
            0.0,
            min(max(0.0, duration - 0.05), duration * float(fraction)),
        )
        frame_path = os.path.join(
            scratch_dir,
            f"sample_{sample_index:02d}.png",
        )
        run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-nostdin",
                "-ss", f"{sample_time:.3f}",
                "-i", path,
                "-frames:v", "1",
                "-an",
                frame_path,
            ],
            timeout=60,
        )
        seam_ratio, dhash_similarity = duplicate_frame_metrics(frame_path)
        seam_ratios.append(seam_ratio)
        dhash_similarities.append(dhash_similarity)
        if (
            seam_ratio >= MIN_CENTER_SEAM_RATIO
            and dhash_similarity >= MIN_DHASH_SIMILARITY
        ):
            votes += 1
        Path(frame_path).unlink(missing_ok=True)

    median_seam = float(np.median(np.asarray(seam_ratios, dtype=float)))
    median_similarity = float(
        np.median(np.asarray(dhash_similarities, dtype=float))
    )
    duplicate = votes >= MIN_DUPLICATE_SAMPLE_VOTES
    return duplicate, median_seam, median_similarity, votes


def normalize_or_crop(
    src: str,
    dst: str,
    *,
    duplicate: bool,
    target_width: int,
    target_height: int,
) -> None:
    filters: list[str] = []
    if duplicate:
        # Product rule for this repair: retain one coherent presenter view only.
        # Upper/lower panels are near-duplicate renditions of the same speaker.
        filters.append("crop=iw:floor(ih/2):0:0")
    filters.extend(
        [
            f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease",
            f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:black",
            "setsar=1",
            "fps=30",
            "format=yuv420p",
        ]
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostdin",
            "-i", src,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-vf", ",".join(filters),
            "-c:v", "libx264",
            "-preset", "superfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "48000",
            "-movflags", "+faststart",
            dst,
        ],
        timeout=300,
    )
    if not Path(dst).is_file() or Path(dst).stat().st_size <= 0:
        fail(f"repaired_segment_missing:{dst}")


def parse_blob_location(video_url: str) -> tuple[str, str]:
    parsed = urlsplit(str(video_url or "").strip())
    host = str(parsed.hostname or "").lower()
    if not host.endswith(".blob.core.windows.net"):
        fail(f"child_video_not_azure_blob:{host or 'missing-host'}")
    path = parsed.path.lstrip("/")
    container, sep, blob_name = path.partition("/")
    if not sep or not container or not blob_name:
        fail(f"child_video_blob_path_invalid:{parsed.path}")
    return container, blob_name


def parse_media_location(storage_ref: str, meta: dict[str, object]) -> tuple[str, str]:
    container = str(meta.get("storage_container") or "").strip()
    blob_name = str(
        meta.get("storage_path")
        or meta.get("blob_name")
        or ""
    ).strip().lstrip("/")
    if container and blob_name:
        return container, blob_name

    ref = str(storage_ref or "").strip()
    if ref.startswith("azure://"):
        remainder = ref[len("azure://"):]
        container, sep, blob_name = remainder.partition("/")
        if sep and container and blob_name:
            return container, blob_name.lstrip("/")
    if ref.startswith("https://"):
        return parse_blob_location(ref)
    fail("media_storage_lineage_missing")


async def snapshot(conn: asyncpg.Connection) -> dict[str, object]:
    stage = await conn.fetchrow(
        """
        select s.state,
               coalesce(s.metadata_json->'fusion_parent_pricing'->>'state','') as parent_state,
               (select count(*) from public.v3_studio_stage_outputs o
                 where o.stage_run_id=s.stage_run_id and o.is_active=true) as active_outputs,
               (select count(*) from public.v3_studio_review_items r
                 join public.v3_studio_stage_outputs o
                   on o.stage_run_id=r.stage_run_id
                  and o.media_id=r.media_id
                  and o.is_active=true
                 where r.stage_run_id=s.stage_run_id and r.decision='pending') as pending_review
        from public.v3_studio_stage_runs s
        where s.stage_run_id=$1::uuid
        """,
        STAGE_ID,
    )
    if not stage:
        fail("fusion_stage_missing")

    attempt = await conn.fetchrow(
        """
        select attempt_id::text,attempt_no,state,metadata_json
        from public.v3_studio_stage_attempts
        where stage_run_id=$1::uuid
        order by attempt_no desc limit 1
        """,
        STAGE_ID,
    )
    if not attempt:
        fail("fusion_attempt_missing")

    active_jobs = int(
        await conn.fetchval(
            """
            select count(*)
            from public.studio_jobs
            where status in (
              'queued','running','processing','submitted','pending',
              'finalizing','pricing_pending'
            )
            """
        )
        or 0
    )

    provider_jobs = int(
        await conn.fetchval(
            """
            select count(*)
            from public.studio_jobs j
            where j.studio_type='fusion'
              and (
                j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'=$1
                or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'=$1
                or j.payload_json #>> '{billing_context,billing_parent_job_id}'=$1
                or j.payload_json #>> '{pricing,parent_job_id}'=$1
              )
            """,
            STAGE_ID,
        )
        or 0
    )

    provider_succeeded = int(
        await conn.fetchval(
            """
            select count(*)
            from public.studio_jobs j
            where j.studio_type='fusion' and j.status='succeeded'
              and (
                j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'=$1
                or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'=$1
                or j.payload_json #>> '{billing_context,billing_parent_job_id}'=$1
                or j.payload_json #>> '{pricing,parent_job_id}'=$1
              )
            """,
            STAGE_ID,
        )
        or 0
    )

    consume_events = int(
        await conn.fetchval(
            """
            select count(*)
            from public.pricing_credit_ledger_events l
            join public.v3_studio_workflows w on w.owner_user_id=l.user_id
            where w.workflow_id=$1::uuid
              and l.idempotency_key like
                'consume:svc-fusion-extension:v3-scene:' || $2 || ':commit:%'
            """,
            WORKFLOW_ID,
            STAGE_ID,
        )
        or 0
    )
    credit_delta = int(
        await conn.fetchval(
            """
            select coalesce(sum(l.credits_delta),0)
            from public.pricing_credit_ledger_events l
            join public.v3_studio_workflows w on w.owner_user_id=l.user_id
            where w.workflow_id=$1::uuid
              and l.idempotency_key like
                'consume:svc-fusion-extension:v3-scene:' || $2 || ':commit:%'
            """,
            WORKFLOW_ID,
            STAGE_ID,
        )
        or 0
    )

    return {
        "stage_state": str(stage["state"]),
        "parent_state": str(stage["parent_state"]),
        "active_outputs": int(stage["active_outputs"] or 0),
        "pending_review": int(stage["pending_review"] or 0),
        "attempt_id": str(attempt["attempt_id"]),
        "attempt_no": int(attempt["attempt_no"]),
        "attempt_state": str(attempt["state"]),
        "attempt_metadata": as_dict(attempt["metadata_json"]),
        "active_jobs": active_jobs,
        "provider_jobs": provider_jobs,
        "provider_succeeded": provider_succeeded,
        "consume_events": consume_events,
        "credit_delta": credit_delta,
    }


def assert_safe_state(s: dict[str, object]) -> None:
    assert s["stage_state"] == "awaiting_review", s
    assert s["parent_state"] == "committed", s
    assert s["attempt_no"] == EXPECTED_ATTEMPT_NO, s
    assert s["attempt_state"] == "succeeded", s
    assert s["active_outputs"] == 1, s
    assert s["pending_review"] == 1, s
    assert s["active_jobs"] == 0, s
    assert s["provider_jobs"] == EXPECTED_CHILDREN, s
    assert s["provider_succeeded"] == EXPECTED_CHILDREN, s
    assert s["consume_events"] == 1, s
    assert s["credit_delta"] == EXPECTED_CREDITS, s


async def main() -> None:
    conn = await asyncpg.connect(settings.DATABASE_URL)
    try:
        before = await snapshot(conn)
        assert_safe_state(before)
        print("PRE_REPAIR_DURABLE_STATE_GATE=PASS")
        print(f"ATTEMPT_ID={before['attempt_id']}")
        print(f"ATTEMPT_NO={before['attempt_no']}")
        print(f"PROVIDER_CHILDREN={before['provider_jobs']}")
        print(f"PROVIDER_CHILDREN_SUCCEEDED={before['provider_succeeded']}")
        print(f"PARENT_CONSUME_EVENTS={before['consume_events']}")
        print(f"PARENT_CREDIT_DELTA={before['credit_delta']}")

        children = list(
            (before["attempt_metadata"] or {}).get("children") or []  # type: ignore[union-attr]
        )
        if len(children) != EXPECTED_CHILDREN:
            fail(f"expected_{EXPECTED_CHILDREN}_children_found_{len(children)}")

        normalized_children: list[dict[str, object]] = []
        seen_turns: set[str] = set()
        for index, raw in enumerate(children, start=1):
            child = dict(raw or {})
            turn_id = str(child.get("dialogue_turn_id") or "").strip()
            video_url = str(child.get("video_url") or "").strip()
            status = str(child.get("status") or "").strip().lower()
            sequence_no = int(child.get("sequence_no") or index)
            if not turn_id or turn_id in seen_turns:
                fail(f"invalid_or_duplicate_dialogue_turn:{turn_id or 'missing'}")
            if status not in {"succeeded", "completed", "complete", "ready"}:
                fail(f"child_not_successful:sequence={sequence_no}:status={status}")
            if not video_url:
                fail(f"child_video_url_missing:sequence={sequence_no}")
            container, blob_name = parse_blob_location(video_url)
            if container != EXPECTED_CONTAINER:
                fail(
                    f"unexpected_child_container:sequence={sequence_no}:"
                    f"{container}!={EXPECTED_CONTAINER}"
                )
            seen_turns.add(turn_id)
            normalized_children.append(
                {
                    "sequence_no": sequence_no,
                    "dialogue_turn_id": turn_id,
                    "participant_id": str(child.get("participant_id") or ""),
                    "fusion_job_id": str(child.get("fusion_job_id") or ""),
                    "container": container,
                    "blob_name": blob_name,
                }
            )

        normalized_children.sort(key=lambda item: int(item["sequence_no"]))
        if len({int(item["sequence_no"]) for item in normalized_children}) != EXPECTED_CHILDREN:
            fail("child_sequence_numbers_not_unique")

        bsc = BlobServiceClient.from_connection_string(
            settings.AZURE_STORAGE_CONNECTION_STRING
        )
        canonical_container = bsc.get_container_client(EXPECTED_CONTAINER)
        canonical_container.get_container_properties()
        print(f"AZURE_CONTAINER={EXPECTED_CONTAINER}")
        print("AZURE_CONTAINER_RESOLVE=PASS")

        with tempfile.TemporaryDirectory(prefix="df_v3_single_presenter_repair_") as td:
            root = Path(td)
            source_dir = root / "source"
            repaired_dir = root / "repaired"
            face_dir = root / "faces"
            source_dir.mkdir()
            repaired_dir.mkdir()
            face_dir.mkdir()

            # Root-cause audit: Director sends exactly one approved primary face
            # asset to each provider child. If that face asset itself is a two-up
            # stack, every child for that participant will naturally preserve the
            # duplicate composition. Detect that once per unique participant.
            participant_ids = sorted(
                {
                    str(item["participant_id"])
                    for item in normalized_children
                    if str(item["participant_id"]).strip()
                }
            )
            duplicate_source_participants: set[str] = set()
            source_face_audit: list[dict[str, object]] = []

            for participant_id in participant_ids:
                face_row = await conn.fetchrow(
                    """
                    select p.primary_face_media_id::text as media_id,
                           ma.storage_ref,
                           ma.meta_json
                    from public.v3_participants p
                    join public.media_assets ma
                      on ma.id=p.primary_face_media_id
                    where p.participant_id=$1::uuid
                    """,
                    participant_id,
                )
                if not face_row:
                    print(
                        "SOURCE_FACE_AUDIT "
                        f"participant={participant_id} unavailable=YES"
                    )
                    continue

                media_id = str(face_row["media_id"])
                container, blob_name = parse_media_location(
                    str(face_row["storage_ref"] or ""),
                    as_dict(face_row["meta_json"]),
                )
                face_path = face_dir / f"{participant_id}.bin"
                face_blob = bsc.get_blob_client(
                    container=container,
                    blob=blob_name,
                )
                with face_path.open("wb") as handle:
                    face_blob.download_blob(max_concurrency=2).readinto(handle)

                from PIL import Image
                with Image.open(face_path) as face_image:
                    face_width, face_height = face_image.size

                face_ratio = face_height / float(face_width)
                face_seam, face_similarity = duplicate_frame_metrics(
                    str(face_path)
                )
                source_stacked = (
                    face_ratio >= MIN_TALL_ASPECT
                    and face_seam >= MIN_CENTER_SEAM_RATIO
                    and face_similarity >= MIN_DHASH_SIMILARITY
                )
                if source_stacked:
                    duplicate_source_participants.add(participant_id)

                source_face_audit.append(
                    {
                        "participant_id": participant_id,
                        "media_id": media_id,
                        "width": face_width,
                        "height": face_height,
                        "height_width_ratio": round(face_ratio, 4),
                        "center_seam_ratio": round(face_seam, 6),
                        "top_bottom_dhash_similarity": round(
                            face_similarity, 6
                        ),
                        "stacked_duplicate_detected": source_stacked,
                    }
                )
                print(
                    "SOURCE_FACE_AUDIT "
                    f"participant={participant_id} "
                    f"media_id={media_id} "
                    f"size={face_width}x{face_height} "
                    f"ratio={face_ratio:.3f} "
                    f"center_seam_ratio={face_seam:.3f} "
                    f"top_bottom_dhash={face_similarity:.3f} "
                    f"stacked_duplicate="
                    f"{'YES' if source_stacked else 'NO'}"
                )

            print(
                "SOURCE_FACE_STACKED_PARTICIPANTS="
                + (
                    ",".join(sorted(duplicate_source_participants))
                    if duplicate_source_participants
                    else "NONE"
                )
            )

            def download_one(item: dict[str, object]) -> Path:
                seq = int(item["sequence_no"])
                dest = source_dir / f"{seq:04d}.mp4"
                blob = bsc.get_blob_client(
                    container=str(item["container"]),
                    blob=str(item["blob_name"]),
                )
                with dest.open("wb") as handle:
                    stream = blob.download_blob(max_concurrency=2)
                    stream.readinto(handle)
                if dest.stat().st_size <= 0:
                    fail(f"downloaded_child_empty:sequence={seq}")
                return dest

            print("DOWNLOADING_PRESERVED_CHILDREN=STARTED")
            downloaded: dict[int, Path] = {}
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {
                    executor.submit(download_one, item): int(item["sequence_no"])
                    for item in normalized_children
                }
                for future in as_completed(futures):
                    seq = futures[future]
                    downloaded[seq] = future.result()
            if len(downloaded) != EXPECTED_CHILDREN:
                fail(f"preserved_download_incomplete:{len(downloaded)}")
            print(f"PRESERVED_CHILDREN_DOWNLOADED={len(downloaded)}/{EXPECTED_CHILDREN}")

            audit: list[dict[str, object]] = []
            post_dims: list[tuple[int, int]] = []
            for item in normalized_children:
                seq = int(item["sequence_no"])
                path = str(downloaded[seq])
                width, height, duration = probe(path)
                tall_aspect = height / float(width)
                sample_dir = str(root / f"audit_{seq:04d}")
                Path(sample_dir).mkdir(parents=True, exist_ok=True)
                video_duplicate, seam_ratio, dhash_similarity, duplicate_votes = (
                    stacked_duplicate_metrics(
                        path,
                        duration=duration,
                        width=width,
                        height=height,
                        scratch_dir=sample_dir,
                    )
                )
                Path(sample_dir).rmdir()
                source_face_duplicate = (
                    str(item["participant_id"])
                    in duplicate_source_participants
                )
                duplicate = source_face_duplicate or video_duplicate
                repaired_dims = (
                    (width, max(2, (height // 2) // 2 * 2))
                    if duplicate
                    else (width, height)
                )
                post_dims.append(repaired_dims)
                audit.append(
                    {
                        **item,
                        "width": width,
                        "height": height,
                        "duration_sec": round(duration, 3),
                        "height_width_ratio": round(tall_aspect, 4),
                        "center_seam_ratio": round(seam_ratio, 6),
                        "top_bottom_dhash_similarity": round(
                            dhash_similarity, 6
                        ),
                        "duplicate_sample_votes": duplicate_votes,
                        "source_face_stacked_duplicate": (
                            source_face_duplicate
                        ),
                        "video_stacked_duplicate": video_duplicate,
                        "stacked_duplicate_detected": duplicate,
                        "post_repair_width": repaired_dims[0],
                        "post_repair_height": repaired_dims[1],
                    }
                )
                print(
                    "SEGMENT_AUDIT "
                    f"sequence={seq} "
                    f"size={width}x{height} "
                    f"ratio={tall_aspect:.3f} "
                    f"center_seam_ratio={seam_ratio:.3f} "
                    f"top_bottom_dhash={dhash_similarity:.3f} "
                    f"duplicate_votes={duplicate_votes}/"
                    f"{len(DUPLICATE_SAMPLE_FRACTIONS)} "
                    f"source_face_duplicate="
                    f"{'YES' if source_face_duplicate else 'NO'} "
                    f"video_duplicate="
                    f"{'YES' if video_duplicate else 'NO'} "
                    f"stacked_duplicate={'YES' if duplicate else 'NO'}"
                )

            duplicates = [
                item for item in audit if bool(item["stacked_duplicate_detected"])
            ]
            if not duplicates:
                fail(
                    "no_stacked_duplicate_segments_detected;"
                    "review detector threshold before any visual mutation"
                )

            # Choose the most common single-view geometry after virtual cropping.
            # This naturally converges to the original intended presenter frame size
            # when the provider output is a two-up vertical stack.
            dims_counter = collections.Counter(post_dims)
            (target_width, target_height), target_count = dims_counter.most_common(1)[0]
            target_width = int(target_width) // 2 * 2
            target_height = int(target_height) // 2 * 2
            if target_width <= 0 or target_height <= 0:
                fail("invalid_repair_target_geometry")

            print(f"DUPLICATE_SEGMENT_COUNT={len(duplicates)}")
            print(
                "DUPLICATE_SEQUENCE_NOS="
                + ",".join(str(item["sequence_no"]) for item in duplicates)
            )
            print(f"CLEAN_SEGMENT_COUNT={EXPECTED_CHILDREN-len(duplicates)}")
            print(f"REPAIR_TARGET_DIMENSIONS={target_width}x{target_height}")
            print(f"REPAIR_TARGET_DIMENSION_SUPPORT={target_count}/{EXPECTED_CHILDREN}")

            repaired_paths: list[str] = []
            for item in audit:
                seq = int(item["sequence_no"])
                src = str(downloaded[seq])
                dst = str(repaired_dir / f"{seq:04d}.mp4")
                normalize_or_crop(
                    src,
                    dst,
                    duplicate=bool(item["stacked_duplicate_detected"]),
                    target_width=target_width,
                    target_height=target_height,
                )
                repaired_paths.append(dst)

            print(f"REPAIRED_SEGMENTS_READY={len(repaired_paths)}/{EXPECTED_CHILDREN}")
            print("PROVIDER_RERENDER=NOT_CALLED")

            candidate_path = str(root / "fusion-single-presenter-review.mp4")
            stitch_started = time.monotonic()
            stitch_videos(
                repaired_paths,
                candidate_path,
                stitch_mode_override="xfade",
            )
            stitch_ms = int((time.monotonic() - stitch_started) * 1000)
            final_width, final_height, final_duration = probe(candidate_path)
            print(f"REPAIRED_STITCH_MS={stitch_ms}")
            print(
                f"REPAIRED_VIDEO_GEOMETRY="
                f"{final_width}x{final_height}"
            )
            print(f"REPAIRED_VIDEO_DURATION_SEC={final_duration:.3f}")

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            attempt_id = str(before["attempt_id"])
            prefix = (
                f"v3/story-scene/{WORKFLOW_ID}/{STAGE_ID}/"
                f"visual-repair/{attempt_id}/{timestamp}"
            )
            candidate_blob = f"{prefix}/single-presenter-candidate.mp4"
            manifest_blob = f"{prefix}/repair-manifest.json"

            candidate_client = bsc.get_blob_client(
                container=EXPECTED_CONTAINER,
                blob=candidate_blob,
            )
            with open(candidate_path, "rb") as handle:
                candidate_client.upload_blob(
                    handle,
                    overwrite=False,
                    content_settings=ContentSettings(content_type="video/mp4"),
                )
            candidate_client.get_blob_properties()

            manifest = {
                "workflow_id": WORKFLOW_ID,
                "stage_run_id": STAGE_ID,
                "attempt_id": attempt_id,
                "source_child_count": EXPECTED_CHILDREN,
                "provider_rerender": False,
                "workflow_db_mutated": False,
                "crop_policy": "upper_half_single_presenter",
                "duplicate_detection": {
                    "minimum_height_width_ratio": MIN_TALL_ASPECT,
                    "minimum_center_seam_ratio": MIN_CENTER_SEAM_RATIO,
                    "minimum_top_bottom_dhash_similarity": (
                        MIN_DHASH_SIMILARITY
                    ),
                    "minimum_duplicate_sample_votes": (
                        MIN_DUPLICATE_SAMPLE_VOTES
                    ),
                    "sample_fractions": list(
                        DUPLICATE_SAMPLE_FRACTIONS
                    ),
                },
                "source_face_audit": source_face_audit,
                "source_face_stacked_participants": sorted(
                    duplicate_source_participants
                ),
                "target_dimensions": [target_width, target_height],
                "candidate_dimensions": [final_width, final_height],
                "candidate_duration_sec": round(final_duration, 3),
                "segments": audit,
            }
            manifest_client = bsc.get_blob_client(
                container=EXPECTED_CONTAINER,
                blob=manifest_blob,
            )
            manifest_client.upload_blob(
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
                overwrite=False,
                content_settings=ContentSettings(
                    content_type="application/json"
                ),
            )

            # Prove our read/process/upload operation did not mutate the durable
            # workflow, provider-job population, or financial state.
            after = await snapshot(conn)
            assert_safe_state(after)
            immutable_fields = (
                "stage_state",
                "parent_state",
                "active_outputs",
                "pending_review",
                "attempt_id",
                "attempt_no",
                "attempt_state",
                "active_jobs",
                "provider_jobs",
                "provider_succeeded",
                "consume_events",
                "credit_delta",
            )
            for field in immutable_fields:
                if after[field] != before[field]:
                    fail(
                        f"durable_state_changed_during_repair:"
                        f"{field}:{before[field]}->{after[field]}"
                    )

            review_url = AzureBlobService(
                settings.AZURE_STORAGE_CONNECTION_STRING
            ).sign_read_url(
                EXPECTED_CONTAINER,
                candidate_blob,
                21600,
            )

            print("POST_REPAIR_DURABLE_STATE_GATE=PASS")
            print("NO_FUSION_RETRY_CREATED=PASS")
            print("NO_PROVIDER_RERENDER=PASS")
            print("NO_PRICING_CHANGE=PASS")
            print("ACTIVE_FUSION_OUTPUT_REPLACED=NO")
            print("HITL_DECISION_CHANGED=NO")
            print(f"REPAIR_MANIFEST_BLOB={manifest_blob}")
            print(f"REPAIR_CANDIDATE_BLOB={candidate_blob}")
            print("VISUAL_REPAIR_CANDIDATE=PASS")
            print("WORKFLOW_PROMOTION_ALLOWED=NO_UNTIL_HUMAN_REVIEW")
            print("REVIEW_URL=")
            print(review_url)
    finally:
        await conn.close()


asyncio.run(main())
PY

  echo "$(date -Is) VISUAL_REPAIR_FINISHED=YES"
}

case "${1:-}" in
  start)
    start
    ;;
  status)
    status
    ;;
  __run)
    run_repair
    ;;
  *)
    echo "Usage: $0 {start|status}" >&2
    exit 2
    ;;
esac
