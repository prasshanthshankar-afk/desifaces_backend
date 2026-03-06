#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont


@dataclass
class Row:
    id: str
    split: str
    composite_url: str
    target_url: str


def _safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _hash_url(u: str) -> str:
    return hashlib.sha256(u.encode("utf-8")).hexdigest()[:16]


def _download(url: str, out: Path, timeout: int = 30) -> None:
    if out.exists() and out.stat().st_size > 0:
        return
    req = Request(url, headers={"User-Agent": "desifaces-eval/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(out)


def _open_rgb(path: Path) -> Image.Image:
    return Image.open(path).convert("RGB")


def _fit(im: Image.Image, w: int, h: int) -> Image.Image:
    im = im.copy()
    im.thumbnail((w, h))
    canvas = Image.new("RGB", (w, h), (18, 18, 18))
    x = (w - im.width) // 2
    y = (h - im.height) // 2
    canvas.paste(im, (x, y))
    return canvas


def _read_csv(csv_path: Path, limit: int) -> List[Row]:
    rows: List[Row] = []
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, r in enumerate(reader):
            if i >= limit:
                break
            rows.append(
                Row(
                    id=str(r.get("id") or "").strip(),
                    split=str(r.get("split") or "").strip(),
                    composite_url=str(r.get("composite_url") or "").strip(),
                    target_url=str(r.get("target_url") or "").strip(),
                )
            )
    return rows


def _make_pair(
    idx: int,
    row: Row,
    comp_path: Path,
    targ_path: Path,
    out_pair: Path,
    cell_w: int,
    cell_h: int,
    font: Optional[ImageFont.ImageFont],
) -> None:
    comp = _fit(_open_rgb(comp_path), cell_w, cell_h)
    targ = _fit(_open_rgb(targ_path), cell_w, cell_h)

    pair_w = cell_w * 2
    pair_h = cell_h + 48
    pair = Image.new("RGB", (pair_w, pair_h), (10, 10, 10))
    pair.paste(comp, (0, 48))
    pair.paste(targ, (cell_w, 48))

    d = ImageDraw.Draw(pair)
    d.text((10, 10), f"{idx:03d} id={row.id} split={row.split}", fill=(235, 235, 235), font=font)
    d.text((10, 28), "LEFT=composite  RIGHT=target", fill=(200, 200, 200), font=font)

    pair.save(out_pair, quality=92)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out_dir", default="/tmp/saree_eval_bundle")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--cell_w", type=int, default=512)
    ap.add_argument("--cell_h", type=int, default=512)
    ap.add_argument("--sheet_cols", type=int, default=3)
    args = ap.parse_args()

    csv_path = Path(args.csv).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    _safe_mkdir(out_dir)

    dl_dir = out_dir / "download"
    pairs_dir = out_dir / "pairs"
    sheets_dir = out_dir / "sheets"
    _safe_mkdir(dl_dir)
    _safe_mkdir(pairs_dir)
    _safe_mkdir(sheets_dir)

    rows = _read_csv(csv_path, args.limit)
    if not rows:
        print(f"❌ No rows found in {csv_path}", file=sys.stderr)
        raise SystemExit(2)

    font: Optional[ImageFont.ImageFont] = None
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    tasks: List[Tuple[int, Row, Path, Path]] = []
    for i, r in enumerate(rows):
        comp_path = dl_dir / f"{i:03d}_comp_{_hash_url(r.composite_url)}.jpg"
        targ_path = dl_dir / f"{i:03d}_targ_{_hash_url(r.target_url)}.jpg"
        tasks.append((i, r, comp_path, targ_path))

    print(f"⬇️ downloading {len(tasks)*2} images to {dl_dir} ...")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = []
        for i, r, comp_path, targ_path in tasks:
            futs.append(ex.submit(_download, r.composite_url, comp_path))
            futs.append(ex.submit(_download, r.target_url, targ_path))
        for f in as_completed(futs):
            _ = f.result()

    print(f"🧩 building pairs in {pairs_dir} ...")
    for i, r, comp_path, targ_path in tasks:
        out_pair = pairs_dir / f"pair_{i:03d}_{r.id or 'noid'}.jpg"
        _make_pair(i, r, comp_path, targ_path, out_pair, args.cell_w, args.cell_h, font)

    pair_paths = sorted(pairs_dir.glob("pair_*.jpg"))
    cols = max(1, args.sheet_cols)
    rows_per_sheet = cols * cols
    sheet_idx = 0

    for start in range(0, len(pair_paths), rows_per_sheet):
        chunk = pair_paths[start : start + rows_per_sheet]
        grid_rows = (len(chunk) + cols - 1) // cols

        first = Image.open(chunk[0]).convert("RGB")
        pw, ph = first.size

        sheet = Image.new("RGB", (pw * cols, ph * grid_rows), (0, 0, 0))
        for j, p in enumerate(chunk):
            im = Image.open(p).convert("RGB")
            rr = j // cols
            cc = j % cols
            sheet.paste(im, (cc * pw, rr * ph))

        sheet_path = sheets_dir / f"sheet_{sheet_idx:02d}.jpg"
        sheet.save(sheet_path, quality=92)
        sheet_idx += 1

    idx_html = out_dir / "index.html"
    with idx_html.open("w", encoding="utf-8") as f:
        f.write("<html><head><meta charset='utf-8'><title>Saree Eval Bundle</title></head><body>\n")
        f.write(f"<h2>Saree Eval Bundle</h2><p>CSV: {csv_path}</p>\n")
        f.write("<h3>Sheets</h3>\n")
        for sp in sorted(sheets_dir.glob("sheet_*.jpg")):
            rel = sp.relative_to(out_dir)
            f.write(f"<div><a href='{rel}'><img src='{rel}' style='max-width:1200px;'></a></div><br/>\n")
        f.write("<h3>Pairs</h3>\n")
        for pp in pair_paths:
            rel = pp.relative_to(out_dir)
            f.write(f"<div><a href='{rel}'>{rel}</a></div>\n")
        f.write("</body></html>\n")

    print(f"✅ Eval bundle ready: {out_dir}")
    print(f"   open: {idx_html}")


if __name__ == "__main__":
    main()
