from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

from PIL import Image
from azure.storage.blob import BlobServiceClient

from app.db import get_pool


def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _ref_to_container_blob(ref: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    c = (ref.get("container") or "").strip()
    b = (ref.get("blob") or "").strip()
    if c and b:
        return c, b

    url = (ref.get("url") or "").strip()
    if not url:
        return None

    if url.startswith("az://"):
        rest = url[len("az://") :]
        if "/" not in rest:
            return None
        return tuple(rest.split("/", 1))  # (container, blob)

    # https://<acct>.blob.core.windows.net/<container>/<blob>?...
    try:
        after = url.split("://", 1)[1]
        parts = after.split("/", 2)
        container = parts[1]
        blob = parts[2].split("?", 1)[0]
        return container, blob
    except Exception:
        return None


def _download_blob(svc: BlobServiceClient, container: str, blob: str, dst: Path) -> int:
    bc = svc.get_blob_client(container=container, blob=blob)
    data = bc.download_blob().readall()
    dst.write_bytes(data)
    return len(data)


def _contact_sheet(img_paths: List[Path], cols: int, out: Path) -> None:
    imgs = [Image.open(p).convert("RGB") for p in img_paths if p.exists()]
    if not imgs:
        return
    w, h = imgs[0].width, imgs[0].height
    rows = (len(imgs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w, rows * h), (0, 0, 0))
    for idx, im in enumerate(imgs):
        r, c = divmod(idx, cols)
        sheet.paste(im, (c * w, r * h))
    sheet.save(out, quality=92)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset_id", required=True)
    ap.add_argument("--n", type=int, default=64)
    ap.add_argument("--out_dir", default="/tmp/saree_qc")
    ap.add_argument("--cols", type=int, default=8)
    args = ap.parse_args()

    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn:
        raise SystemExit("AZURE_STORAGE_CONNECTION_STRING not set")

    svc = BlobServiceClient.from_connection_string(conn)
    pool = await get_pool()

    out_root = Path(args.out_dir) / args.dataset_id
    out_root.mkdir(parents=True, exist_ok=True)

    rows = await pool.fetch(
        """
        select id, person_ref, garment_refs, conditioning_refs, target_ref
        from training_examples
        where dataset_id=$1::uuid
        order by random()
        limit $2
        """,
        args.dataset_id,
        args.n,
    )

    summary = {
        "dataset_id": args.dataset_id,
        "requested_n": args.n,
        "sampled_n": len(rows),
        "missing": {"target": 0},
        "targets_contact_sheet": None,
        "targets": [],
    }

    target_paths: List[Path] = []

    for i, r in enumerate(rows):
        ex_id = str(r["id"])
        ex_dir = out_root / f"{i:03d}_{ex_id}"
        ex_dir.mkdir(parents=True, exist_ok=True)

        target_ref = _as_dict(r["target_ref"] or {})
        target_cb = _ref_to_container_blob(target_ref)
        if not target_cb:
            summary["missing"]["target"] += 1
            continue

        dst = ex_dir / "target.png"
        _download_blob(svc, target_cb[0], target_cb[1], dst)
        target_paths.append(dst)
        summary["targets"].append({"id": ex_id, "path": str(dst)})

    sheet = out_root / "targets_contact_sheet.jpg"
    _contact_sheet(target_paths, cols=args.cols, out=sheet)
    if sheet.exists():
        summary["targets_contact_sheet"] = str(sheet)

    (out_root / "qc_summary.json").write_text(json.dumps(summary, indent=2))
    print("QC_DIR=", str(out_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
