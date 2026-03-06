# services/svc-marketing/app/app/services/orchestration/utils/azure_sas.py
from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger("svc-marketing-azure-sas")

try:
    from azure.storage.blob import generate_blob_sas, BlobSasPermissions  # type: ignore
except Exception:  # pragma: no cover
    generate_blob_sas = None  # type: ignore
    BlobSasPermissions = None  # type: ignore


def _azure_account_key_from_env() -> Optional[str]:
    for k in ("AZURE_STORAGE_ACCOUNT_KEY", "DF_AZURE_STORAGE_ACCOUNT_KEY", "STORAGE_ACCOUNT_KEY"):
        v = os.getenv(k)
        if v:
            return v.strip()

    cs = (
        os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        or os.getenv("DF_AZURE_STORAGE_CONNECTION_STRING")
        or os.getenv("STORAGE_CONNECTION_STRING")
        or ""
    ).strip()
    if cs:
        parts: Dict[str, str] = {}
        for seg in cs.split(";"):
            if "=" in seg:
                a, b = seg.split("=", 1)
                parts[a.strip()] = b.strip()
        k = parts.get("AccountKey")
        if k:
            return k
    return None


def maybe_add_azure_read_sas(url: str, expiry_hours: int = 24) -> str:
    """
    Adds read-only SAS to Azure blob URLs that don't already have one.
    Fails open (returns original url) if dependencies/keys are missing.
    """
    if not (isinstance(url, str) and url.startswith("http")):
        return url
    if "blob.core.windows.net" not in url:
        return url
    if "sig=" in url:
        return url

    if generate_blob_sas is None or BlobSasPermissions is None:
        return url

    key = _azure_account_key_from_env()
    if not key:
        return url

    try:
        u = urlparse(url)
        host = (u.netloc or "").strip()
        account_name = host.split(".")[0] if host else ""
        path = (u.path or "").lstrip("/")
        if not account_name or not path or "/" not in path:
            return url
        container, blob = path.split("/", 1)
        if not container or not blob:
            return url

        sas = generate_blob_sas(
            account_name=account_name,
            account_key=key,
            container_name=container,
            blob_name=blob,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(hours=int(expiry_hours)),
        )
        if not sas:
            return url

        base = f"{u.scheme}://{u.netloc}/{container}/{blob}"
        return base + "?" + sas

    except Exception as e:
        logger.warning("Failed to generate SAS for url=%s err=%s", url, str(e))
        return url