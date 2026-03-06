from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from uuid import UUID, uuid4

import asyncpg
import requests


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class BlobRef:
    container: str
    blob: str

    def as_json(self) -> Dict[str, Any]:
        return {"container": self.container, "blob": self.blob}


def parse_container_blob_from_url(url: str) -> Optional[Tuple[str, str]]:
    """
    https://<acct>.blob.core.windows.net/<container>/<blob...>?...
    """
    try:
        base = url.split("?", 1)[0]
        m = re.search(r"blob\.core\.windows\.net/([^/]+)/(.+)$", base)
        if not m:
            return None
        return m.group(1), m.group(2)
    except Exception:
        return None


class TrainingDatasetService:
    """
    - Creates dataset rows in Postgres
    - Uploads assets to Azure (via AzureStorageService instance you pass)
    - Inserts training_examples rows
    """

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        storage: Any,  # AzureStorageService (sync upload_bytes/upload_file)
        training_container: str = "commerce-training",
    ) -> None:
        self.pool = pool
        self.storage = storage
        self.training_container = training_container

        # in-memory cache: source_url -> BlobRef
        self._source_cache: Dict[str, BlobRef] = {}

    async def create_dataset(
        self,
        *,
        name: str,
        kind: str = "synthetic",
        usage_scope: str = "commercial_ok",
        recipe_json: Optional[Dict[str, Any]] = None,
        license_name: Optional[str] = None,
        license_url: Optional[str] = None,
    ) -> Tuple[UUID, str]:
        dataset_id = uuid4()
        prefix = f"training/saree_synth/{datetime.utcnow().strftime('%Y-%m-%d')}/{dataset_id}"
        rj = recipe_json or {}
        rj.setdefault("created_at", _utc_now())

        q = """
        INSERT INTO training_datasets
          (id, name, kind, usage_scope, license_name, license_url, storage_container, storage_prefix, recipe_json, stats_json, is_frozen)
        VALUES
          ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, '{}'::jsonb, false)
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                q,
                dataset_id,
                name,
                kind,
                usage_scope,
                license_name,
                license_url,
                self.training_container,
                prefix,
                json.dumps(rj),
            )
        return dataset_id, prefix

    async def finalize_dataset_stats(self, *, dataset_id: UUID, stats_json: Dict[str, Any], freeze: bool = True) -> None:
        q = """
        UPDATE training_datasets
        SET stats_json = $2::jsonb,
            is_frozen = $3,
            updated_at = now()
        WHERE id = $1
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, dataset_id, json.dumps(stats_json), freeze)

    def upload_bytes(
        self,
        *,
        data: bytes,
        blob: str,
        content_type: str,
    ) -> BlobRef:
        # AzureStorageService.upload_bytes supports container_name in your svc-commerce version
        url = self.storage.upload_bytes(
            data=data,
            blob_name=blob,
            content_type=content_type,
            container_name=self.training_container,
        )
        # we store container/blob refs, not SAS URLs
        return BlobRef(container=self.training_container, blob=blob)

    def download_url_bytes(self, url: str, timeout_s: int = 60) -> bytes:
        r = requests.get(url, timeout=timeout_s, allow_redirects=True)
        r.raise_for_status()
        return r.content

    def cache_source_url(
        self,
        *,
        url: str,
        dataset_prefix: str,
        kind: str,
        content_type: str = "image/png",
        timeout_s: int = 60,
    ) -> BlobRef:
        """
        Downloads and uploads a source URL to commerce-training once, content-addressed by sha256(bytes).
        """
        got = parse_container_blob_from_url(url)
        if got and got[0] == self.training_container:
            ref = BlobRef(container=got[0], blob=got[1])
            self._source_cache[url] = ref
            return ref

        if url in self._source_cache:
            return self._source_cache[url]

        b = self.download_url_bytes(url, timeout_s=timeout_s)
        sha = _sha256_bytes(b)[:20]
        blob = f"{dataset_prefix}/sources/{kind}/{sha}.bin"

        # Upload if not already cached in this process
        ref = self.upload_bytes(data=b, blob=blob, content_type=content_type)
        self._source_cache[url] = ref
        return ref

    async def insert_example(
        self,
        *,
        dataset_id: UUID,
        template_id: Optional[UUID],
        split: str,
        task: str,
        person_ref: Dict[str, Any],
        garment_refs: Dict[str, Any],
        conditioning_refs: Optional[Dict[str, Any]],
        target_ref: Dict[str, Any],
        mask_refs: Optional[Dict[str, Any]],
        labels_json: Dict[str, Any],
        quality_json: Dict[str, Any],
        consent_json: Optional[Dict[str, Any]] = None,
    ) -> UUID:
        ex_id = uuid4()

        # Dedup based on content refs + labels
        dedup_obj = {
            "person_ref": person_ref,
            "garment_refs": garment_refs,
            "labels": labels_json,
            "template_id": str(template_id) if template_id else None,
        }
        dedup_hash = _sha256_text(_stable_json(dedup_obj))

        sha_json = {
            "dedup": dedup_hash,
        }

        q = """
        INSERT INTO training_examples
          (id, dataset_id, template_id, split, task,
           person_ref, garment_refs, conditioning_refs,
           target_ref, mask_refs, labels_json,
           quality_json, consent_json,
           dedup_hash, sha256_json)
        VALUES
          ($1, $2, $3, $4, $5,
           $6::jsonb, $7::jsonb, $8::jsonb,
           $9::jsonb, $10::jsonb, $11::jsonb,
           $12::jsonb, $13::jsonb,
           $14, $15::jsonb)
        ON CONFLICT (dataset_id, dedup_hash) DO NOTHING
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                q,
                ex_id,
                dataset_id,
                template_id,
                split,
                task,
                json.dumps(person_ref),
                json.dumps(garment_refs),
                json.dumps(conditioning_refs or {}),
                json.dumps(target_ref),
                json.dumps(mask_refs or {}),
                json.dumps(labels_json),
                json.dumps(quality_json),
                json.dumps(consent_json or {}),
                dedup_hash,
                json.dumps(sha_json),
            )
        return ex_id


async def get_db_pool() -> asyncpg.Pool:
    dsn = _env("DATABASE_URL") or _env("COMMERCE_DATABASE_URL")
    if not dsn:
        raise RuntimeError("Missing DATABASE_URL (or COMMERCE_DATABASE_URL)")
    return await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=int(_env("TRAIN_DB_POOL_MAX", "5") or "5"))