from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class ShotPlanItem(TypedDict, total=False):
    shot_id: str
    shot_type: str
    render_kind: str
    provider_hint: str
    start_sec: float
    end_sec: float
    aspect_ratio: str
    background_style: str
    camera_style: str
    gesture_style: str
    movement_intensity: str
    prompt_fragment: str


class ChildRenderRecord(TypedDict, total=False):
    shot_id: str
    render_kind: str
    provider_hint: str
    fusion_job_id: str
    provider_job_id: str
    status: str
    clip_artifact_id: str
    clip_url: str
    start_sec: float
    end_sec: float
    error_code: str
    error_message: str


class StoryGraphState(TypedDict, total=False):
    job_id: str
    user_id: str

    face_artifact_id: str
    face_ref_urls: List[str]

    audio_artifact_id: str
    audio_url: str
    transcript: str
    word_timings: List[Dict[str, Any]]
    audio_duration_sec: float

    video_direction: str
    aspect_ratio: str
    parsed_direction: Dict[str, Any]

    shot_plan: List[ShotPlanItem]
    provider_plan: Dict[str, Any]

    pricing: Dict[str, Any]
    pricing_summary: Dict[str, Any]

    anchor_jobs: List[ChildRenderRecord]
    insert_jobs: List[ChildRenderRecord]

    composed_video_url: Optional[str]
    final_artifact_id: Optional[str]

    failures: List[Dict[str, Any]]
    status: str
