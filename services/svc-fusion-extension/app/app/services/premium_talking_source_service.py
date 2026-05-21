from __future__ import annotations

import io
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple

import urllib.request


class PremiumSceneDependencyError(RuntimeError):
    pass


def _lazy_import_pillow():
    try:
        from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps
        return Image, ImageChops, ImageDraw, ImageFilter, ImageOps
    except Exception as ex:
        raise PremiumSceneDependencyError(
            "Pillow is required for talking_video premium scene composition. "
            "Install package 'Pillow' in svc-fusion-extension."
        ) from ex


def _lazy_import_rembg():
    try:
        from rembg import remove
        return remove
    except Exception:
        return None


def _download_to_bytes(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "svc-fusion-extension/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _load_image_from_url(url: str):
    Image, _, _, _, ImageOps = _lazy_import_pillow()
    raw = _download_to_bytes(url)
    img = Image.open(io.BytesIO(raw))
    img.load()
    return ImageOps.exif_transpose(img).convert("RGBA")


def _soft_portrait_alpha(size: Tuple[int, int]):
    Image, _, ImageDraw, ImageFilter, _ = _lazy_import_pillow()
    w, h = size
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    # Simple portrait-safe ellipse; avoids hard crash when rembg isn't available.
    left = int(w * 0.12)
    top = int(h * 0.04)
    right = int(w * 0.88)
    bottom = int(h * 0.98)
    d.ellipse((left, top, right, bottom), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(8, int(min(w, h) * 0.03))))
    return mask


def extract_subject_cutout(face_image_url: str, out_png: str) -> str:
    Image, _, _, _, _ = _lazy_import_pillow()
    img = _load_image_from_url(face_image_url)
    remove = _lazy_import_rembg()

    if remove is not None:
        try:
            raw = _download_to_bytes(face_image_url)
            removed = remove(raw)
            out = Image.open(io.BytesIO(removed)).convert("RGBA")
            out.save(out_png, format="PNG")
            return out_png
        except Exception:
            pass

    # Launch-safe fallback: soft portrait matte.
    alpha = _soft_portrait_alpha(img.size)
    out = img.copy()
    out.putalpha(alpha)
    out.save(out_png, format="PNG")
    return out_png


def build_scene_composite(subject_png: str, scene_image_url: str, out_png: str, aspect_ratio: str = "16:9") -> str:
    Image, ImageChops, _, ImageFilter, _ = _lazy_import_pillow()
    bg = _load_image_from_url(scene_image_url)
    fg = Image.open(subject_png).convert("RGBA")

    # Normalize to target canvas
    target = (1280, 720) if aspect_ratio == "16:9" else (1080, 1920) if aspect_ratio == "9:16" else (1080, 1080)
    bg = bg.resize(target, Image.Resampling.LANCZOS)

    # Scale subject proportionally
    max_h = int(target[1] * (0.82 if aspect_ratio == "9:16" else 0.88))
    scale = min((target[0] * 0.62) / fg.width, max_h / fg.height)
    fg = fg.resize((max(1, int(fg.width * scale)), max(1, int(fg.height * scale))), Image.Resampling.LANCZOS)

    # Simple shadow
    shadow = Image.new("RGBA", fg.size, (0, 0, 0, 0))
    alpha = fg.getchannel("A").filter(ImageFilter.GaussianBlur(radius=18))
    shadow.putalpha(alpha.point(lambda p: int(p * 0.45)))
    shadow = ImageChops.offset(shadow, 10, 16)

    # Placement
    x = (target[0] - fg.width) // 2
    y = target[1] - fg.height - (90 if aspect_ratio == "9:16" else 40)

    comp = bg.copy()
    comp.alpha_composite(shadow, (x, y))
    comp.alpha_composite(fg, (x, y))
    comp.save(out_png, format="PNG")
    return out_png
