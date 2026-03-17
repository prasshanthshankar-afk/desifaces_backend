from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import asyncpg
from PIL import Image, ImageOps
from azure.storage.blob import BlobServiceClient
from azure.storage.blob import BlobSasPermissions, generate_blob_sas

logger_name = __name__


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    try:
        v = (_env_str(name) or "").strip()
        return int(v) if v else default
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        v = (_env_str(name) or "").strip()
        return float(v) if v else default
    except Exception:
        return default


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _safe_root(s: str) -> str:
    s = str(s or "")
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", s)
    return s[:64] or "ex"


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


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    return [x]


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj if obj is not None else {}, ensure_ascii=False, separators=(",", ":"))


def _jget(d: Dict[str, Any], *path: str) -> Any:
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _http_json(
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    body: Optional[Dict[str, Any]] = None,
    timeout_s: int = 120,
) -> Dict[str, Any]:
    m = (method or "GET").strip().upper()
    hdrs = {"Accept": "application/json", "User-Agent": "df-commerce-training-orchestrator/1.0"}
    hdrs.update(headers or {})

    data = None
    if body is not None and m not in ("GET", "HEAD"):
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = Request(url=url, method=m, headers=hdrs, data=data)
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read() or b""
            txt = raw.decode("utf-8", errors="replace").strip()
            if not txt:
                return {}
            out = json.loads(txt)
            return out if isinstance(out, dict) else {"raw": out}
    except HTTPError as e:
        raw = b""
        try:
            raw = e.read() or b""
        except Exception:
            pass
        txt = raw.decode("utf-8", errors="replace").strip() if raw else str(e)
        try:
            j = json.loads(txt) if txt else {}
        except Exception:
            j = {"raw": txt}
        raise RuntimeError(f"HTTPError code={e.code} url={url} body={j}") from e
    except URLError as e:
        raise RuntimeError(f"URLError url={url} err={e}") from e


def _download_bytes(url: str, timeout_s: int = 180) -> bytes:
    req = Request(url, headers={"User-Agent": "df-commerce-training-orchestrator/1.0"})
    with urlopen(req, timeout=timeout_s) as resp:
        return resp.read() or b""


def _ensure_png_bytes(img_bytes: bytes) -> bytes:
    im = Image.open(io.BytesIO(img_bytes))
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _blob_exists_and_big_enough(
    *,
    conn_str: str,
    az_path: str,
    min_bytes: int,
) -> bool:
    parsed = _parse_az_path(az_path)
    if not parsed:
        return False
    container, blob_name = parsed
    try:
        svc = BlobServiceClient.from_connection_string(conn_str)
        blob = svc.get_blob_client(container=container, blob=blob_name)
        props = blob.get_blob_properties()
        size = int(getattr(props, "size", 0) or 0)
        return size >= int(min_bytes)
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Azure helpers
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class AzureCtx:
    container: str
    conn_str: str
    account_name: str
    account_key: str


def _azure_conn_parts(conn: str) -> Tuple[str, str]:
    parts = {}
    for kv in conn.split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            parts[k.strip()] = v.strip()
    acct = parts.get("AccountName") or ""
    key = parts.get("AccountKey") or ""
    if not acct or not key:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING must include AccountName and AccountKey")
    return acct, key


def _azure_ctx_from_conn_str(container: str) -> AzureCtx:
    conn = (_env_str("AZURE_STORAGE_CONNECTION_STRING") or "").strip()
    if not conn:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is required")
    acct, key = _azure_conn_parts(conn)
    return AzureCtx(container=container, conn_str=conn, account_name=acct, account_key=key)


def _download_blob_bytes(bsc: BlobServiceClient, container: str, blob: str) -> bytes:
    bc = bsc.get_blob_client(container=container, blob=blob)
    return bc.download_blob().readall()


def _upload_blob_bytes(
    bsc: BlobServiceClient,
    container: str,
    blob: str,
    data: bytes,
    content_type: str,
) -> None:
    bc = bsc.get_blob_client(container=container, blob=blob)
    bc.upload_blob(
        data,
        overwrite=True,
        content_settings={"content_type": content_type},  # type: ignore[arg-type]
    )


def _sas_url_for_blob(ctx: AzureCtx, blob: str, *, hours: int = 24) -> str:
    expiry = datetime.now(timezone.utc) + timedelta(hours=hours)
    sas = generate_blob_sas(
        account_name=ctx.account_name,
        account_key=ctx.account_key,
        container_name=ctx.container,
        blob_name=blob,
        permission=BlobSasPermissions(read=True),
        expiry=expiry,
    )
    return f"https://{ctx.account_name}.blob.core.windows.net/{ctx.container}/{blob}?{sas}"


def _parse_az_path(az_path: str) -> Optional[Tuple[str, str]]:
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


# -----------------------------------------------------------------------------
# Family training specs
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class FamilyTrainingSpec:
    family: str
    model_family: str
    accepted_tasks: Tuple[str, ...]
    default_caption: str
    train_min_examples: int
    val_min_examples: int
    export_prefix: str
    checkpoint_prefix: str
    trainer_endpoint: str
    default_steps: int
    default_learning_rate: float
    min_target_stddev: float
    min_person_target_mad: float
    min_person_target_center_mad: float


