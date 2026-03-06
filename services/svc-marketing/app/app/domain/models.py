from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.domain.enums import MarketingRunMode, RecipeKind, Persona


class UseCaseSpec(BaseModel):
    use_case_id: Optional[UUID] = None
    persona: Persona
    industry: str = Field(..., min_length=2)
    campaign_type: str = Field(..., description="seasonal|product_launch|evergreen|promo_offer")
    recipe: RecipeKind

    # anchors make the output specific
    season_event: Optional[str] = None
    offer: Optional[str] = None
    product_anchor: Optional[str] = None

    target_seconds: int = Field(default=10, ge=6, le=15)
    language_hint: str = "en"

    hook_text: str = Field(..., min_length=4, max_length=120)
    onscreen_lines: List[str] = Field(default_factory=list)
    voiceover_script: str = Field(..., min_length=10, max_length=600)
    music_prompt: Optional[str] = None

    required_assets: Dict[str, Any] = Field(default_factory=dict)
    evidence_ids: List[str] = Field(default_factory=list)


class MarketingRunIn(BaseModel):
    mode: MarketingRunMode = MarketingRunMode.stage
    recipe: RecipeKind

    persona: Optional[Persona] = None
    industry: Optional[str] = None
    tags: List[str] = Field(default_factory=list)

    season_event: Optional[str] = None
    offer: Optional[str] = None
    language_hint: str = "en"

    use_case_id: Optional[UUID] = None
    inputs: Dict[str, Any] = Field(default_factory=dict)
    target_seconds: Optional[int] = Field(default=None, ge=6, le=15)


class MarketingRunOut(BaseModel):
    run_id: UUID
    status: str
    mode: MarketingRunMode
    recipe: RecipeKind
    stage: str
    created_at: str
    updated_at: str
    use_case: Optional[UseCaseSpec] = None
    output: Dict[str, Any] = Field(default_factory=dict)


class MarketingRunStatusOut(BaseModel):
    run_id: UUID
    status: str
    stage: str
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    use_case: Optional[UseCaseSpec] = None
    output: Dict[str, Any] = Field(default_factory=dict)


class ScheduleIn(BaseModel):
    name: str
    enabled: bool = True
    freq: str = Field(..., description="daily|weekly")
    hour: int = Field(..., ge=0, le=23)
    minute: int = Field(..., ge=0, le=59)
    dow: Optional[str] = Field(default=None, description="mon,tue,wed,thu,fri,sat,sun for weekly")

    mode: MarketingRunMode = MarketingRunMode.stage
    recipe: RecipeKind
    persona: Optional[Persona] = None
    industry: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    season_event: Optional[str] = None
    offer: Optional[str] = None
    language_hint: str = "en"
    inputs: Dict[str, Any] = Field(default_factory=dict)
    target_seconds: Optional[int] = Field(default=None, ge=6, le=15)


class ScheduleOut(BaseModel):
    schedule_id: UUID
    name: str
    enabled: bool
    freq: str
    hour: int
    minute: int
    dow: Optional[str] = None
    last_run_at: Optional[str] = None
    created_at: str
    updated_at: str

class UseCaseCreateBase(BaseModel):
    persona: Persona
    industry: str = Field(..., min_length=2)
    recipe: RecipeKind
    campaign_type: str = Field(default="evergreen")
    season_event: Optional[str] = None
    tags: List[str] = Field(default_factory=list)

    product_anchor: Optional[str] = None
    default_offer: Optional[str] = None
    default_seconds: int = Field(default=10, ge=6, le=15)

    default_hook: Optional[str] = None
    base_overlay_lines: List[str] = Field(default_factory=list)
    base_script: Optional[str] = None
    default_music_prompt: Optional[str] = None
    required_assets_json: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None


class UseCaseSuggestIn(BaseModel):
    # Constraints
    persona: Optional[Persona] = None
    industry: Optional[str] = None
    recipe: Optional[RecipeKind] = None
    season_event: Optional[str] = None
    tags: List[str] = Field(default_factory=list)

    # How many suggestions
    count: int = Field(default=5, ge=1, le=20)


class UseCaseSuggestOut(BaseModel):
    suggested_use_case_ids: List[UUID]


class UseCaseApproveIn(BaseModel):
    approved: bool = True


class UseCaseOut(BaseModel):
    use_case_id: UUID
    approved: bool
    source: str
    version: int
    parent_use_case_id: Optional[UUID] = None

    persona: Persona
    industry: str
    recipe: RecipeKind
    campaign_type: str
    season_event: Optional[str] = None
    tags: List[str] = Field(default_factory=list)

    product_anchor: Optional[str] = None
    default_offer: Optional[str] = None
    default_seconds: int

    default_hook: Optional[str] = None
    base_overlay_lines: List[str] = Field(default_factory=list)
    base_script: Optional[str] = None
    default_music_prompt: Optional[str] = None
    required_assets_json: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None

    weight: float = 1.0
    usage_count: int = 0
    last_used_at: Optional[str] = None
    last_metrics_json: Dict[str, Any] = Field(default_factory=dict)

    created_at: str
    updated_at: str


class MetricsIngestIn(BaseModel):
    platform: str = Field(default="instagram")
    media_id: str
    metric_date: str  # YYYY-MM-DD

    impressions: Optional[int] = None
    reach: Optional[int] = None
    plays: Optional[int] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    shares: Optional[int] = None
    saves: Optional[int] = None
    profile_visits: Optional[int] = None
    follows: Optional[int] = None
    watch_time_ms: Optional[int] = None

    raw_json: Dict[str, Any] = Field(default_factory=dict)