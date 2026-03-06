from __future__ import annotations

import os
from typing import Tuple

from PIL import Image


def validate_alpha_mask_png(mask_path: str) -> Tuple[int, int]:
    if not mask_path or not os.path.exists(mask_path):
        raise RuntimeError(f"alpha mask missing: {mask_path}")

    if os.path.getsize(mask_path) < 1024:
        raise RuntimeError(f"alpha mask file too small / invalid: {mask_path}")

    with Image.open(mask_path) as im:
        im = im.convert("RGBA")
        w, h = im.size
        if w < 128 or h < 128:
            raise RuntimeError(f"alpha mask too small ({w}x{h}): {mask_path}")

        a = im.getchannel("A")
        extrema = a.getextrema()  # (min, max)
        # production sanity: must not be fully transparent or fully opaque
        if extrema[1] == 0:
            raise RuntimeError(f"alpha mask is fully transparent (no overlay area): {mask_path}")
        if extrema[0] == 255:
            raise RuntimeError(f"alpha mask is fully opaque (no cutout): {mask_path}")

        return w, h


def apply_alpha_mask_to_overlay(overlay_path: str, mask_path: str) -> None:
    """
    Multiplies overlay alpha by mask alpha (resizing mask if needed).
    In-place write back to overlay_path.
    """
    with Image.open(overlay_path) as ov:
        ov = ov.convert("RGBA")
        ow, oh = ov.size
        oa = ov.getchannel("A")

        with Image.open(mask_path) as mk:
            mk = mk.convert("RGBA")
            if mk.size != (ow, oh):
                mk = mk.resize((ow, oh), resample=Image.BILINEAR)
            ma = mk.getchannel("A")

            # multiply alpha: newA = oa * ma / 255
            oa_bytes = oa.tobytes()
            ma_bytes = ma.tobytes()
            new = bytearray(len(oa_bytes))
            for i in range(len(oa_bytes)):
                new[i] = (oa_bytes[i] * ma_bytes[i]) // 255

            ov.putalpha(Image.frombytes("L", (ow, oh), bytes(new)))
            ov.save(overlay_path, "PNG")