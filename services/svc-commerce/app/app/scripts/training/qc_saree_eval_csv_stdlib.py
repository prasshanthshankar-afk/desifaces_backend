#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Dict, List
from urllib.request import Request, urlopen

from PIL import Image


@dataclass
class QCResult:
    ok: bool
    reason: str
    w: int = 0
    h: int = 0
    aspect: float = 0.0


def _fetch_image(url: str, timeout: int = 25) -> Image.Image:
    req = Request(url, headers={"User-Agent": "desifaces-qc/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return Image.open(BytesIO(data)).convert("RGB")


def _ahash(im: Image.Image, size: int = 8) -> int:
    g = im.convert("L").resize((size, size))
    px = list(g.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for i, v in enumerate(px):
        if v > avg:
            bits |= 1 << i
    return bits


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _qc_pair(comp: Image.Image, targ: Image.Image, min_dim: int) -> QCResult:
    tw, th = targ.size
    aspect = tw / max(th, 1)

    if tw < min_dim or th < min_dim:
        return QCResult(False, f"target_too_small({tw}x{th})", tw, th, aspect)

    # full-body targets should be portrait-ish
    if aspect > 0.95:
        return QCResult(False, f"target_not_portrait(aspect={aspect:.2f})", tw, th, aspect)

    # reject near-identical comp/target (echo bug)
    try:
        d = _hamming(_ahash(comp), _ahash(targ))
        if d <= 4:
            return QCResult(False, f"comp_target_too_similar(ahash_d={d})", tw, th, aspect)
    except Exception:
        pass

    return QCResult(True, "ok", tw, th, aspect)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out_dir", default="/tmp/saree_eval_qc")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--min_dim", type=int, default=512)
    args = ap.parse_args()

    csv_path = Path(args.csv).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: List[Dict[str, str]] = []
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        for i, r in enumerate(rd):
            if i >= args.limit:
                break
            rows.append({k: (v or "").strip() for k, v in r.items()})

    if not rows:
        raise SystemExit("no rows")

    pass_csv = out_dir / "qc_pass.csv"
    fail_csv = out_dir / "qc_fail.csv"
    fields = list(rows[0].keys()) + ["qc_reason", "target_w", "target_h", "target_aspect"]

    passed = failed = 0
    with pass_csv.open("w", newline="", encoding="utf-8") as fp, fail_csv.open("w", newline="", encoding="utf-8") as ff:
        wp = csv.DictWriter(fp, fieldnames=fields)
        wf = csv.DictWriter(ff, fieldnames=fields)
        wp.writeheader()
        wf.writeheader()

        for r in rows:
            comp_url = r.get("composite_url", "")
            targ_url = r.get("target_url", "")
            try:
                comp = _fetch_image(comp_url)
                targ = _fetch_image(targ_url)
                qc = _qc_pair(comp, targ, args.min_dim)
            except Exception as e:
                qc = QCResult(False, f"fetch_or_decode_failed({type(e).__name__})")

            out = dict(r)
            out["qc_reason"] = qc.reason
            out["target_w"] = str(qc.w)
            out["target_h"] = str(qc.h)
            out["target_aspect"] = f"{qc.aspect:.3f}"

            if qc.ok:
                wp.writerow(out)
                passed += 1
            else:
                wf.writerow(out)
                failed += 1

    print(f"✅ QC done: passed={passed} failed={failed}")
    print(f"pass: {pass_csv}")
    print(f"fail: {fail_csv}")


if __name__ == "__main__":
    main()
