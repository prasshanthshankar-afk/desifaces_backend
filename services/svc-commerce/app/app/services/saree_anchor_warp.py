# services/svc-commerce/app/app/services/saree_anchor_warp.py
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from PIL import Image


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _alpha_bbox(overlay_rgba: Image.Image, alpha_thresh: int = 10) -> Optional[Tuple[int, int, int, int]]:
    """Return bbox (x0,y0,x1,y1) of alpha>threshold. x1/y1 are exclusive."""
    ov = overlay_rgba.convert("RGBA")
    a = ov.split()[3]
    bbox = a.point(lambda p: 255 if p > alpha_thresh else 0).getbbox()
    return bbox  # may be None


def _apply_head_cutoff(
    overlay_rgba: Image.Image,
    *,
    y_cut: int,
    fade_px: int = 18,
) -> Image.Image:
    """
    Zero alpha above y_cut. Add a small fade band to avoid harsh seam.
    """
    ov = overlay_rgba.convert("RGBA")
    w, h = ov.size
    y_cut = int(_clamp(float(y_cut), 0.0, float(h)))

    if y_cut <= 0:
        return ov

    pix = ov.load()
    for y in range(0, min(h, y_cut)):
        # Fade zone: from (y_cut-fade_px .. y_cut)
        if fade_px > 0 and y >= max(0, y_cut - fade_px):
            t = (y - (y_cut - fade_px)) / float(max(1, fade_px))
            # t in [0..1], alpha multiplier in [0..1]
            mult = t
        else:
            mult = 0.0

        for x in range(w):
            r, g, b, a = pix[x, y]
            if a == 0:
                continue
            na = int(a * mult)
            pix[x, y] = (r, g, b, na)

    return ov


def _solve_perspective_coeffs(
    src_pts: Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float], Tuple[float, float]],
    dst_pts: Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float], Tuple[float, float]],
) -> Tuple[float, float, float, float, float, float, float, float]:
    """
    Compute PIL perspective transform coeffs that map output (dst) -> input (src).
    Returns 8 coeffs for Image.transform(..., Image.PERSPECTIVE, coeffs).

    Implementation: solve 8x8 linear system with Gaussian elimination (no numpy dependency).
    """
    # Build system Ax=b
    # For each correspondence (x,y) in dst -> (u,v) in src:
    # u = (a*x + b*y + c) / (g*x + h*y + 1)
    # v = (d*x + e*y + f) / (g*x + h*y + 1)
    A = [[0.0] * 8 for _ in range(8)]
    B = [0.0] * 8

    def row(i: int, x: float, y: float, u: float, v: float) -> None:
        A[2 * i + 0] = [x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y]
        B[2 * i + 0] = u
        A[2 * i + 1] = [0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y]
        B[2 * i + 1] = v

    for i in range(4):
        x, y = dst_pts[i]
        u, v = src_pts[i]
        row(i, x, y, u, v)

    # Gaussian elimination
    n = 8
    # Augment
    M = [A[i] + [B[i]] for i in range(n)]

    for col in range(n):
        # pivot
        pivot = col
        for r in range(col + 1, n):
            if abs(M[r][col]) > abs(M[pivot][col]):
                pivot = r
        if abs(M[pivot][col]) < 1e-12:
            # Singular; fallback to identity-ish
            return (1, 0, 0, 0, 1, 0, 0, 0)

        if pivot != col:
            M[col], M[pivot] = M[pivot], M[col]

        # normalize pivot row
        div = M[col][col]
        for c in range(col, n + 1):
            M[col][c] /= div

        # eliminate
        for r in range(n):
            if r == col:
                continue
            factor = M[r][col]
            if abs(factor) < 1e-12:
                continue
            for c in range(col, n + 1):
                M[r][c] -= factor * M[col][c]

    sol = [M[i][n] for i in range(n)]
    return (sol[0], sol[1], sol[2], sol[3], sol[4], sol[5], sol[6], sol[7])


