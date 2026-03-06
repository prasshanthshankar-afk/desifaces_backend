from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

try:
    from PIL import Image
except Exception as e:  # pragma: no cover
    Image = None  # type: ignore


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _load_image_bytes(b: bytes) -> Tuple[int, int, str]:
    if Image is None:
        raise RuntimeError("Pillow is required for quality gates")
    im = Image.open(io.BytesIO(b))
    im.load()
    w, h = im.size
    mode = im.mode
    return w, h, mode


def _downsample_gray(b: bytes, size: int = 64) -> Optional[list]:
    if Image is None:
        return None
    im = Image.open(io.BytesIO(b)).convert("L")
    im = im.resize((size, size))
    return list(im.getdata())


def _mean_abs_diff(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(abs(x - y) for x, y in zip(a, b)) / (255.0 * len(a))


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
) -> GateResult:
    """
    Very lightweight gates:
      - images decodable
      - minimum size
      - target not byte-identical to person/garment
      - target not trivially identical to person (low-res MAD)
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

    if tw < min_px or th < min_px:
        reasons.append("target_too_small")

    # crude similarity vs person
    p_small = _downsample_gray(person_bytes)
    t_small = _downsample_gray(target_bytes)
    if p_small and t_small:
        mad = _mean_abs_diff(p_small, t_small)
        metrics["person_target_mad64"] = mad
        if mad < 0.01:
            reasons.append("target_too_similar_to_person")

    ok = len(reasons) == 0
    return GateResult(ok=ok, reasons=tuple(reasons), metrics=metrics)