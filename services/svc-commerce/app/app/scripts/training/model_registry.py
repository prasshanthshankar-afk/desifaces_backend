# services/svc-commerce/app/app/scripts/training/model_registry.py
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

import asyncpg
from azure.storage.blob import BlobServiceClient


def _env_bool(name: str, default: bool = True) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _parse_az_path(az_path: str) -> Optional[Tuple[str, str]]:
    # az://<container>/<blob>
    if not isinstance(az_path, str):
        return None
    s = az_path.strip()
    if not s.startswith("az://"):
        return None
    rest = s[len("az://") :]
    if "/" not in rest:
        return None
    container, blob_name = rest.split("/", 1)
    if not container or not blob_name:
        return None
    return container, blob_name


def _blob_exists_and_big_enough(az_path: str, *, min_bytes: int) -> bool:
    """
    Validates blob exists and size >= min_bytes.
    Uses AZURE_STORAGE_CONNECTION_STRING.
    """
    parsed = _parse_az_path(az_path)
    if not parsed:
        return False

    conn = (os.getenv("AZURE_STORAGE_CONNECTION_STRING") or "").strip()
    if not conn:
        # If the env isn't available, we can't verify. Default to "not valid" in prod.
        return False

    container, blob_name = parsed
    svc = BlobServiceClient.from_connection_string(conn)
    blob = svc.get_blob_client(container=container, blob=blob_name)
    try:
        props = blob.get_blob_properties()  # HEAD
        size = int(getattr(props, "size", 0) or 0)
        return size >= int(min_bytes)
    except Exception:
        return False


async def get_latest_succeeded_checkpoint(
    pool: asyncpg.Pool,
    model_family: str,
    *,
    drape_template_slug: Optional[str] = None,
    validate_blob: Optional[bool] = None,
    max_candidates: int = 25,
) -> Optional[Dict[str, Any]]:
    """
    Returns the newest succeeded checkpoint for a model_family that is actually usable.

    Safety:
      - requires artifacts_json.weights.path non-empty
      - rejects placeholder paths containing 'REPLACE_ME'
      - optionally filters by config_json->>'drape_template_slug'
      - optionally validates az:// blob exists and is "large enough" to be real weights

    Env:
      - DF_CHECKPOINT_VALIDATE_BLOB (default true)
      - DF_CHECKPOINT_MIN_BYTES (default 10MB)
    """
    if validate_blob is None:
        validate_blob = _env_bool("DF_CHECKPOINT_VALIDATE_BLOB", default=True)

    min_bytes = int((os.getenv("DF_CHECKPOINT_MIN_BYTES") or "10485760").strip() or "10485760")  # 10MB

    base_where = """
      where model_family = $1
        and status = 'succeeded'
        and coalesce(artifacts_json->'weights'->>'path','') <> ''
        and (artifacts_json->'weights'->>'path') not ilike '%REPLACE_ME%'
        and coalesce(artifacts_json->>'checkpoint_root','') not ilike '%REPLACE_ME%'
    """

    if drape_template_slug:
        sql = f"""
        select
          id,
          model_family,
          base_model,
          status,
          created_at,
          config_json,
          hyperparams_json,
          metrics_json,
          artifacts_json,
          notes
        from model_checkpoints
        {base_where}
          and (config_json->>'drape_template_slug') = $2
        order by created_at desc
        limit {int(max_candidates)}
        """
        args = (model_family, drape_template_slug)
    else:
        sql = f"""
        select
          id,
          model_family,
          base_model,
          status,
          created_at,
          config_json,
          hyperparams_json,
          metrics_json,
          artifacts_json,
          notes
        from model_checkpoints
        {base_where}
        order by created_at desc
        limit {int(max_candidates)}
        """
        args = (model_family,)

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    if not rows:
        return None

    # If we can't validate blobs, return the newest row (already filtered for REPLACE_ME/empty).
    if not validate_blob:
        return dict(rows[0])

    # Validate blob existence/size; return first valid candidate
    for r in rows:
        d = dict(r)
        weights_path = (((d.get("artifacts_json") or {}).get("weights") or {}).get("path") or "").strip()
        if not weights_path:
            continue
        if _blob_exists_and_big_enough(weights_path, min_bytes=min_bytes):
            return d

    return None