from __future__ import annotations

from enum import Enum


class CommerceMode(str, Enum):
    platform_models = "platform_models"
    customer_tryon = "customer_tryon"


class CommerceProductType(str, Enum):
    apparel = "apparel"
    fmcg = "fmcg"
    electronics = "electronics"
    mixed = "mixed"


class CommerceChannel(str, Enum):
    instagram = "instagram"
    facebook = "facebook"
    whatsapp = "whatsapp"
    tiktok = "tiktok"
    youtube = "youtube"


class MarketplaceAddon(str, Enum):
    amazon = "amazon"
    flipkart = "flipkart"


class Resolution(str, Enum):
    hd = "hd"         # 1080p-ish
    hi_res = "hi_res" # upscaled / higher quality