from enum import Enum


class LongformJobStatus(str, Enum):
    queued = "queued"
    running = "running"
    stitching = "stitching"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


class SegmentStatus(str, Enum):
    queued = "queued"
    tts_pending = "tts_pending"
    video_pending = "video_pending"
    succeeded = "succeeded"
    failed = "failed"


# -------------------------------------------------------------------
# NEW: orchestration mode
# -------------------------------------------------------------------

class LongformMode(str, Enum):
    legacy = "legacy"       # existing chunk -> render -> stitch flow
    directed = "directed"   # new intent/scenario/shot-planned flow


# -------------------------------------------------------------------
# NEW: internal stage machine
# Keep LongformJobStatus untouched for DB/API compatibility.
# -------------------------------------------------------------------

class LongformStage(str, Enum):
    queued = "queued"
    normalizing = "normalizing"
    scenario_planning = "scenario_planning"
    story_planning = "story_planning"
    shot_planning = "shot_planning"
    asset_resolution = "asset_resolution"
    rendering = "rendering"
    composing = "composing"
    quality_check = "quality_check"
    repairing = "repairing"
    stitching = "stitching"
    finalizing = "finalizing"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


# -------------------------------------------------------------------
# NEW: scenario / situation types
# -------------------------------------------------------------------

class ScenarioType(str, Enum):
    auto = "auto"
    founder_story = "founder_story"
    product_explainer = "product_explainer"
    campaign_promo = "campaign_promo"
    testimonial = "testimonial"
    educational_walkthrough = "educational_walkthrough"
    brand_film = "brand_film"
    commerce_showcase = "commerce_showcase"
    launch_announcement = "launch_announcement"
    festive_campaign = "festive_campaign"


# -------------------------------------------------------------------
# NEW: shot grammar for cinematic longform
# -------------------------------------------------------------------

class ShotType(str, Enum):
    hook_open = "hook_open"
    talking_head = "talking_head"
    voiceover_broll = "voiceover_broll"
    montage = "montage"
    title_card = "title_card"
    quote_card = "quote_card"
    stat_card = "stat_card"
    product_showcase = "product_showcase"
    screen_demo = "screen_demo"
    transition_bridge = "transition_bridge"
    social_proof = "social_proof"
    outro_cta = "outro_cta"
    logo_sting = "logo_sting"


# -------------------------------------------------------------------
# NEW: how a shot gets rendered
# -------------------------------------------------------------------

class RenderRoute(str, Enum):
    fusion = "fusion"
    internal_card = "internal_card"
    internal_montage = "internal_montage"
    audio_broll = "audio_broll"
    imported_asset = "imported_asset"
    legacy_segment_pipeline = "legacy_segment_pipeline"


# -------------------------------------------------------------------
# NEW: QC / repair decisions
# -------------------------------------------------------------------

class QcDecision(str, Enum):
    accept = "accept"
    repair_shot = "repair_shot"
    repair_section = "repair_section"
    insert_hook = "insert_hook"
    insert_cta = "insert_cta"
    rebalance_pacing = "rebalance_pacing"
    fail = "fail"