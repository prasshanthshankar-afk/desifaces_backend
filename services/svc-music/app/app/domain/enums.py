from __future__ import annotations
from enum import Enum


class MusicProjectMode(str, Enum):
    autopilot = "autopilot"
    co_create = "co_create"
    byo = "byo"


class DuetLayout(str, Enum):
    split_screen = "split_screen"
    alternating = "alternating"
    same_stage = "same_stage"


class CameraEdit(str, Enum):
    smooth = "smooth"
    beat_cut = "beat_cut"
    aggressive = "aggressive"


class MusicProjectStatus(str, Enum):
    draft = "draft"
    planning = "planning"
    ready = "ready"
    rendering = "rendering"
    succeeded = "succeeded"
    failed = "failed"


class MusicTrackType(str, Enum):
    instrumental = "instrumental"
    vocals = "vocals"
    full_mix = "full_mix"
    stems_zip = "stems_zip"
    lyrics_json = "lyrics_json"
    timed_lyrics_json = "timed_lyrics_json"
    cover_art = "cover_art"


class MusicPerformerRole(str, Enum):
    lead = "lead"
    harmony = "harmony"
    rap = "rap"
    backing = "backing"
    adlib = "adlib"
    narration = "narration"


class VoiceMode(str, Enum):
    uploaded = "uploaded"
    generated = "generated"
    none = "none"


class MusicJobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class MusicJobStage(str, Enum):
    intent = "intent"
    creative_brief = "creative_brief"
    lyrics = "lyrics"
    arrangement = "arrangement"
    provider_route = "provider_route"
    generate_audio = "generate_audio"
    align_lyrics = "align_lyrics"
    generate_performer_videos = "generate_performer_videos"
    compose_video = "compose_video"
    qc = "qc"
    publish = "publish"