from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont


@dataclass
class SlideResult:
    path: str
    width: int
    height: int


def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except Exception:
        return ImageFont.load_default()


def compose_slide(
    out_path: str,
    headline: str,
    lines: List[str],
    bg_path: Optional[str] = None,
    size: Tuple[int, int] = (1080, 1920),
) -> SlideResult:
    """
    Creates a single IG/Shorts-friendly slide image.

    - out_path: output PNG file path
    - headline: large title text
    - lines: bullet lines (max ~6 will be rendered)
    - bg_path: optional background image path
    - size: (width, height) e.g. (1080,1920) story/reel, (1080,1350) feed carousel
    """
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    W, H = size

    if bg_path and os.path.exists(bg_path):
        img = Image.open(bg_path).convert("RGB").resize((W, H))
    else:
        img = Image.new("RGB", (W, H), color=(10, 10, 14))

    draw = ImageDraw.Draw(img)
    margin = 120
    y = margin

    hf = _font(72)
    draw.text((margin, y), headline, font=hf, fill=(255, 255, 255))
    y += 120

    bf = _font(44)
    for line in lines[:6]:
        txt = line.strip()
        if not txt:
            continue
        draw.text((margin, y), f"• {txt}", font=bf, fill=(230, 230, 230))
        y += 70

    wf = _font(36)
    draw.text((margin, H - margin), "desifaces.ai", font=wf, fill=(200, 200, 200))

    img.save(out_path, format="PNG")
    return SlideResult(path=out_path, width=W, height=H)