def _warp_overlay_to_quad(
    overlay_rgba: Image.Image,
    *,
    dst_quad: Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float], Tuple[float, float]],
    out_size: Tuple[int, int],
    alpha_bbox: Optional[Tuple[int, int, int, int]],
) -> Image.Image:
    """
    Warp overlay (using its alpha bbox as source quad) into dst_quad in output image space.
    """
    ov = overlay_rgba.convert("RGBA")
    W, H = out_size

    # Determine source quad from alpha bbox (fallback to full image)
    if alpha_bbox is None:
        x0, y0, x1, y1 = 0, 0, ov.size[0], ov.size[1]
    else:
        x0, y0, x1, y1 = alpha_bbox

    # Source quad corners in overlay image
    src_quad = (
        (float(x0), float(y0)),
        (float(x1), float(y0)),
        (float(x1), float(y1)),
        (float(x0), float(y1)),
    )

    coeffs = _solve_perspective_coeffs(src_quad, dst_quad)

    # Warp into full-size canvas
    warped = ov.transform(
        (int(W), int(H)),
        Image.PERSPECTIVE,
        coeffs,
        resample=Image.BICUBIC,
    )
    return warped


def _pose_landmarks(base_rgb: Image.Image) -> Optional[Dict[str, Tuple[float, float]]]:
    """
    Best-effort MediaPipe Pose landmarks.
    Returns dict of keypoints in pixel coords, or None if MP not available / no pose found.
    """
    try:
        import mediapipe as mp  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return None

    img = base_rgb.convert("RGB")
    w, h = img.size
    arr = np.array(img)

    mp_pose = mp.solutions.pose
    with mp_pose.Pose(static_image_mode=True, model_complexity=1, enable_segmentation=False) as pose:
        res = pose.process(arr)
        if not res.pose_landmarks:
            return None

        lm = res.pose_landmarks.landmark

        def pt(idx: int) -> Tuple[float, float]:
            return (float(lm[idx].x) * w, float(lm[idx].y) * h)

        # indices: https://developers.google.com/mediapipe/solutions/vision/pose_landmarker
        # We'll use common ones:
        NOSE = 0
        L_SHOULDER = 11
        R_SHOULDER = 12
        L_HIP = 23
        R_HIP = 24
        L_ANKLE = 27
        R_ANKLE = 28

        return {
            "nose": pt(NOSE),
            "ls": pt(L_SHOULDER),
            "rs": pt(R_SHOULDER),
            "lh": pt(L_HIP),
            "rh": pt(R_HIP),
            "la": pt(L_ANKLE),
            "ra": pt(R_ANKLE),
        }


