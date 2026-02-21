# services/svc-commerce/app/app/domain/models.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, Field

from app.domain.enums import CommerceChannel, CommerceMode, CommerceProductType, MarketplaceAddon, Resolution


# -------------------------
# VTON input envelope
# -------------------------

class ModelRef(BaseModel):
    """
    Explicit model/human reference for VTON.

    Preferred:
      - image_url (or human_image_url)

    Optional future:
      - asset_id: media_assets row (resolver can fetch SAS)
      - platform_model_id: platform model catalog id
    """
    image_url: Optional[AnyHttpUrl] = None
    human_image_url: Optional[AnyHttpUrl] = None

    asset_id: Optional[UUID] = None
    platform_model_id: Optional[str] = None

    # legacy/compat keys (accepted if client sends them)
    url: Optional[AnyHttpUrl] = None
    ref_url: Optional[AnyHttpUrl] = None
    photo_url: Optional[AnyHttpUrl] = None

    meta: Dict[str, Any] = Field(default_factory=dict)


class ProductAssetItem(BaseModel):
    """
    One component in an outfit. Supports multi-item outfits:
      - pant + shirt
      - saree + blouse
      - accessories/jewelry etc.
    """
    component_code: str = Field(..., min_length=1)  # vendor-defined (DB-driven catalog later)
    kind: str = "garment"  # garment|accessory|jewelry|footwear|other

    # Primary image
    image_url: Optional[AnyHttpUrl] = None

    # Optional alternates: back view, flat-lay, closeups, etc.
    image_urls: List[AnyHttpUrl] = Field(default_factory=list)

    # Optional hinting
    is_primary: bool = False
    dominance_rank: Optional[int] = None  # smaller = more dominant (vendor hint)

    # Optional metadata
    display_name: Optional[str] = None
    vendor_sku: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class ProductAssets(BaseModel):
    """
    product_assets.items[] is the primary schema.

    Resolver (commerce_processor.py) will:
      - choose dominant item
      - set garment_image_url
      - set dominant_component_code
    """
    items: List[ProductAssetItem] = Field(default_factory=list)

    # Optional global hints
    product_type: Optional[str] = None
    cloth_type: Optional[str] = None  # upper|lower|dress (provider-specific ok)

    # Canonical resolved keys (client may provide; resolver will populate if items[] present)
    dominant_component_code: Optional[str] = None
    garment_image_url: Optional[AnyHttpUrl] = None

    # Legacy compatibility keys (optional)
    primary_image_url: Optional[AnyHttpUrl] = None
    product_image_url: Optional[AnyHttpUrl] = None
    saree_image_url: Optional[AnyHttpUrl] = None
    blouse_image_url: Optional[AnyHttpUrl] = None

    meta: Dict[str, Any] = Field(default_factory=dict)


# -------------------------
# Existing quote models
# -------------------------

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

    # NEW: VTON/try-on inputs
    product_assets: ProductAssets = Field(default_factory=ProductAssets)
    model_ref: ModelRef = Field(default_factory=ModelRef)


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