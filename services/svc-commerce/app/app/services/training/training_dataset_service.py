from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4

import asyncpg
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        v = (_env(name) or "").strip()
        return int(v) if v else default
    except Exception:
        return default


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj if obj is not None else {}, ensure_ascii=False, separators=(",", ":"))


def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            j = json.loads(s)
            return j if isinstance(j, dict) else {}
        except Exception:
            return {}
    try:
        return dict(x)
    except Exception:
        return {}


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _norm_split(split: str) -> str:
    s = (split or "").strip().lower()
    if s in {"train", "tr"}:
        return "train"
    if s in {"val", "valid", "validation", "dev"}:
        return "val"
    if s in {"test", "eval"}:
        return "test"
    return "train"


def _norm_task(task: str) -> str:
    s = (task or "").strip().lower()
    return s or "tryon"


def _sanitize_blob_component(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._/-]+", "-", str(s or "").strip())
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("/")


def _guess_extension_from_url_or_ct(url: str, content_type: Optional[str], default_ext: str = ".bin") -> str:
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            if guessed == ".jpe":
                return ".jpg"
            return guessed

    path = (url or "").split("?", 1)[0].lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".json"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext

    return default_ext


@dataclass(frozen=True)
class BlobRef:
    container: str
    blob: str

    def as_json(self) -> Dict[str, Any]:
        return {"container": self.container, "blob": self.blob}

    def as_az_ref(self) -> str:
        return f"az://{self.container}/{self.blob}"


@dataclass(frozen=True)
class DownloadedBytes:
    data: bytes
    content_type: str
    source_url: str


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


def parse_az_ref(ref: str) -> Optional[BlobRef]:
    s = (ref or "").strip()
    if not s.startswith("az://"):
        return None
    rest = s[len("az://") :]
    if "/" not in rest:
        return None
    container, blob = rest.split("/", 1)
    container = container.strip()
    blob = blob.lstrip("/")
    if not container or not blob:
        return None
    return BlobRef(container=container, blob=blob)


