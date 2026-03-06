from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from app.services.azure_storage_service import AzureStorageService  # your existing service


@dataclass(frozen=True)
class AzRef:
    container: str
    blob: str


def parse_az_ref(s: str) -> Optional[AzRef]:
    # expected: az://container/path/to/blob.ext
    if not s or not s.startswith("az://"):
        return None
    rest = s[len("az://"):]
    if "/" not in rest:
        return None
    container, blob = rest.split("/", 1)
    container = container.strip()
    blob = blob.strip()
    if not container or not blob:
        return None
    return AzRef(container=container, blob=blob)


def _cache_path(cache_dir: str, container: str, blob: str) -> str:
    os.makedirs(cache_dir, exist_ok=True)
    ext = os.path.splitext(blob)[1].lower() or ".bin"
    key = f"{container}/{blob}".encode("utf-8")
    h = hashlib.sha256(key).hexdigest()[:24]
    return os.path.join(cache_dir, f"{h}{ext}")


async def ensure_local_file(
    storage: AzureStorageService,
    path_or_az: str,
    cache_dir: str,
) -> Tuple[str, bool]:
    """
    Returns (local_path, downloaded_now)
    - local_path is a real file path in the container FS
    - downloaded_now indicates if we fetched it in this call
    """
    az = parse_az_ref(path_or_az)
    if not az:
        # local path
        return path_or_az, False

    local = _cache_path(cache_dir, az.container, az.blob)
    if os.path.exists(local) and os.path.getsize(local) > 0:
        return local, False

    # This assumes your AzureStorageService supports downloading to local path.
    # If your method name differs, adapt here once.
    await storage.download_blob_to_path(container=az.container, blob_name=az.blob, local_path=local)
    if not os.path.exists(local) or os.path.getsize(local) == 0:
        raise RuntimeError(f"Failed to download az://{az.container}/{az.blob} to {local}")
    return local, True