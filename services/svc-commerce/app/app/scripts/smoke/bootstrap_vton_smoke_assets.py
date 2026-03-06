from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from urllib.request import urlopen, Request

# Azure SDK
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.storage.blob import generate_blob_sas, BlobSasPermissions


# -----------------------------
# Curated smoke sources (internet)
# Use Wikimedia "Special:FilePath/<filename>" so download works reliably.
# -----------------------------
SOURCES: List[Dict[str, str]] = [
    {
        "outfit_kind": "tshirt",
        "component_code": "top",
        "filename": "tshirt_front.png",
        "source_url": "https://commons.wikimedia.org/wiki/Special:FilePath/Rabimas%20Proofreading%20Contest%201427%20Men%27s%20T-shirt%20-%20front%20side.png",
        "license_page": "https://commons.wikimedia.org/wiki/File:Rabimas_Proofreading_Contest_1427_Men%27s_T-shirt_-_front_side.png",
    },
    {
        "outfit_kind": "dress",
        "component_code": "dress",
        "filename": "little_black_dress.jpg",
        "source_url": "https://commons.wikimedia.org/wiki/Special:FilePath/Little%20black%20dress.jpg",
        "license_page": "https://commons.wikimedia.org/wiki/File:Little_black_dress.jpg",
    },
    {
        "outfit_kind": "skirt",
        "component_code": "bottom",
        "filename": "pink_skirt.png",
        "source_url": "https://commons.wikimedia.org/wiki/Special:FilePath/Skirt%20of%20pink%20satin.png",
        "license_page": "https://commons.wikimedia.org/wiki/File:Skirt_of_pink_satin.png",
    },
    {
        "outfit_kind": "kurta",
        "component_code": "top",
        "filename": "mens_kurta.jpg",
        "source_url": "https://commons.wikimedia.org/wiki/Special:FilePath/Men%27s%20kurta%2001.jpg",
        "license_page": "https://commons.wikimedia.org/wiki/File:Men%27s_kurta_01.jpg",
    },
    {
        "outfit_kind": "jacket",
        "component_code": "outerwear",
        "filename": "suit_jacket.jpg",
        "source_url": "https://commons.wikimedia.org/wiki/Special:FilePath/Suit%20jacket.jpg",
        "license_page": "https://commons.wikimedia.org/wiki/File:Suit_jacket.jpg",
    },
    {
        "outfit_kind": "pantsuit",
        "component_code": "set",
        "filename": "trouser_suit.jpg",
        "source_url": "https://commons.wikimedia.org/wiki/Special:FilePath/Trouser%20suit%20MET%20CI60.21.1%20F.jpg",
        "license_page": "https://commons.wikimedia.org/wiki/File:Trouser_suit_MET_CI60.21.1_F.jpg",
    },
]

DEFAULT_CONTAINER = "commerce-training"
DEFAULT_PREFIX = "smoke/vton_assets"


def _guess_content_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".png":
        return "image/png"
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".webp":
        return "image/webp"
    return "application/octet-stream"


def _download(url: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": "desifaces-smoke/1.0"})
    with urlopen(req, timeout=60) as r:
        data = r.read()
    out_path.write_bytes(data)


def _parse_conn_str(conn_str: str) -> Dict[str, str]:
    # Typical: "AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net"
    parts = {}
    for seg in conn_str.split(";"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            parts[k.strip()] = v.strip()
    return parts


def _make_read_sas_url(
    *,
    account_name: str,
    account_key: str,
    container: str,
    blob_name: str,
    ttl_days: int,
) -> str:
    sas = generate_blob_sas(
        account_name=account_name,
        container_name=container,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(days=ttl_days),
    )
    return f"https://{account_name}.blob.core.windows.net/{container}/{blob_name}?{sas}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default=DEFAULT_CONTAINER)
    ap.add_argument("--prefix", default=DEFAULT_PREFIX)
    ap.add_argument("--ttl-days", type=int, default=7)
    ap.add_argument("--out-dir", default="/tmp/df_vton_smoke_assets")
    ap.add_argument("--manifest-dir", default="/tmp/df_vton_smoke_manifests")
    args = ap.parse_args()

    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not conn_str:
        print("ERROR: AZURE_STORAGE_CONNECTION_STRING is not set", file=sys.stderr)
        return 2

    conn = _parse_conn_str(conn_str)
    account_name = conn.get("AccountName")
    account_key = conn.get("AccountKey")
    if not account_name or not account_key:
        print("ERROR: could not parse AccountName/AccountKey from AZURE_STORAGE_CONNECTION_STRING", file=sys.stderr)
        return 2

    bsc = BlobServiceClient.from_connection_string(conn_str)
    cc = bsc.get_container_client(args.container)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_out = Path(args.out_dir) / run_id
    local_out.mkdir(parents=True, exist_ok=True)

    manifest: Dict[str, Any] = {
        "run_id": run_id,
        "container": args.container,
        "prefix": args.prefix,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "items": [],
    }

    for s in SOURCES:
        outfit_kind = s["outfit_kind"]
        filename = s["filename"]
        src_url = s["source_url"]
        license_page = s["license_page"]

        local_path = local_out / outfit_kind / filename
        print(f"[download] {outfit_kind}: {src_url}")
        _download(src_url, local_path)

        blob_name = f"{args.prefix}/{outfit_kind}/{filename}"
        ctype = _guess_content_type(local_path)

        print(f"[upload]   az://{args.container}/{blob_name}")
        with local_path.open("rb") as f:
            cc.upload_blob(
                name=blob_name,
                data=f,
                overwrite=True,
                content_settings=ContentSettings(content_type=ctype),
            )

        sas_url = _make_read_sas_url(
            account_name=account_name,
            account_key=account_key,
            container=args.container,
            blob_name=blob_name,
            ttl_days=args.ttl_days,
        )

        manifest["items"].append(
            {
                "outfit_kind": outfit_kind,
                "component_code": s["component_code"],
                "filename": filename,
                "content_type": ctype,
                "source_url": src_url,
                "license_page": license_page,
                "blob": f"az://{args.container}/{blob_name}",
                "image_url": sas_url,
            }
        )

    mdir = Path(args.manifest_dir) / run_id
    mdir.mkdir(parents=True, exist_ok=True)
    mpath = mdir / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print("\n✅ DONE")
    print(f"MANIFEST={mpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())