class TrainingDatasetService:
    """
    Production-scale dataset service for svc-commerce training.

    Responsibilities:
      - Creates/updates dataset rows in Postgres
      - Uploads assets/manifests to Azure (via AzureStorageService instance you pass)
      - Inserts training_examples rows (single + bulk)
      - Computes/finalizes dataset stats
      - Content-addresses cached source assets under commerce-training

    Notes:
      - Keeps the original public API methods:
          create_dataset(...)
          finalize_dataset_stats(...)
          upload_bytes(...)
          download_url_bytes(...)
          cache_source_url(...)
          insert_example(...)
          get_db_pool()
      - Adds bulk and stats helpers for production-scale ingestion.
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
        self.training_container = (training_container or "commerce-training").strip() or "commerce-training"

        self.request_timeout_s = _env_int("TRAIN_HTTP_TIMEOUT_S", 60)
        self.max_download_bytes = _env_int("TRAIN_MAX_DOWNLOAD_BYTES", 25 * 1024 * 1024)
        self.insert_batch_size = _env_int("TRAIN_INSERT_BATCH_SIZE", 500)

        # in-memory cache: source_url -> BlobRef
        self._source_cache: Dict[str, BlobRef] = {}

        self._session = self._build_requests_session()

    # -------------------------------------------------------------------------
    # HTTP / Azure helpers
    # -------------------------------------------------------------------------

    def _build_requests_session(self) -> requests.Session:
        retries = Retry(
            total=_env_int("TRAIN_HTTP_RETRIES", 3),
            connect=3,
            read=3,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=50)
        s = requests.Session()
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers.update({"User-Agent": _env("TRAIN_HTTP_USER_AGENT", "df-commerce-training/1.0")})
        return s

    def _call_storage_upload_bytes(
        self,
        *,
        data: bytes,
        blob_name: str,
        content_type: str,
        container_name: str,
    ) -> Any:
        """
        Best-effort support for slightly different AzureStorageService signatures.
        """
        candidates = [
            ("upload_bytes", {"data": data, "blob_name": blob_name, "content_type": content_type, "container_name": container_name}),
            ("upload_bytes", {"data": data, "blob_name": blob_name, "content_type": content_type, "container": container_name}),
            ("upload_blob", {"data": data, "blob_name": blob_name, "content_type": content_type, "container_name": container_name}),
            ("upload_blob", {"data": data, "blob_name": blob_name, "content_type": content_type, "container": container_name}),
        ]

        last_err: Optional[Exception] = None
        for method_name, kwargs in candidates:
            fn = getattr(self.storage, method_name, None)
            if not callable(fn):
                continue
            try:
                return fn(**kwargs)
            except Exception as e:
                last_err = e
                continue

        raise RuntimeError(f"Azure storage upload_bytes/upload_blob call failed: {last_err!r}")

    # -------------------------------------------------------------------------
    # Dataset lifecycle
    # -------------------------------------------------------------------------

    async def create_dataset(
        self,
        *,
        name: str,
        kind: str = "synthetic",
        usage_scope: str = "commercial_ok",
        recipe_json: Optional[Dict[str, Any]] = None,
        license_name: Optional[str] = None,
        license_url: Optional[str] = None,
        dataset_id: Optional[UUID] = None,
        storage_prefix: Optional[str] = None,
    ) -> Tuple[UUID, str]:
        dataset_id = dataset_id or uuid4()
        prefix = storage_prefix or f"training/saree_synth/{_today_utc()}/{dataset_id}"

        rj = _as_dict(recipe_json)
        rj.setdefault("created_at", _utc_now())
        rj.setdefault("dataset_name", name)
        rj.setdefault("dataset_kind", kind)

        q = """
        INSERT INTO training_datasets
          (id, name, kind, usage_scope, license_name, license_url,
           storage_container, storage_prefix, recipe_json, stats_json, is_frozen)
        VALUES
          ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, '{}'::jsonb, false)
        ON CONFLICT (id) DO UPDATE SET
          name = EXCLUDED.name,
          kind = EXCLUDED.kind,
          usage_scope = EXCLUDED.usage_scope,
          license_name = EXCLUDED.license_name,
          license_url = EXCLUDED.license_url,
          storage_container = EXCLUDED.storage_container,
          storage_prefix = EXCLUDED.storage_prefix,
          recipe_json = EXCLUDED.recipe_json,
          updated_at = now()
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
                _json_dumps(rj),
            )
        return dataset_id, prefix

    async def get_dataset_row(self, *, dataset_id: UUID) -> Optional[Dict[str, Any]]:
        q = """
        SELECT id, name, kind, usage_scope, license_name, license_url,
               storage_container, storage_prefix, recipe_json, stats_json,
               is_frozen, created_at, updated_at
        FROM training_datasets
        WHERE id = $1
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q, dataset_id)
        return dict(row) if row else None

    async def compute_dataset_stats(self, *, dataset_id: UUID) -> Dict[str, Any]:
        q_total = """
        SELECT COUNT(*)::bigint AS n
        FROM training_examples
        WHERE dataset_id = $1
        """
        q_by_split = """
        SELECT split, COUNT(*)::bigint AS n
        FROM training_examples
        WHERE dataset_id = $1
        GROUP BY split
        ORDER BY split
        """
        q_by_task = """
        SELECT task, COUNT(*)::bigint AS n
        FROM training_examples
        WHERE dataset_id = $1
        GROUP BY task
        ORDER BY task
        """

        async with self.pool.acquire() as conn:
            total = await conn.fetchval(q_total, dataset_id)
            split_rows = await conn.fetch(q_by_split, dataset_id)
            task_rows = await conn.fetch(q_by_task, dataset_id)

        by_split = {str(r["split"]): int(r["n"]) for r in split_rows}
        by_task = {str(r["task"]): int(r["n"]) for r in task_rows}

        return {
            "dataset_id": str(dataset_id),
            "computed_at": _utc_now(),
            "example_count": int(total or 0),
            "by_split": by_split,
            "by_task": by_task,
        }

    async def finalize_dataset_stats(
        self,
        *,
        dataset_id: UUID,
        stats_json: Optional[Dict[str, Any]] = None,
        freeze: bool = True,
    ) -> None:
        final_stats = _as_dict(stats_json) if stats_json is not None else await self.compute_dataset_stats(dataset_id=dataset_id)

        q = """
        UPDATE training_datasets
        SET stats_json = $2::jsonb,
            is_frozen = $3,
            updated_at = now()
        WHERE id = $1
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, dataset_id, _json_dumps(final_stats), freeze)

    # -------------------------------------------------------------------------
    # Blob / source caching
    # -------------------------------------------------------------------------

    def upload_bytes(
        self,
        *,
        data: bytes,
        blob: str,
        content_type: str,
    ) -> BlobRef:
        self._call_storage_upload_bytes(
            data=data,
            blob_name=blob,
            content_type=content_type,
            container_name=self.training_container,
        )
        return BlobRef(container=self.training_container, blob=blob)

    def upload_json(
        self,
        *,
        obj: Dict[str, Any],
        blob: str,
    ) -> BlobRef:
        payload = _json_dumps(obj).encode("utf-8")
        return self.upload_bytes(data=payload, blob=blob, content_type="application/json")

    def download_url_bytes(self, url: str, timeout_s: Optional[int] = None) -> bytes:
        got = self.download_url(url, timeout_s=timeout_s)
        return got.data

    def download_url(self, url: str, timeout_s: Optional[int] = None) -> DownloadedBytes:
        timeout = int(timeout_s or self.request_timeout_s)
        with self._session.get(url, timeout=timeout, allow_redirects=True, stream=True) as r:
            r.raise_for_status()

            content_type = (r.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0].strip()
            chunks: List[bytes] = []
            total = 0
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.max_download_bytes:
                    raise RuntimeError(
                        f"download_too_large url={url} size={total} max={self.max_download_bytes}"
                    )
                chunks.append(chunk)

        return DownloadedBytes(
            data=b"".join(chunks),
            content_type=content_type,
            source_url=url,
        )

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
        Supports:
          - normal https:// blob URLs
          - az://<container>/<blob>
          - already-cached commerce-training URLs
        """
        az = parse_az_ref(url)
        if az and az.container == self.training_container:
            self._source_cache[url] = az
            return az

        got = parse_container_blob_from_url(url)
        if got and got[0] == self.training_container:
            ref = BlobRef(container=got[0], blob=got[1])
            self._source_cache[url] = ref
            return ref

        if url in self._source_cache:
            return self._source_cache[url]

        dl = self.download_url(url, timeout_s=timeout_s)
        sha = _sha256_bytes(dl.data)[:20]
        ext = _guess_extension_from_url_or_ct(url, dl.content_type or content_type, default_ext=".bin")
        blob = _sanitize_blob_component(f"{dataset_prefix}/sources/{kind}/{sha}{ext}")

        ref = self.upload_bytes(
            data=dl.data,
            blob=blob,
            content_type=dl.content_type or content_type,
        )
        self._source_cache[url] = ref
        return ref

    # -------------------------------------------------------------------------
    # Example insert helpers
    # -------------------------------------------------------------------------

    def _build_dedup_hash(
        self,
        *,
        template_id: Optional[UUID],
        split: str,
        task: str,
        person_ref: Dict[str, Any],
        garment_refs: Dict[str, Any],
        conditioning_refs: Optional[Dict[str, Any]],
        target_ref: Dict[str, Any],
        mask_refs: Optional[Dict[str, Any]],
        labels_json: Dict[str, Any],
    ) -> str:
        dedup_obj = {
            "template_id": str(template_id) if template_id else None,
            "split": _norm_split(split),
            "task": _norm_task(task),
            "person_ref": _as_dict(person_ref),
            "garment_refs": _as_dict(garment_refs),
            "conditioning_refs": _as_dict(conditioning_refs),
            "target_ref": _as_dict(target_ref),
            "mask_refs": _as_dict(mask_refs),
            "labels": _as_dict(labels_json),
        }
        return _sha256_text(_stable_json(dedup_obj))

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
        split_n = _norm_split(split)
        task_n = _norm_task(task)

        person_ref = _as_dict(person_ref)
        garment_refs = _as_dict(garment_refs)
        conditioning_refs = _as_dict(conditioning_refs)
        target_ref = _as_dict(target_ref)
        mask_refs = _as_dict(mask_refs)
        labels_json = _as_dict(labels_json)
        quality_json = _as_dict(quality_json)
        consent_json = _as_dict(consent_json)

        dedup_hash = self._build_dedup_hash(
            template_id=template_id,
            split=split_n,
            task=task_n,
            person_ref=person_ref,
            garment_refs=garment_refs,
            conditioning_refs=conditioning_refs,
            target_ref=target_ref,
            mask_refs=mask_refs,
            labels_json=labels_json,
        )

        sha_json = {
            "dedup": dedup_hash,
            "labels_sha256": _sha256_text(_stable_json(labels_json)),
            "person_ref_sha256": _sha256_text(_stable_json(person_ref)),
            "garment_refs_sha256": _sha256_text(_stable_json(garment_refs)),
            "target_ref_sha256": _sha256_text(_stable_json(target_ref)),
        }

        q = """
        WITH ins AS (
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
            RETURNING id
        )
        SELECT COALESCE(
            (SELECT id FROM ins),
            (SELECT id FROM training_examples WHERE dataset_id = $2 AND dedup_hash = $14)
        ) AS id
        """
        async with self.pool.acquire() as conn:
            got = await conn.fetchval(
                q,
                ex_id,
                dataset_id,
                template_id,
                split_n,
                task_n,
                _json_dumps(person_ref),
                _json_dumps(garment_refs),
                _json_dumps(conditioning_refs),
                _json_dumps(target_ref),
                _json_dumps(mask_refs),
                _json_dumps(labels_json),
                _json_dumps(quality_json),
                _json_dumps(consent_json),
                dedup_hash,
                _json_dumps(sha_json),
            )

        return got if isinstance(got, UUID) else ex_id

    async def insert_examples_bulk(
        self,
        *,
        dataset_id: UUID,
        examples: Sequence[Dict[str, Any]],
        batch_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Bulk insert helper for synthetic dataset generation.

        Each example dict should provide the same keys used by insert_example():
          template_id, split, task, person_ref, garment_refs, conditioning_refs,
          target_ref, mask_refs, labels_json, quality_json, consent_json
        """
        if not examples:
            return {"dataset_id": str(dataset_id), "received": 0, "inserted_estimate": 0}

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

        batch_n = int(batch_size or self.insert_batch_size)

        async with self.pool.acquire() as conn:
            before = await conn.fetchval(
                "SELECT COUNT(*)::bigint FROM training_examples WHERE dataset_id = $1",
                dataset_id,
            )

            async with conn.transaction():
                for start in range(0, len(examples), batch_n):
                    batch = examples[start : start + batch_n]
                    rows: List[Tuple[Any, ...]] = []

                    for ex in batch:
                        template_id = ex.get("template_id")
                        split_n = _norm_split(str(ex.get("split") or "train"))
                        task_n = _norm_task(str(ex.get("task") or "tryon"))
                        person_ref = _as_dict(ex.get("person_ref"))
                        garment_refs = _as_dict(ex.get("garment_refs"))
                        conditioning_refs = _as_dict(ex.get("conditioning_refs"))
                        target_ref = _as_dict(ex.get("target_ref"))
                        mask_refs = _as_dict(ex.get("mask_refs"))
                        labels_json = _as_dict(ex.get("labels_json"))
                        quality_json = _as_dict(ex.get("quality_json"))
                        consent_json = _as_dict(ex.get("consent_json"))

                        dedup_hash = self._build_dedup_hash(
                            template_id=template_id,
                            split=split_n,
                            task=task_n,
                            person_ref=person_ref,
                            garment_refs=garment_refs,
                            conditioning_refs=conditioning_refs,
                            target_ref=target_ref,
                            mask_refs=mask_refs,
                            labels_json=labels_json,
                        )

                        sha_json = {
                            "dedup": dedup_hash,
                            "labels_sha256": _sha256_text(_stable_json(labels_json)),
                            "person_ref_sha256": _sha256_text(_stable_json(person_ref)),
                            "garment_refs_sha256": _sha256_text(_stable_json(garment_refs)),
                            "target_ref_sha256": _sha256_text(_stable_json(target_ref)),
                        }

                        rows.append(
                            (
                                uuid4(),
                                dataset_id,
                                template_id,
                                split_n,
                                task_n,
                                _json_dumps(person_ref),
                                _json_dumps(garment_refs),
                                _json_dumps(conditioning_refs),
                                _json_dumps(target_ref),
                                _json_dumps(mask_refs),
                                _json_dumps(labels_json),
                                _json_dumps(quality_json),
                                _json_dumps(consent_json),
                                dedup_hash,
                                _json_dumps(sha_json),
                            )
                        )

                    await conn.executemany(q, rows)

            after = await conn.fetchval(
                "SELECT COUNT(*)::bigint FROM training_examples WHERE dataset_id = $1",
                dataset_id,
            )

        return {
            "dataset_id": str(dataset_id),
            "received": len(examples),
            "inserted_estimate": max(0, int(after or 0) - int(before or 0)),
            "final_count": int(after or 0),
            "batch_size": batch_n,
        }

    async def upload_manifest_json(
        self,
        *,
        dataset_prefix: str,
        manifest_name: str,
        manifest_json: Dict[str, Any],
    ) -> BlobRef:
        blob = _sanitize_blob_component(f"{dataset_prefix}/manifests/{manifest_name}")
        if not blob.endswith(".json"):
            blob = f"{blob}.json"
        return self.upload_json(obj=manifest_json, blob=blob)


async def get_db_pool() -> asyncpg.Pool:
    dsn = _env("DATABASE_URL") or _env("COMMERCE_DATABASE_URL")
    if not dsn:
        raise RuntimeError("Missing DATABASE_URL (or COMMERCE_DATABASE_URL)")
    return await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=int(_env("TRAIN_DB_POOL_MAX", "5") or "5"),
        command_timeout=_env_int("TRAIN_DB_COMMAND_TIMEOUT_S", 120),
    )