def maybe_anchor_warp_saree_overlay(
    base_rgb: Image.Image,
    overlay_rgba: Image.Image,
    context: Optional[Dict[str, Any]] = None,
):
    """
    Warps the overlay into a saree-like drape region.
    Returns (overlay_warped_rgba, debug_dict)

    context keys (optional):
      - layer: "saree" or "pallu" (default "saree")
      - drape_style: e.g. "nivi"
      - variant_idx: int
    """
    ctx = dict(context or {})
    layer = str(ctx.get("layer") or "saree").strip().lower()
    drape_style = str(ctx.get("drape_style") or "nivi").strip().lower()

    w, h = base_rgb.size
    dbg: Dict[str, Any] = {
        "ok": False,
        "layer": layer,
        "drape_style": drape_style,
        "used_pose": False,
        "dst_quad": None,
        "head_cut_y": None,
        "fallback": None,
    }

    ov = overlay_rgba.convert("RGBA")

    # Identify source alpha bbox (saree area on the template render)
    bbox = _alpha_bbox(ov, alpha_thresh=10)
    dbg["alpha_bbox"] = list(bbox) if bbox else None

    # 1) Get pose landmarks if possible
    lm = _pose_landmarks(base_rgb)
    if lm:
        dbg["used_pose"] = True
        # Keypoints
        nose_x, nose_y = lm["nose"]
        ls_x, ls_y = lm["ls"]
        rs_x, rs_y = lm["rs"]
        lh_x, lh_y = lm["lh"]
        rh_x, rh_y = lm["rh"]
        la_x, la_y = lm["la"]
        ra_x, ra_y = lm["ra"]

        # Head cutoff just below nose
        head_cut_y = int(_clamp(nose_y + 0.08 * h, 0.18 * h, 0.35 * h))
        dbg["head_cut_y"] = int(head_cut_y)
        ov = _apply_head_cutoff(ov, y_cut=head_cut_y, fade_px=22)

        # Body extents (robust)
        hip_y = min(lh_y, rh_y)
        ankle_y = max(la_y, ra_y)
        shoulder_y = min(ls_y, rs_y)

        # Compute dst quad by layer
        if layer == "pallu":
            # Pallu: from shoulder band down to hips across chest
            top_y = shoulder_y - 0.03 * h
            bot_y = hip_y + 0.10 * h

            left_x = min(ls_x, rs_x) - 0.10 * w
            right_x = max(lh_x, rh_x) + 0.10 * w

            # Slight tilt: left shoulder to right hip
            dst = (
                (_clamp(left_x, 0, w - 1), _clamp(top_y, 0, h - 1)),
                (_clamp(right_x, 0, w - 1), _clamp(top_y + 0.06 * h, 0, h - 1)),
                (_clamp(right_x - 0.04 * w, 0, w - 1), _clamp(bot_y, 0, h - 1)),
                (_clamp(left_x + 0.02 * w, 0, w - 1), _clamp(bot_y - 0.04 * h, 0, h - 1)),
            )
        else:
            # Saree body: from hips down to ankles, slightly covering waist
            top_y = hip_y - 0.06 * h
            bot_y = ankle_y + 0.02 * h

            # widen around hips
            left_x = min(lh_x, rh_x) - 0.12 * w
            right_x = max(lh_x, rh_x) + 0.12 * w

            dst = (
                (_clamp(left_x, 0, w - 1), _clamp(top_y, 0, h - 1)),
                (_clamp(right_x, 0, w - 1), _clamp(top_y, 0, h - 1)),
                (_clamp(right_x - 0.05 * w, 0, w - 1), _clamp(bot_y, 0, h - 1)),
                (_clamp(left_x + 0.05 * w, 0, w - 1), _clamp(bot_y, 0, h - 1)),
            )

        dbg["dst_quad"] = [(float(x), float(y)) for (x, y) in dst]
        warped = _warp_overlay_to_quad(ov, dst_quad=dst, out_size=(w, h), alpha_bbox=bbox)
        dbg["ok"] = True
        return warped, dbg

    # 2) Fallback: deterministic heuristic placement (still better than “sticker on face”)
    dbg["used_pose"] = False
    dbg["fallback"] = "heuristic_full_body"

    # Head cutoff: prevent overlay on face/head no matter what
    head_cut_y = int(0.22 * h)
    dbg["head_cut_y"] = int(head_cut_y)
    ov = _apply_head_cutoff(ov, y_cut=head_cut_y, fade_px=22)

    if layer == "pallu":
        # chest band
        top_y = 0.18 * h
        bot_y = 0.52 * h
        left_x = 0.18 * w
        right_x = 0.78 * w
        dst = (
            (left_x, top_y),
            (right_x, top_y + 0.06 * h),
            (right_x - 0.05 * w, bot_y),
            (left_x + 0.02 * w, bot_y - 0.04 * h),
        )
    else:
        # skirt/legs
        top_y = 0.45 * h
        bot_y = 0.98 * h
        left_x = 0.22 * w
        right_x = 0.78 * w
        dst = (
            (left_x, top_y),
            (right_x, top_y),
            (right_x - 0.05 * w, bot_y),
            (left_x + 0.05 * w, bot_y),
        )

    dbg["dst_quad"] = [(float(x), float(y)) for (x, y) in dst]
    warped = _warp_overlay_to_quad(
        ov,
        dst_quad=((dst[0][0], dst[0][1]), (dst[1][0], dst[1][1]), (dst[2][0], dst[2][1]), (dst[3][0], dst[3][1])),
        out_size=(w, h),
        alpha_bbox=bbox,
    )
    dbg["ok"] = True
    return warped, dbg