from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

try:
    from PIL import Image, ImageOps, ImageStat
except Exception as e:  # pragma: no cover
    Image = None  # type: ignore
    ImageOps = None  # type: ignore
    ImageStat = None  # type: ignore


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _require_pillow() -> None:
    if Image is None or ImageOps is None or ImageStat is None:
        raise RuntimeError("Pillow is required for quality gates")


def _open_image(b: bytes):
    _require_pillow()
    im = Image.open(io.BytesIO(b))
    im.load()
    im = ImageOps.exif_transpose(im)
    return im


def _load_image_bytes(b: bytes) -> Tuple[int, int, str]:
    im = _open_image(b)
    w, h = im.size
    mode = im.mode
    return w, h, mode


def _downsample_gray(b: bytes, size: int = 64) -> Optional[list]:
    if Image is None:
        return None
    im = _open_image(b).convert("L")
    im = im.resize((size, size))
    return list(im.getdata())


def _mean_abs_diff(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(abs(x - y) for x, y in zip(a, b)) / (255.0 * len(a))


def _image_stddev_norm(b: bytes) -> float:
    """
    Returns grayscale stddev normalized to 0..1.
    Very low values usually indicate blank / near-solid outputs.
    """
    im = _open_image(b).convert("L")
    st = ImageStat.Stat(im)
    sd = float(st.stddev[0] if st.stddev else 0.0)
    return sd / 255.0


def _non_extreme_pixel_ratio(b: bytes, *, black_thresh: int = 8, white_thresh: int = 247) -> float:
    """
    Fraction of grayscale pixels that are not near-black or near-white.
    Useful to catch all-black / all-white / nearly empty renders.
    """
    im = _open_image(b).convert("L").resize((128, 128))
    vals = list(im.getdata())
    if not vals:
        return 0.0
    mid = sum(1 for v in vals if black_thresh < int(v) < white_thresh)
    return float(mid) / float(len(vals))


def _alpha_nonempty_ratio(b: bytes) -> Optional[float]:
    """
    For RGBA/LA images, returns fraction of pixels with alpha > 0.
    """
    im = _open_image(b)
    if "A" not in im.getbands():
        return None
    alpha = im.getchannel("A").resize((128, 128))
    vals = list(alpha.getdata())
    if not vals:
        return 0.0
    nonempty = sum(1 for v in vals if int(v) > 0)
    return float(nonempty) / float(len(vals))


def _safe_aspect_ratio(w: int, h: int) -> float:
    if w <= 0 or h <= 0:
        return 0.0
    return max(float(w) / float(h), float(h) / float(w))


def _center_crop_mad(a_bytes: bytes, b_bytes: bytes, *, size: int = 64, crop_frac: float = 0.7) -> Optional[float]:
    """
    Coarse center-region similarity. Helps detect unchanged outputs while reducing border/background effects.
    """
    if Image is None:
        return None

    def _prep(b: bytes):
        im = _open_image(b).convert("L")
        w, h = im.size
        cw = max(1, int(w * crop_frac))
        ch = max(1, int(h * crop_frac))
        x0 = max(0, (w - cw) // 2)
        y0 = max(0, (h - ch) // 2)
        im = im.crop((x0, y0, x0 + cw, y0 + ch)).resize((size, size))
        return list(im.getdata())

    a = _prep(a_bytes)
    b = _prep(b_bytes)
    return _mean_abs_diff(a, b)


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reasons: Tuple[str, ...]
    metrics: Dict[str, Any]


def evaluate_example(
    *,
    person_bytes: bytes,
    garment_bytes: bytes,
    target_bytes: bytes,
    min_px: int = 256,
    max_aspect_ratio: float = 4.0,
    min_target_stddev: float = 0.015,
    min_target_non_extreme_ratio: float = 0.02,
    min_person_target_mad: float = 0.010,
    min_person_target_center_mad: float = 0.008,
) -> GateResult:
    """
    Lightweight but production-usable gates:
      - images decodable
      - minimum size
      - aspect ratio sanity
      - target not byte-identical to person/garment
      - target not trivially unchanged from person (global + center MAD)
      - target not blank / near-solid / empty-alpha
    """
    reasons = []
    metrics: Dict[str, Any] = {}

    p_sha = sha256_bytes(person_bytes)
    g_sha = sha256_bytes(garment_bytes)
    t_sha = sha256_bytes(target_bytes)

    metrics["sha_person"] = p_sha
    metrics["sha_garment"] = g_sha
    metrics["sha_target"] = t_sha

    if t_sha == p_sha:
        reasons.append("target_identical_to_person_bytes")
    if t_sha == g_sha:
        reasons.append("target_identical_to_garment_bytes")

    try:
        pw, ph, pm = _load_image_bytes(person_bytes)
        gw, gh, gm = _load_image_bytes(garment_bytes)
        tw, th, tm = _load_image_bytes(target_bytes)
        metrics.update(
            {
                "person_w": pw,
                "person_h": ph,
                "person_mode": pm,
                "garment_w": gw,
                "garment_h": gh,
                "garment_mode": gm,
                "target_w": tw,
                "target_h": th,
                "target_mode": tm,
            }
        )
    except Exception as e:
        reasons.append(f"decode_failed:{type(e).__name__}")
        return GateResult(ok=False, reasons=tuple(reasons), metrics=metrics)

    if pw < min_px or ph < min_px:
        reasons.append("person_too_small")
    if gw < min_px or gh < min_px:
        reasons.append("garment_too_small")
    if tw < min_px or th < min_px:
        reasons.append("target_too_small")

    person_ar = _safe_aspect_ratio(pw, ph)
    garment_ar = _safe_aspect_ratio(gw, gh)
    target_ar = _safe_aspect_ratio(tw, th)
    metrics["person_aspect_ratio"] = person_ar
    metrics["garment_aspect_ratio"] = garment_ar
    metrics["target_aspect_ratio"] = target_ar

    if person_ar > max_aspect_ratio:
        reasons.append("person_aspect_ratio_invalid")
    if garment_ar > max_aspect_ratio:
        reasons.append("garment_aspect_ratio_invalid")
    if target_ar > max_aspect_ratio:
        reasons.append("target_aspect_ratio_invalid")

    # blank / near-solid detection
    try:
        target_stddev = _image_stddev_norm(target_bytes)
        target_non_extreme_ratio = _non_extreme_pixel_ratio(target_bytes)
        target_alpha_nonempty_ratio = _alpha_nonempty_ratio(target_bytes)

        metrics["target_stddev_norm"] = target_stddev
        metrics["target_non_extreme_ratio"] = target_non_extreme_ratio
        if target_alpha_nonempty_ratio is not None:
            metrics["target_alpha_nonempty_ratio"] = target_alpha_nonempty_ratio

        if target_stddev < float(min_target_stddev):
            reasons.append("target_low_variance_blankish")
        if target_non_extreme_ratio < float(min_target_non_extreme_ratio):
            reasons.append("target_low_content_blankish")
        if target_alpha_nonempty_ratio is not None and target_alpha_nonempty_ratio <= 0.001:
            reasons.append("target_empty_alpha")
    except Exception as e:
        metrics["blank_check_error"] = f"{type(e).__name__}: {e}"

    # crude similarity vs original person
    p_small = _downsample_gray(person_bytes)
    t_small = _downsample_gray(target_bytes)
    if p_small and t_small:
        mad = _mean_abs_diff(p_small, t_small)
        metrics["person_target_mad64"] = mad
        if mad < float(min_person_target_mad):
            reasons.append("target_too_similar_to_person")

    try:
        center_mad = _center_crop_mad(person_bytes, target_bytes)
        if center_mad is not None:
            metrics["person_target_center_mad64"] = center_mad
            if center_mad < float(min_person_target_center_mad):
                reasons.append("target_center_too_similar_to_person")
    except Exception as e:
        metrics["center_similarity_error"] = f"{type(e).__name__}: {e}"

    ok = len(reasons) == 0
    return GateResult(ok=ok, reasons=tuple(reasons), metrics=metrics)