def _family_specs() -> Dict[str, FamilyTrainingSpec]:
    trainer_endpoint = _env_str("COMMERCE_TRAINER_ENDPOINT", "fal-ai/flux-2-trainer-v2/edit")
    export_prefix = _env_str("COMMERCE_TRAIN_EXPORT_PREFIX", "training/flux2_edit_zips")
    checkpoint_prefix = _env_str("COMMERCE_TRAIN_CHECKPOINT_PREFIX", "checkpoints/indian_non_saree_flux2_edit")

    return {
        "salwar_suit": FamilyTrainingSpec(
            family="salwar_suit",
            model_family="salwar_suit",
            accepted_tasks=("salwar_suit_tryon", "indian_non_saree_tryon", "tryon"),
            default_caption=(
                "Dress the person in a traditional Indian salwar suit. "
                "Preserve identity, pose, lighting, body shape, and background. "
                "Match garment color, fabric, embroidery, silhouette, and overall styling."
            ),
            train_min_examples=_env_int("TRAIN_MIN_EXAMPLES_SALWAR_SUIT", 100),
            val_min_examples=_env_int("TRAIN_MIN_VAL_EXAMPLES_SALWAR_SUIT", 10),
            export_prefix=export_prefix,
            checkpoint_prefix=checkpoint_prefix,
            trainer_endpoint=trainer_endpoint,
            default_steps=_env_int("TRAIN_DEFAULT_STEPS_SALWAR_SUIT", 1200),
            default_learning_rate=_env_float("TRAIN_DEFAULT_LR_SALWAR_SUIT", 0.00005),
            min_target_stddev=_env_float("TRAIN_MIN_TARGET_STDDEV_SALWAR_SUIT", 0.015),
            min_person_target_mad=_env_float("TRAIN_MIN_PERSON_TARGET_MAD_SALWAR_SUIT", 0.010),
            min_person_target_center_mad=_env_float("TRAIN_MIN_PERSON_TARGET_CENTER_MAD_SALWAR_SUIT", 0.008),
        ),
        "lehenga_set": FamilyTrainingSpec(
            family="lehenga_set",
            model_family="lehenga_set",
            accepted_tasks=("lehenga_set_tryon", "indian_non_saree_tryon", "tryon"),
            default_caption=(
                "Dress the person in a traditional Indian lehenga set. "
                "Preserve identity, pose, lighting, body shape, and background. "
                "Match lehenga, choli, dupatta, textile pattern, and ornamentation."
            ),
            train_min_examples=_env_int("TRAIN_MIN_EXAMPLES_LEHENGA_SET", 100),
            val_min_examples=_env_int("TRAIN_MIN_VAL_EXAMPLES_LEHENGA_SET", 10),
            export_prefix=export_prefix,
            checkpoint_prefix=checkpoint_prefix,
            trainer_endpoint=trainer_endpoint,
            default_steps=_env_int("TRAIN_DEFAULT_STEPS_LEHENGA_SET", 1200),
            default_learning_rate=_env_float("TRAIN_DEFAULT_LR_LEHENGA_SET", 0.00005),
            min_target_stddev=_env_float("TRAIN_MIN_TARGET_STDDEV_LEHENGA_SET", 0.015),
            min_person_target_mad=_env_float("TRAIN_MIN_PERSON_TARGET_MAD_LEHENGA_SET", 0.010),
            min_person_target_center_mad=_env_float("TRAIN_MIN_PERSON_TARGET_CENTER_MAD_LEHENGA_SET", 0.008),
        ),
        "kurta_pyjama": FamilyTrainingSpec(
            family="kurta_pyjama",
            model_family="kurta_pyjama",
            accepted_tasks=("kurta_pyjama_tryon", "indian_non_saree_tryon", "tryon"),
            default_caption=(
                "Dress the person in a traditional Indian kurta pyjama outfit. "
                "Preserve identity, pose, lighting, body shape, and background. "
                "Match kurta fit, pyjama silhouette, fabric, and colors."
            ),
            train_min_examples=_env_int("TRAIN_MIN_EXAMPLES_KURTA_PYJAMA", 100),
            val_min_examples=_env_int("TRAIN_MIN_VAL_EXAMPLES_KURTA_PYJAMA", 10),
            export_prefix=export_prefix,
            checkpoint_prefix=checkpoint_prefix,
            trainer_endpoint=trainer_endpoint,
            default_steps=_env_int("TRAIN_DEFAULT_STEPS_KURTA_PYJAMA", 1200),
            default_learning_rate=_env_float("TRAIN_DEFAULT_LR_KURTA_PYJAMA", 0.00005),
            min_target_stddev=_env_float("TRAIN_MIN_TARGET_STDDEV_KURTA_PYJAMA", 0.015),
            min_person_target_mad=_env_float("TRAIN_MIN_PERSON_TARGET_MAD_KURTA_PYJAMA", 0.010),
            min_person_target_center_mad=_env_float("TRAIN_MIN_PERSON_TARGET_CENTER_MAD_KURTA_PYJAMA", 0.008),
        ),
        "sherwani": FamilyTrainingSpec(
            family="sherwani",
            model_family="sherwani",
            accepted_tasks=("sherwani_tryon", "indian_non_saree_tryon", "tryon"),
            default_caption=(
                "Dress the person in a traditional Indian sherwani. "
                "Preserve identity, pose, lighting, body shape, and background. "
                "Match garment structure, embroidery, buttons, fabric, and color."
            ),
            train_min_examples=_env_int("TRAIN_MIN_EXAMPLES_SHERWANI", 100),
            val_min_examples=_env_int("TRAIN_MIN_VAL_EXAMPLES_SHERWANI", 10),
            export_prefix=export_prefix,
            checkpoint_prefix=checkpoint_prefix,
            trainer_endpoint=trainer_endpoint,
            default_steps=_env_int("TRAIN_DEFAULT_STEPS_SHERWANI", 1200),
            default_learning_rate=_env_float("TRAIN_DEFAULT_LR_SHERWANI", 0.00005),
            min_target_stddev=_env_float("TRAIN_MIN_TARGET_STDDEV_SHERWANI", 0.015),
            min_person_target_mad=_env_float("TRAIN_MIN_PERSON_TARGET_MAD_SHERWANI", 0.010),
            min_person_target_center_mad=_env_float("TRAIN_MIN_PERSON_TARGET_CENTER_MAD_SHERWANI", 0.008),
        ),
    }


# -----------------------------------------------------------------------------
# Export result / training result
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportResult:
    zip_blob: str
    zip_sas_url: str
    summary_blob: str
    summary_sas_url: str
    kept: int
    rejected: int
    rejection_reasons: Dict[str, int]
    work_dir: str


