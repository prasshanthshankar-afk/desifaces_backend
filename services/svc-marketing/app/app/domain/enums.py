from __future__ import annotations
from enum import Enum


class MarketingRunStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class MarketingRunMode(str, Enum):
    stage = "stage"
    publish = "publish"


class RecipeKind(str, Enum):
    face_audio_video = "FACE_AUDIO_VIDEO"
    face_music_musicvideo = "FACE_MUSIC_MUSICVIDEO"
    face_catalog_product_promo = "FACE_CATALOG_PRODUCT_PROMO"


class AssetKind(str, Enum):
    reel_mp4 = "reel_mp4"
    reel_cover_png = "reel_cover_png"
    story_mp4 = "story_mp4"
    story_png = "story_png"
    slide_01_png = "slide_01_png"
    slide_02_png = "slide_02_png"
    caption_txt = "caption_txt"
    manifest_json = "manifest_json"


class Persona(str, Enum):
    creator = "creator"
    smb = "smb"
    user = "user"