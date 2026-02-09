from __future__ import annotations

from datetime import datetime
from enum import Enum as PyEnum
from typing import Any, Dict, List, Optional, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from .enums import (
    MusicProjectMode,
    DuetLayout,
    CameraEdit,
    MusicProjectStatus,
    MusicTrackType,
    MusicJobStatus,
    MusicJobStage,
    MusicPerformerRole,
    VoiceMode,
)

# -----------------------------
# Music Projects
# -----------------------------


class MusicProjectOut(BaseModel):
    id: UUID
    title: str
    mode: MusicProjectMode
    duet_layout: DuetLayout
    language_hint: Optional[str] = None
    scene_pack_id: Optional[str] = None
    camera_edit: CameraEdit
    band_pack: List[str] = Field(default_factory=list)
    status: MusicProjectStatus
    created_at: datetime
    updated_at: datetime


class CreateMusicProjectIn(BaseModel):
    title: str = Field(default="Untitled Music Video", max_length=200)
    mode: MusicProjectMode = MusicProjectMode.autopilot
    duet_layout: DuetLayout = DuetLayout.split_screen
    language_hint: Optional[str] = None
    scene_pack_id: Optional[str] = None
    camera_edit: CameraEdit = CameraEdit.beat_cut
    band_pack: List[str] = Field(default_factory=list)


class CreateMusicProjectOut(BaseModel):
    project_id: UUID
    status: MusicProjectStatus = MusicProjectStatus.draft
    created_at: datetime
    updated_at: datetime


class UpdateMusicProjectIn(BaseModel):
    title: Optional[str] = None
    mode: Optional[MusicProjectMode] = None
    duet_layout: Optional[DuetLayout] = None
    language_hint: Optional[str] = None
    scene_pack_id: Optional[str] = None
    camera_edit: Optional[CameraEdit] = None
    band_pack: Optional[List[str]] = None
    status: Optional[MusicProjectStatus] = None


# -----------------------------
# BYO Audio (Upload / Reference)
# -----------------------------


class UpsertMusicAudioIn(BaseModel):
    """
    For MusicProjectMode.byo:
      - store the user's master audio input for the project.
      - lyrics are optional: upload|generate|none
    """
    audio_asset_id: UUID
    kind: Literal["master", "reference"] = "master"

    title: Optional[str] = None
    artist: Optional[str] = None

    lyrics_source: Literal["upload", "generate", "none"] = "none"
    lyrics_text: Optional[str] = None


class UpsertMusicAudioOut(BaseModel):
    project_id: UUID
    audio_asset_id: UUID
    kind: str
    updated_at: datetime


# -----------------------------
# Music Track Generation (audio)
# -----------------------------


class GenerateMusicIn(BaseModel):
    """
    Works for all project modes:
      autopilot: system generates track + (default) lyrics
      co_create: system generates track; lyrics can be uploaded or generated (default generate)
      byo:       uses uploaded audio; lyrics can be none/upload/generate
    """
    seed: Optional[int] = None
    quality: Literal["draft", "standard", "pro"] = "standard"

    outputs: List[MusicTrackType] = Field(default_factory=lambda: [MusicTrackType.full_mix])
    provider_hints: Dict[str, Any] = Field(default_factory=dict)

    # ✅ Track source (mostly relevant for BYO)
    # - if project.mode==byo, server should prefer project master audio (or these overrides)
    uploaded_audio_asset_id: Optional[UUID] = None
    uploaded_audio_url: Optional[str] = None  # optional SAS/direct URL override

    # ✅ Creative controls (autopilot/co_create)
    track_prompt: Optional[str] = None
    genre_hint: Optional[str] = None
    vibe_hint: Optional[str] = None

    # ✅ Lyrics are optional unless timed_lyrics_json requested
    lyrics_source: Optional[Literal["generate", "upload", "none"]] = None
    lyrics_text: Optional[str] = None
    lyrics_language_hint: Optional[str] = None


class GenerateMusicOut(BaseModel):
    job_id: UUID
    status: MusicJobStatus


class TrackItem(BaseModel):
    track_type: MusicTrackType
    artifact_id: Optional[UUID] = None
    media_asset_id: Optional[UUID] = None
    duration_ms: Optional[int] = None

    # optional convenience fields (viewer/mobile)
    url: Optional[str] = None
    content_type: Optional[str] = None


class MusicJobStatusOut(BaseModel):
    job_id: UUID
    project_id: UUID
    status: MusicJobStatus
    stage: Optional[MusicJobStage] = None
    progress: float = 0.0
    tracks: List[TrackItem] = Field(default_factory=list)
    error: Optional[str] = None


# -----------------------------
# Publish
# -----------------------------


class PublishMusicIn(BaseModel):
    target: Literal["viewer", "fusion"] = "fusion"
    consent: Dict[str, Any] = Field(default_factory=dict)


class PublishMusicOut(BaseModel):
    status: str
    video_job_id: Optional[UUID] = None
    fusion_payload: Optional[Dict[str, Any]] = None


# -----------------------------
# Performers + Lyrics
# -----------------------------


class UpsertMusicPerformerIn(BaseModel):
    role: MusicPerformerRole = MusicPerformerRole.lead
    image_asset_id: UUID
    voice_mode: VoiceMode = VoiceMode.uploaded
    user_is_owner: bool = False


class MusicPerformerOut(BaseModel):
    id: UUID
    project_id: UUID
    role: MusicPerformerRole
    image_asset_id: UUID
    voice_mode: VoiceMode
    user_is_owner: bool
    created_at: datetime

    # Joined from media_assets in repo.get_performers()
    image_url: Optional[str] = None
    image_content_type: Optional[str] = None


class UpsertMusicLyricsIn(BaseModel):
    lyrics_text: str = Field(min_length=1)


class MusicLyricsOut(BaseModel):
    project_id: UUID
    lyrics_text: str
    created_at: datetime
    updated_at: datetime


# -----------------------------
# Music Video Generation
# -----------------------------


class MusicRenderMode(str, PyEnum):
    lipsync = "lipsync"
    montage = "montage"


class VoiceReferenceOut(BaseModel):
    project_id: UUID
    voice_ref_asset_id: UUID
    content_type: str
    bytes: int = Field(ge=0)
    storage_ref: str
    created_at: Optional[datetime] = None


class GenerateMusicVideoIn(BaseModel):
    render_mode: MusicRenderMode = MusicRenderMode.lipsync

    # voice texture (optional for montage; recommended for lipsync)
    voice_ref_asset_id: Optional[UUID] = None

    # ✅ audio input options (pick one)
    audio_track_media_asset_id: Optional[UUID] = None
    audio_track_artifact_id: Optional[UUID] = None
    audio_master_url: Optional[str] = None

    # ✅ captions/lyrics behavior
    # - "project": use stored project lyrics if present
    # - "upload": use lyrics_text below
    # - "none": no captions (and no timed_lyrics_json export)
    # - "auto_transcribe": reserved for future (server may ignore today)
    lyrics_source: Literal["project", "upload", "none", "auto_transcribe"] = "project"
    lyrics_text: Optional[str] = None

    burn_captions: bool = True

    mood: Optional[str] = None
    scene_prompt: Optional[str] = None

    exports: List[str] = Field(default_factory=lambda: ["9:16"])
    quality: Literal["draft", "standard", "pro"] = "standard"
    seed: Optional[int] = None
    provider_hints: Dict[str, Any] = Field(default_factory=dict)


class GenerateMusicVideoOut(BaseModel):
    job_id: UUID
    status: MusicJobStatus