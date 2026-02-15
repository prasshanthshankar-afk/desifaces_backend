from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.enums import CommerceChannel, CommerceMode, CommerceProductType, MarketplaceAddon, Resolution


class QuoteOutputs(BaseModel):
    num_images: int = 0
    num_videos: int = 0


class QuoteViews(BaseModel):
    full_body: bool = True
    half_body: bool = True


class QuoteCTA(BaseModel):
    type: str = "whatsapp"      # whatsapp | url | none
    value: Optional[str] = None # phone number or url


class CommerceQuoteIn(BaseModel):
    mode: CommerceMode
    product_type: CommerceProductType
    # Either look_sets or products; allow both for future (mixed)
    look_set_ids: List[UUID] = Field(default_factory=list)
    product_ids: List[UUID] = Field(default_factory=list)

    outputs: QuoteOutputs = Field(default_factory=QuoteOutputs)
    views: QuoteViews = Field(default_factory=QuoteViews)

    people: List[str] = Field(default_factory=lambda: ["solo_female"])  # solo_female|solo_male|couple|group3
    drape_styles: List[str] = Field(default_factory=list)

    channels: List[CommerceChannel] = Field(default_factory=list)
    marketplaces: List[MarketplaceAddon] = Field(default_factory=list)

    resolution: Resolution = Resolution.hd
    template_pack: str = "default"
    language: str = "en"
    cta: QuoteCTA = Field(default_factory=QuoteCTA)

    provider_policy: str = "auto"  # auto|fal|internal
    currency_hint: str = "USD"     # USD|INR (UI convenience)


class QuoteLineItem(BaseModel):
    type: str
    qty: int
    credits: int


class QuoteBreakdownItem(BaseModel):
    ref_id: str  # SKU or LOOKSET id (string)
    credits: int
    items: List[QuoteLineItem] = Field(default_factory=list)


class CommerceQuoteOut(BaseModel):
    quote_id: UUID
    total_credits: int
    totals: Dict[str, float] = Field(default_factory=dict)  # {"usd": 0.0, "inr": 0.0}
    breakdown: List[QuoteBreakdownItem] = Field(default_factory=list)
    expires_at: datetime
    expires_in_seconds: int = 900
    assumptions: Dict[str, Any] = Field(default_factory=dict)


class CommerceConfirmIn(BaseModel):
    quote_id: UUID
    idempotency_key: Optional[str] = None  # client-generated key for safe retries


class CommerceConfirmOut(BaseModel):
    campaign_id: UUID
    studio_job_id: UUID
    status: str