@dataclass(frozen=True)
class FalSubmitResult:
    request_id: str
    post_url: str
    status_url: str
    result_url: str
    status_endpoint_id: str
    submit_payload: Dict[str, Any]
    submit_response: Dict[str, Any]


@dataclass(frozen=True)
class MirroredArtifactResult:
    lora_blob: str
    lora_az_path: str
    lora_sas_url: str
    config_blob: str
    config_az_path: str
    config_sas_url: str


# -----------------------------------------------------------------------------
# Main orchestrator
# -----------------------------------------------------------------------------


class TrainingOrchestrator:
    """
    Production-oriented orchestrator for Indian non-saree training.

    It intentionally wraps the current proven flow:
      dataset/training_examples -> export zip -> Fal trainer -> Azure mirror -> model_checkpoints

    It uses model_checkpoints as the current training lifecycle record to avoid a new migration.
    """

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        training_container: str = "commerce-training",
    ) -> None:
        self.pool = pool
        self.training_container = (training_container or "commerce-training").strip() or "commerce-training"
        self.az = _azure_ctx_from_conn_str(self.training_container)
        self.bsc = BlobServiceClient.from_connection_string(self.az.conn_str)

        self.fal_key = (_env_str("FAL_KEY") or _env_str("FAL_API_KEY")).strip()
        self.base_queue_url = _env_str("COMMERCE_FAL_BASE_URL", "https://queue.fal.run").rstrip("/")
        self.default_poll_secs = _env_float("COMMERCE_TRAIN_POLL_SECS", 5.0)
        self.default_poll_timeout_s = _env_int("COMMERCE_TRAIN_POLL_TIMEOUT_S", 60 * 60)
        self.sas_hours = _env_int("COMMERCE_TRAIN_SAS_HOURS", 72)
        self.validate_blob = _env_bool("DF_CHECKPOINT_VALIDATE_BLOB", default=True)
        self.min_checkpoint_bytes = _env_int("DF_CHECKPOINT_MIN_BYTES", 10 * 1024 * 1024)

        self._specs = _family_specs()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def start_training(
        self,
        *,
        dataset_id: UUID | str,
        family: str,
        split: str = "train",
        limit: Optional[int] = None,
        base_model: str = "fal-ai/flux-2",
        steps: Optional[int] = None,
        learning_rate: Optional[float] = None,
        default_caption: Optional[str] = None,
        force_new_run: bool = False,
        mirror_to_azure: bool = True,
    ) -> Dict[str, Any]:
        """
        Validates dataset, exports training zip, submits Fal training job,
        and persists a running checkpoint row in model_checkpoints.
        """
        spec = self._get_spec(family)
        dataset_id_s = str(dataset_id)

        ds = await self._get_dataset_row(dataset_id_s)
        if not ds:
            raise RuntimeError(f"training_dataset_not_found dataset_id={dataset_id_s}")
        if not bool(ds.get("is_frozen")):
            raise RuntimeError(f"training_dataset_not_frozen dataset_id={dataset_id_s}")

        counts = await self._get_dataset_counts(dataset_id_s, spec.accepted_tasks)
        train_count = int(_as_dict(counts.get("by_split")).get("train", 0))
        val_count = int(_as_dict(counts.get("by_split")).get("val", 0))

        if train_count < int(spec.train_min_examples):
            raise RuntimeError(
                f"training_dataset_too_small family={family} train_count={train_count} required={spec.train_min_examples}"
            )
        if val_count > 0 and val_count < int(spec.val_min_examples):
            raise RuntimeError(
                f"training_dataset_val_too_small family={family} val_count={val_count} required={spec.val_min_examples}"
            )

        if not force_new_run:
            existing = await self._find_existing_active_run(dataset_id_s=dataset_id_s, model_family=spec.model_family)
            if existing:
                return {
                    "reused_existing": True,
                    "checkpoint_id": str(existing["id"]),
                    "status": existing.get("status"),
                    "model_family": spec.model_family,
                    "dataset_id": dataset_id_s,
                    "artifacts_json": _as_dict(existing.get("artifacts_json")),
                }

        checkpoint_id = uuid4()
        run_steps = int(steps or spec.default_steps)
        run_lr = float(learning_rate or spec.default_learning_rate)
        run_caption = str(default_caption or spec.default_caption)

        hyperparams_json = {
            "steps": run_steps,
            "learning_rate": run_lr,
            "split": split,
            "limit": int(limit or 0),
            "mirror_to_azure": bool(mirror_to_azure),
        }
        config_json = {
            "dataset_id": dataset_id_s,
            "family": spec.family,
            "model_family": spec.model_family,
            "base_model": base_model,
            "trainer_endpoint": spec.trainer_endpoint,
            "default_caption": run_caption,
            "accepted_tasks": list(spec.accepted_tasks),
            "created_at": _utc_now(),
            "flow": "training_orchestrator_v1",
        }

        await self._insert_checkpoint_row(
            checkpoint_id=checkpoint_id,
            model_family=spec.model_family,
            base_model=base_model,
            status="queued",
            config_json=config_json,
            hyperparams_json=hyperparams_json,
            metrics_json={"dataset_counts": counts},
            artifacts_json={},
            notes="queued_for_export",
        )

        export = await self._export_training_zip(
            dataset_id=dataset_id_s,
            family=family,
            split=split,
            limit=limit,
            caption=run_caption,
        )

        await self._update_checkpoint_row(
            checkpoint_id=checkpoint_id,
            status="running",
            notes="export_ready_submitting_training",
            metrics_json={
                "dataset_counts": counts,
                "export": {
                    "kept": export.kept,
                    "rejected": export.rejected,
                    "rejection_reasons": export.rejection_reasons,
                },
            },
            artifacts_json={
                "export": {
                    "zip": {
                        "path": f"az://{self.training_container}/{export.zip_blob}",
                        "sas_url": export.zip_sas_url,
                    },
                    "summary": {
                        "path": f"az://{self.training_container}/{export.summary_blob}",
                        "sas_url": export.summary_sas_url,
                    },
                }
            },
        )

        submit = await self._submit_fal_training(
            zip_url=export.zip_sas_url,
            steps=run_steps,
            learning_rate=run_lr,
            default_caption=run_caption,
            trainer_endpoint=spec.trainer_endpoint,
        )

        await self._update_checkpoint_row(
            checkpoint_id=checkpoint_id,
            status="running",
            notes="provider_training_running",
            artifacts_json={
                "export": {
                    "zip": {
                        "path": f"az://{self.training_container}/{export.zip_blob}",
                        "sas_url": export.zip_sas_url,
                    },
                    "summary": {
                        "path": f"az://{self.training_container}/{export.summary_blob}",
                        "sas_url": export.summary_sas_url,
                    },
                },
                "provider": {
                    "name": "fal",
                    "request_id": submit.request_id,
                    "endpoint": spec.trainer_endpoint,
                    "status_endpoint_id": submit.status_endpoint_id,
                    "post_url": submit.post_url,
                    "status_url": submit.status_url,
                    "result_url": submit.result_url,
                    "submit_payload": submit.submit_payload,
                    "submit_response": submit.submit_response,
                },
            },
        )

        return {
            "checkpoint_id": str(checkpoint_id),
            "dataset_id": dataset_id_s,
            "family": family,
            "model_family": spec.model_family,
            "status": "running",
            "export": {
                "zip_sas_url": export.zip_sas_url,
                "summary_sas_url": export.summary_sas_url,
                "kept": export.kept,
                "rejected": export.rejected,
            },
            "provider": {
                "request_id": submit.request_id,
                "status_url": submit.status_url,
                "result_url": submit.result_url,
            },
        }

    async def poll_training(
        self,
        *,
        checkpoint_id: UUID | str,
        mirror_to_azure: bool = True,
    ) -> Dict[str, Any]:
        """
        Polls a running checkpoint row. If provider completed, mirrors artifacts,
        validates checkpoint blob, and marks succeeded/failed.
        """
        row = await self._get_checkpoint_row(str(checkpoint_id))
        if not row:
            raise RuntimeError(f"checkpoint_not_found checkpoint_id={checkpoint_id}")

        status = str(row.get("status") or "").strip().lower()
        if status in ("succeeded", "failed"):
            return {
                "checkpoint_id": str(row["id"]),
                "status": status,
                "artifacts_json": _as_dict(row.get("artifacts_json")),
                "metrics_json": _as_dict(row.get("metrics_json")),
            }

        artifacts = _as_dict(row.get("artifacts_json"))
        provider = _as_dict(artifacts.get("provider"))
        request_id = str(provider.get("request_id") or "").strip()
        status_url = str(provider.get("status_url") or "").strip()
        result_url = str(provider.get("result_url") or "").strip()
        endpoint = str(provider.get("endpoint") or "").strip()

        if not request_id or not status_url or not result_url:
            raise RuntimeError(f"checkpoint_missing_provider_state checkpoint_id={checkpoint_id}")

        st = await asyncio.to_thread(
            _http_json,
            "GET",
            status_url,
            headers={"Authorization": f"Key {self.fal_key}"},
            body=None,
            timeout_s=120,
        )
        provider_status = str(st.get("status") or "").upper()

        await self._update_checkpoint_row(
            checkpoint_id=UUID(str(row["id"])),
            status="running" if provider_status not in ("COMPLETED", "FAILED", "ERROR", "CANCELED", "CANCELLED") else status,
            notes=f"provider_status={provider_status}",
            artifacts_json={
                **artifacts,
                "provider": {
                    **provider,
                    "last_status": st,
                    "last_polled_at": _utc_now(),
                },
            },
        )

        if provider_status in ("FAILED", "ERROR", "CANCELED", "CANCELLED"):
            await self._update_checkpoint_row(
                checkpoint_id=UUID(str(row["id"])),
                status="failed",
                notes=f"provider_failed status={provider_status}",
                metrics_json={
                    **_as_dict(row.get("metrics_json")),
                    "provider_error": st,
                },
            )
            return {
                "checkpoint_id": str(row["id"]),
                "status": "failed",
                "provider_status": provider_status,
                "last_status": st,
            }

        if provider_status != "COMPLETED":
            return {
                "checkpoint_id": str(row["id"]),
                "status": "running",
                "provider_status": provider_status,
                "last_status": st,
            }

        out = await asyncio.to_thread(
            _http_json,
            "GET",
            result_url,
            headers={"Authorization": f"Key {self.fal_key}"},
            body=None,
            timeout_s=180,
        )

        diffusers_url, config_url = self._extract_fal_output_urls(out)
        if not diffusers_url:
            await self._update_checkpoint_row(
                checkpoint_id=UUID(str(row["id"])),
                status="failed",
                notes="provider_completed_but_missing_diffusers_lora_file_url",
                metrics_json={
                    **_as_dict(row.get("metrics_json")),
                    "provider_result": out,
                },
            )
            raise RuntimeError("provider_completed_but_missing_diffusers_lora_file_url")

        cfg = _as_dict(row.get("config_json"))
        dataset_id_s = str(cfg.get("dataset_id") or "unknown_dataset")
        family = str(cfg.get("family") or row.get("model_family") or "unknown_family")

        mirrored = await self._mirror_training_outputs(
            dataset_id=dataset_id_s,
            family=family,
            request_id=request_id,
            diffusers_url=diffusers_url,
            config_url=config_url,
            mirror_to_azure=mirror_to_azure,
        )

        final_artifacts = {
            **artifacts,
            "provider": {
                **provider,
                "result": out,
                "diffusers_lora_file_url": diffusers_url,
                "config_file_url": config_url,
            },
            "weights": {
                "path": mirrored.lora_az_path,
                "sas_url": mirrored.lora_sas_url,
                "source_url": diffusers_url,
            },
            "config": {
                "path": mirrored.config_az_path,
                "sas_url": mirrored.config_sas_url,
                "source_url": config_url,
            },
            "checkpoint_root": f"az://{self.training_container}/{os.path.dirname(mirrored.lora_blob)}",
        }

        valid = True
        if self.validate_blob:
            valid = _blob_exists_and_big_enough(
                conn_str=self.az.conn_str,
                az_path=mirrored.lora_az_path,
                min_bytes=self.min_checkpoint_bytes,
            )

        final_status = "succeeded" if valid else "failed"
        final_notes = "checkpoint_ready" if valid else "checkpoint_blob_invalid"

        await self._update_checkpoint_row(
            checkpoint_id=UUID(str(row["id"])),
            status=final_status,
            notes=final_notes,
            artifacts_json=final_artifacts,
            metrics_json={
                **_as_dict(row.get("metrics_json")),
                "provider_result_summary": {
                    "request_id": request_id,
                    "diffusers_url_found": bool(diffusers_url),
                    "config_url_found": bool(config_url),
                    "mirrored": True,
                    "blob_validated": bool(valid),
                },
            },
        )

        return {
            "checkpoint_id": str(row["id"]),
            "status": final_status,
            "provider_status": provider_status,
            "weights_path": mirrored.lora_az_path,
            "weights_sas_url": mirrored.lora_sas_url,
            "config_path": mirrored.config_az_path,
            "config_sas_url": mirrored.config_sas_url,
        }

    async def run_training(
        self,
        *,
        dataset_id: UUID | str,
        family: str,
        split: str = "train",
        limit: Optional[int] = None,
        base_model: str = "fal-ai/flux-2",
        steps: Optional[int] = None,
        learning_rate: Optional[float] = None,
        default_caption: Optional[str] = None,
        force_new_run: bool = False,
        mirror_to_azure: bool = True,
        poll_secs: Optional[float] = None,
        poll_timeout_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Convenience method: start + wait until terminal.
        """
        started = await self.start_training(
            dataset_id=dataset_id,
            family=family,
            split=split,
            limit=limit,
            base_model=base_model,
            steps=steps,
            learning_rate=learning_rate,
            default_caption=default_caption,
            force_new_run=force_new_run,
            mirror_to_azure=mirror_to_azure,
        )
        checkpoint_id = started["checkpoint_id"]

        every = float(poll_secs or self.default_poll_secs)
        timeout_s = int(poll_timeout_s or self.default_poll_timeout_s)
        t0 = time.time()

        while True:
            if time.time() - t0 > float(timeout_s):
                raise RuntimeError(f"training_poll_timeout checkpoint_id={checkpoint_id}")
            polled = await self.poll_training(checkpoint_id=checkpoint_id, mirror_to_azure=mirror_to_azure)
            s = str(polled.get("status") or "").lower()
            if s in ("succeeded", "failed"):
                return polled
            await asyncio.sleep(every)

    async def resume_incomplete_runs(
        self,
        *,
        limit: int = 10,
        mirror_to_azure: bool = True,
    ) -> List[Dict[str, Any]]:
        q = """
        select id
        from model_checkpoints
        where status in ('queued', 'running')
        order by created_at asc
        limit $1
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q, int(limit))

        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                out.append(await self.poll_training(checkpoint_id=str(r["id"]), mirror_to_azure=mirror_to_azure))
            except Exception as e:
                out.append({"checkpoint_id": str(r["id"]), "status": "error", "error": f"{type(e).__name__}: {e}"})
        return out

    # -------------------------------------------------------------------------
    # Dataset validation / export
    # -------------------------------------------------------------------------

    def _get_spec(self, family: str) -> FamilyTrainingSpec:
        key = str(family or "").strip().lower()
        spec = self._specs.get(key)
        if not spec:
            raise RuntimeError(f"unsupported_training_family family={family!r}")
        return spec

    async def _get_dataset_row(self, dataset_id_s: str) -> Optional[Dict[str, Any]]:
        q = """
        select id, name, kind, usage_scope, storage_container, storage_prefix,
               recipe_json, stats_json, is_frozen, created_at, updated_at
        from training_datasets
        where id = $1
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q, dataset_id_s)
        return dict(row) if row else None

    async def _get_dataset_counts(self, dataset_id_s: str, accepted_tasks: Sequence[str]) -> Dict[str, Any]:
        q = """
        select split, count(*)::bigint as n
        from training_examples
        where dataset_id = $1
          and task = any($2::text[])
        group by split
        order by split
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q, dataset_id_s, list(accepted_tasks))
        by_split = {str(r["split"]): int(r["n"]) for r in rows}
        total = sum(by_split.values())
        return {"dataset_id": dataset_id_s, "accepted_tasks": list(accepted_tasks), "total": total, "by_split": by_split}

    async def _fetch_examples(
        self,
        *,
        dataset_id: str,
        split: str,
        accepted_tasks: Sequence[str],
        limit: Optional[int],
    ) -> List[Dict[str, Any]]:
        q = """
        select
          id,
          split,
          task,
          person_ref,
          garment_refs,
          conditioning_refs,
          target_ref,
          mask_refs,
          labels_json,
          quality_json
        from training_examples
        where dataset_id = $1
          and split = $2
          and task = any($3::text[])
          and target_ref is not null
        order by created_at asc
        limit $4
        """
        lim = int(limit or 1000000)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q, dataset_id, split, list(accepted_tasks), lim)
        return [dict(r) for r in rows]

    def _passes_quality(self, row: Dict[str, Any], spec: FamilyTrainingSpec) -> Tuple[bool, Dict[str, Any]]:
        qj = _as_dict(row.get("quality_json"))
        metrics = _as_dict(qj.get("metrics"))
        reasons = [str(x) for x in _as_list(qj.get("reasons"))]

        if qj.get("ok") is False:
            return False, {"source": "quality_json.ok=false", "reasons": reasons, "metrics": metrics}

        fatal_markers = (
            "decode_failed",
            "target_identical_to_person_bytes",
            "target_identical_to_garment_bytes",
            "target_low_variance_blankish",
            "target_low_content_blankish",
            "target_empty_alpha",
        )
        if any(any(m in r for m in fatal_markers) for r in reasons):
            return False, {"source": "quality_json.reasons", "reasons": reasons, "metrics": metrics}

        stddev = metrics.get("target_stddev_norm")
        if stddev is not None:
            try:
                if float(stddev) < float(spec.min_target_stddev):
                    return False, {"source": "metrics.target_stddev_norm", "metrics": metrics}
            except Exception:
                pass

        mad = metrics.get("person_target_mad64")
        if mad is not None:
            try:
                if float(mad) < float(spec.min_person_target_mad):
                    return False, {"source": "metrics.person_target_mad64", "metrics": metrics}
            except Exception:
                pass

        center_mad = metrics.get("person_target_center_mad64")
        if center_mad is not None:
            try:
                if float(center_mad) < float(spec.min_person_target_center_mad):
                    return False, {"source": "metrics.person_target_center_mad64", "metrics": metrics}
            except Exception:
                pass

        return True, {"source": "accepted", "metrics": metrics}

    def _first_ref(self, x: Any, preferred_keys: Sequence[str] = ()) -> Optional[Dict[str, str]]:
        """
        Returns one of:
          {"type":"blob","container":"...","blob":"..."}
          {"type":"url","url":"https://..."}
        """
        if x is None:
            return None

        if isinstance(x, str):
            s = x.strip()
            az = _parse_az_path(s)
            if az:
                return {"type": "blob", "container": az[0], "blob": az[1]}
            if s.startswith("http://") or s.startswith("https://"):
                return {"type": "url", "url": s}
            return None

        if isinstance(x, dict):
            container = str(x.get("container") or "").strip()
            blob = str(x.get("blob") or "").strip()
            if container and blob:
                return {"type": "blob", "container": container, "blob": blob}

            path = str(x.get("path") or "").strip()
            az = _parse_az_path(path)
            if az:
                return {"type": "blob", "container": az[0], "blob": az[1]}

            for k in ("url", "sas_url", "image_url", "asset_url", "src"):
                v = x.get(k)
                if isinstance(v, str) and v.strip().startswith(("http://", "https://")):
                    return {"type": "url", "url": v.strip()}

            for k in preferred_keys:
                if k in x:
                    got = self._first_ref(x.get(k), preferred_keys=())
                    if got:
                        return got

            for v in x.values():
                got = self._first_ref(v, preferred_keys=())
                if got:
                    return got
            return None

        if isinstance(x, (list, tuple)):
            for it in x:
                got = self._first_ref(it, preferred_keys=preferred_keys)
                if got:
                    return got
            return None

        return None

    def _download_ref_bytes(self, ref: Dict[str, str]) -> bytes:
        if ref["type"] == "blob":
            return _download_blob_bytes(self.bsc, ref["container"], ref["blob"])
        if ref["type"] == "url":
            return _download_bytes(ref["url"], timeout_s=180)
        raise RuntimeError(f"unsupported_ref {ref!r}")

    def _resolve_example_assets(self, row: Dict[str, Any], family: str) -> Dict[str, Optional[Dict[str, str]]]:
        person_ref = _as_dict(row.get("person_ref"))
        garment_refs = _as_dict(row.get("garment_refs"))
        conditioning_refs = _as_dict(row.get("conditioning_refs"))
        target_ref = _as_dict(row.get("target_ref"))

        person = self._first_ref(person_ref, preferred_keys=("image", "person", "source"))
        target = self._first_ref(target_ref, preferred_keys=("target", "image", "result"))

        if family == "salwar_suit":
            primary = self._first_ref(
                garment_refs,
                preferred_keys=("primary", "salwar_suit", "overall", "garment", "image", "source"),
            )
        elif family == "lehenga_set":
            primary = self._first_ref(
                garment_refs,
                preferred_keys=("primary", "lehenga_set", "lehenga", "overall", "garment", "image"),
            )
        elif family == "kurta_pyjama":
            primary = self._first_ref(
                garment_refs,
                preferred_keys=("primary", "kurta_pyjama", "overall", "garment", "image"),
            )
        else:  # sherwani
            primary = self._first_ref(
                garment_refs,
                preferred_keys=("primary", "sherwani", "outer", "overall", "garment", "image"),
            )

        secondary = self._first_ref(
            conditioning_refs,
            preferred_keys=("composite", "secondary", "reference", "image", "conditioning"),
        )
        if not secondary:
            secondary = self._first_ref(
                garment_refs,
                preferred_keys=("secondary", "composite", "reference", "raw", "image"),
            )

        return {
            "person": person,
            "primary": primary,
            "secondary": secondary,
            "target": target,
        }

    async def _export_training_zip(
        self,
        *,
        dataset_id: str,
        family: str,
        split: str,
        limit: Optional[int],
        caption: str,
    ) -> ExportResult:
        spec = self._get_spec(family)
        rows = await self._fetch_examples(
            dataset_id=dataset_id,
            split=split,
            accepted_tasks=spec.accepted_tasks,
            limit=limit,
        )
        if not rows:
            raise RuntimeError(f"no_training_examples_for_export dataset_id={dataset_id} family={family} split={split}")

        work_dir = tempfile.mkdtemp(prefix=f"df_train_{family}_{dataset_id[:8]}_")
        data_dir = os.path.join(work_dir, "data")
        os.makedirs(data_dir, exist_ok=True)

        kept = 0
        rejected = 0
        rejection_reasons: Dict[str, int] = {}

        def _write(name: str, b: bytes) -> None:
            with open(os.path.join(data_dir, name), "wb") as f:
                f.write(b)

        for r in rows:
            ok, qdbg = self._passes_quality(r, spec)
            if not ok:
                rejected += 1
                key = str(qdbg.get("source") or "quality_rejected")
                rejection_reasons[key] = rejection_reasons.get(key, 0) + 1
                continue

            ex_id = str(r["id"])
            root = _safe_root(ex_id.replace("-", "")[:32])

            assets = self._resolve_example_assets(r, family)
            if not assets["person"] or not assets["primary"] or not assets["target"]:
                rejected += 1
                key = "missing_required_refs"
                rejection_reasons[key] = rejection_reasons.get(key, 0) + 1
                continue

            try:
                person_png = _ensure_png_bytes(await asyncio.to_thread(self._download_ref_bytes, assets["person"]))
                primary_png = _ensure_png_bytes(await asyncio.to_thread(self._download_ref_bytes, assets["primary"]))
                target_png = _ensure_png_bytes(await asyncio.to_thread(self._download_ref_bytes, assets["target"]))
                secondary_png = (
                    _ensure_png_bytes(await asyncio.to_thread(self._download_ref_bytes, assets["secondary"]))
                    if assets["secondary"]
                    else None
                )
            except Exception:
                rejected += 1
                key = "asset_download_or_decode_failed"
                rejection_reasons[key] = rejection_reasons.get(key, 0) + 1
                continue

            _write(f"{root}_start.png", person_png)
            _write(f"{root}_start2.png", primary_png)
            _write(f"{root}_end.png", target_png)

            # Keep the current training convention of optional extra conditioning images.
            if secondary_png:
                _write(f"{root}_start3.png", secondary_png)
            else:
                _write(f"{root}_start3.png", primary_png)

            with open(os.path.join(data_dir, f"{root}.txt"), "w", encoding="utf-8") as f:
                f.write(caption)

            kept += 1

        if kept <= 0:
            raise RuntimeError(f"all_examples_rejected family={family} dataset_id={dataset_id} reasons={rejection_reasons}")

        zip_name = f"{family}_flux2_edit_{dataset_id[:8]}_{split}_{kept}.zip"
        zip_path = os.path.join(work_dir, zip_name)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for fn in os.listdir(data_dir):
                z.write(os.path.join(data_dir, fn), arcname=fn)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        zip_blob = f"{spec.export_prefix.strip().strip('/')}/{family}/{dataset_id}/{ts}/{zip_name}"
        with open(zip_path, "rb") as f:
            _upload_blob_bytes(self.bsc, self.az.container, zip_blob, f.read(), "application/zip")

        zip_sas_url = _sas_url_for_blob(self.az, zip_blob, hours=int(self.sas_hours))

        summary = {
            "dataset_id": dataset_id,
            "family": family,
            "split": split,
            "limit": limit,
            "kept": kept,
            "rejected": rejected,
            "rejection_reasons": rejection_reasons,
            "zip": {
                "container": self.az.container,
                "blob": zip_blob,
                "sas_url": zip_sas_url,
            },
            "generated_at": _utc_now(),
        }

        summary_blob = f"{spec.export_prefix.strip().strip('/')}/{family}/{dataset_id}/{ts}/summary.json"
        _upload_blob_bytes(
            self.bsc,
            self.az.container,
            summary_blob,
            json.dumps(summary, indent=2).encode("utf-8"),
            "application/json",
        )
        summary_sas_url = _sas_url_for_blob(self.az, summary_blob, hours=int(self.sas_hours))

        return ExportResult(
            zip_blob=zip_blob,
            zip_sas_url=zip_sas_url,
            summary_blob=summary_blob,
            summary_sas_url=summary_sas_url,
            kept=kept,
            rejected=rejected,
            rejection_reasons=rejection_reasons,
            work_dir=work_dir,
        )

    # -------------------------------------------------------------------------
    # Fal trainer
    # -------------------------------------------------------------------------

    def _status_endpoint_id_for(self, endpoint_id: str) -> str:
        """
        Use top-level Fal model path for polling to avoid 405s on subpaths.
        Example:
          fal-ai/flux-2-trainer-v2/edit -> fal-ai/flux-2-trainer-v2
        """
        parts = [p for p in str(endpoint_id or "").split("/") if p]
        if len(parts) >= 2:
            return "/".join(parts[:2])
        return str(endpoint_id or "").strip("/")

    async def _submit_fal_training(
        self,
        *,
        zip_url: str,
        steps: int,
        learning_rate: float,
        default_caption: str,
        trainer_endpoint: str,
    ) -> FalSubmitResult:
        if not self.fal_key:
            raise RuntimeError("FAL_KEY (or FAL_API_KEY) is required")

        endpoint = str(trainer_endpoint or "").strip().strip("/")
        post_url = f"{self.base_queue_url}/{endpoint}"
        submit_payload = {
            "image_data_url": zip_url,
            "steps": int(steps),
            "learning_rate": float(learning_rate),
            "default_caption": str(default_caption),
        }

        submit = await asyncio.to_thread(
            _http_json,
            "POST",
            post_url,
            headers={"Authorization": f"Key {self.fal_key}"},
            body=submit_payload,
            timeout_s=180,
        )
        request_id = str(submit.get("request_id") or "").strip()
        if not request_id:
            raise RuntimeError(f"fal_submit_missing_request_id submit={submit}")

        status_ep = self._status_endpoint_id_for(endpoint)
        status_url = f"{self.base_queue_url}/{status_ep}/requests/{request_id}/status?logs=1"
        result_url = f"{self.base_queue_url}/{status_ep}/requests/{request_id}"

        return FalSubmitResult(
            request_id=request_id,
            post_url=post_url,
            status_url=status_url,
            result_url=result_url,
            status_endpoint_id=status_ep,
            submit_payload=submit_payload,
            submit_response=submit,
        )

    def _extract_fal_output_urls(self, out: Dict[str, Any]) -> Tuple[str, str]:
        diffusers_url = ""
        config_url = ""

        def scan(obj: Any) -> None:
            nonlocal diffusers_url, config_url
            if isinstance(obj, dict):
                if not diffusers_url:
                    u = (((obj.get("diffusers_lora_file") or {}) if isinstance(obj.get("diffusers_lora_file"), dict) else {}).get("url"))
                    if isinstance(u, str) and u.startswith("http"):
                        diffusers_url = u
                if not config_url:
                    u = (((obj.get("config_file") or {}) if isinstance(obj.get("config_file"), dict) else {}).get("url"))
                    if isinstance(u, str) and u.startswith("http"):
                        config_url = u
                for v in obj.values():
                    scan(v)
            elif isinstance(obj, list):
                for v in obj:
                    scan(v)

        scan(out)
        return diffusers_url, config_url

    async def _mirror_training_outputs(
        self,
        *,
        dataset_id: str,
        family: str,
        request_id: str,
        diffusers_url: str,
        config_url: str,
        mirror_to_azure: bool,
    ) -> MirroredArtifactResult:
        if not mirror_to_azure:
            raise RuntimeError("mirror_to_azure=false is not supported in production orchestrator path")

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        base_prefix = f"{_safe_root(_family_specs()[family].checkpoint_prefix)}/{family}/{dataset_id}/{ts}_{request_id}"
        # preserve full path prefix without applying safe_root to slashes
        base_prefix = f"{_family_specs()[family].checkpoint_prefix.strip().strip('/')}/{family}/{dataset_id}/{ts}_{request_id}"

        lora_bytes = await asyncio.to_thread(_download_bytes, diffusers_url, 600)
        lora_blob = f"{base_prefix}/diffusers_lora.safetensors"
        _upload_blob_bytes(self.bsc, self.training_container, lora_blob, lora_bytes, "application/octet-stream")
        lora_sas = _sas_url_for_blob(self.az, lora_blob, hours=int(self.sas_hours))

        cfg_blob = ""
        cfg_sas = ""
        if config_url:
            cfg_bytes = await asyncio.to_thread(_download_bytes, config_url, 300)
            cfg_blob = f"{base_prefix}/config.json"
            _upload_blob_bytes(self.bsc, self.training_container, cfg_blob, cfg_bytes, "application/json")
            cfg_sas = _sas_url_for_blob(self.az, cfg_blob, hours=int(self.sas_hours))

        return MirroredArtifactResult(
            lora_blob=lora_blob,
            lora_az_path=f"az://{self.training_container}/{lora_blob}",
            lora_sas_url=lora_sas,
            config_blob=cfg_blob,
            config_az_path=f"az://{self.training_container}/{cfg_blob}" if cfg_blob else "",
            config_sas_url=cfg_sas,
        )

    # -------------------------------------------------------------------------
    # model_checkpoints helpers
    # -------------------------------------------------------------------------

    async def _find_existing_active_run(
        self,
        *,
        dataset_id_s: str,
        model_family: str,
    ) -> Optional[Dict[str, Any]]:
        q = """
        select id, status, config_json, hyperparams_json, metrics_json, artifacts_json, notes, created_at
        from model_checkpoints
        where model_family = $1
          and status in ('queued', 'running')
          and coalesce(config_json->>'dataset_id', '') = $2
        order by created_at desc
        limit 1
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q, model_family, dataset_id_s)
        return dict(row) if row else None

    async def _insert_checkpoint_row(
        self,
        *,
        checkpoint_id: UUID,
        model_family: str,
        base_model: str,
        status: str,
        config_json: Dict[str, Any],
        hyperparams_json: Dict[str, Any],
        metrics_json: Dict[str, Any],
        artifacts_json: Dict[str, Any],
        notes: str,
    ) -> None:
        q = """
        insert into model_checkpoints
          (id, model_family, base_model, status, config_json, hyperparams_json, metrics_json, artifacts_json, notes)
        values
          ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9)
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                q,
                checkpoint_id,
                model_family,
                base_model,
                status,
                _json_dumps(config_json),
                _json_dumps(hyperparams_json),
                _json_dumps(metrics_json),
                _json_dumps(artifacts_json),
                notes,
            )

    async def _update_checkpoint_row(
        self,
        *,
        checkpoint_id: UUID,
        status: Optional[str] = None,
        notes: Optional[str] = None,
        config_json: Optional[Dict[str, Any]] = None,
        hyperparams_json: Optional[Dict[str, Any]] = None,
        metrics_json: Optional[Dict[str, Any]] = None,
        artifacts_json: Optional[Dict[str, Any]] = None,
    ) -> None:
        sets: List[str] = []
        args: List[Any] = [checkpoint_id]
        n = 2

        if status is not None:
            sets.append(f"status = ${n}")
            args.append(status)
            n += 1
        if notes is not None:
            sets.append(f"notes = ${n}")
            args.append(notes)
            n += 1
        if config_json is not None:
            sets.append(f"config_json = ${n}::jsonb")
            args.append(_json_dumps(config_json))
            n += 1
        if hyperparams_json is not None:
            sets.append(f"hyperparams_json = ${n}::jsonb")
            args.append(_json_dumps(hyperparams_json))
            n += 1
        if metrics_json is not None:
            sets.append(f"metrics_json = ${n}::jsonb")
            args.append(_json_dumps(metrics_json))
            n += 1
        if artifacts_json is not None:
            sets.append(f"artifacts_json = ${n}::jsonb")
            args.append(_json_dumps(artifacts_json))
            n += 1

        if not sets:
            return

        q = f"""
        update model_checkpoints
        set {", ".join(sets)}
        where id = $1
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, *args)

    async def _get_checkpoint_row(self, checkpoint_id_s: str) -> Optional[Dict[str, Any]]:
        q = """
        select id, model_family, base_model, status, created_at,
               config_json, hyperparams_json, metrics_json, artifacts_json, notes
        from model_checkpoints
        where id = $1
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q, checkpoint_id_s)
        return dict(row) if row else None