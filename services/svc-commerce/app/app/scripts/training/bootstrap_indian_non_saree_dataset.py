from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.db import get_pool
from app.services.azure_storage_service import AzureStorageService
from app.services.training.quality_gates import evaluate_example
from app.services.training.training_dataset_service import TrainingDatasetService


def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _load_manifest(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"manifest_not_found path={path}")

    if p.suffix.lower() == ".jsonl":
        rows: List[Dict[str, Any]] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s:
                continue
            rows.append(json.loads(s))
        return rows

    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        raise RuntimeError("json_manifest_must_be_list")

    raise RuntimeError("manifest must be .json or .jsonl")


def _split_for_index(i: int, total: int) -> str:
    if total <= 1:
        return "train"
    # ~90 / 8 / 2
    if i >= int(total * 0.98):
        return "test"
    if i >= int(total * 0.90):
        return "val"
    return "train"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-id", required=True)
    ap.add_argument("--family", required=True, choices=["salwar_suit", "lehenga_set", "kurta_pyjama", "sherwani"])
    ap.add_argument("--manifest", required=True, help="Path to JSON or JSONL manifest")
    ap.add_argument("--training-container", default="commerce-training")
    args = ap.parse_args()

    task = f"{args.family}_tryon"

    rows = _load_manifest(args.manifest)
    if not rows:
        raise RuntimeError("manifest_empty")

    pool = await get_pool()
    storage = AzureStorageService()
    svc = TrainingDatasetService(
        pool=pool,
        storage=storage,
        training_container=args.training_container,
    )

    ds = await svc.get_dataset_row(dataset_id=args.dataset_id)
    if not ds:
        raise RuntimeError(f"dataset_not_found dataset_id={args.dataset_id}")

    dataset_prefix = str(ds["storage_prefix"])
    stats = Counter()
    inserted_ids: List[str] = []

    for i, row in enumerate(rows):
        person_url = str(row.get("person_url") or "").strip()
        garment_url = str(row.get("garment_url") or "").strip()
        target_url = str(row.get("target_url") or "").strip()
        composite_url = str(row.get("composite_url") or "").strip()

        if not person_url or not garment_url or not target_url:
            stats["missing_required_urls"] += 1
            continue

        try:
            person_ref = svc.cache_source_url(
                url=person_url,
                dataset_prefix=dataset_prefix,
                kind=f"{args.family}/person",
                timeout_s=90,
            )
            garment_ref = svc.cache_source_url(
                url=garment_url,
                dataset_prefix=dataset_prefix,
                kind=f"{args.family}/garment",
                timeout_s=90,
            )
            target_ref = svc.cache_source_url(
                url=target_url,
                dataset_prefix=dataset_prefix,
                kind=f"{args.family}/target",
                timeout_s=90,
            )
            composite_ref: Optional[Any] = None
            if composite_url:
                composite_ref = svc.cache_source_url(
                    url=composite_url,
                    dataset_prefix=dataset_prefix,
                    kind=f"{args.family}/conditioning",
                    timeout_s=90,
                )

            person_bytes = svc.download_url_bytes(person_url, timeout_s=90)
            garment_bytes = svc.download_url_bytes(garment_url, timeout_s=90)
            target_bytes = svc.download_url_bytes(target_url, timeout_s=90)

            quality = evaluate_example(
                person_bytes=person_bytes,
                garment_bytes=garment_bytes,
                target_bytes=target_bytes,
            )

            split = str(row.get("split") or _split_for_index(i, len(rows))).strip().lower()
            if split not in {"train", "val", "test"}:
                split = "train"

            ex_id = await svc.insert_example(
                dataset_id=args.dataset_id,
                template_id=None,
                split=split,
                task=task,
                person_ref=person_ref.as_json(),
                garment_refs={
                    "primary": garment_ref.as_json(),
                },
                conditioning_refs={
                    "composite": composite_ref.as_json(),
                } if composite_ref else {},
                target_ref=target_ref.as_json(),
                mask_refs={},
                labels_json={
                    "family": args.family,
                    "task": task,
                    "gender": row.get("gender"),
                    "pose": row.get("pose"),
                    "source": row.get("source", "bootstrap_manifest"),
                },
                quality_json={
                    "ok": quality.ok,
                    "reasons": list(quality.reasons),
                    "metrics": quality.metrics,
                },
                consent_json={
                    "synthetic": bool(row.get("synthetic", True)),
                    "source": row.get("source", "bootstrap_manifest"),
                },
            )
            inserted_ids.append(str(ex_id))
            stats["processed"] += 1
            stats[f"split_{split}"] += 1
            if quality.ok:
                stats["quality_ok"] += 1
            else:
                stats["quality_not_ok"] += 1

        except Exception as e:
            stats["insert_failed"] += 1
            print(f"[WARN] row={i} failed: {type(e).__name__}: {e}")

    final_stats = await svc.compute_dataset_stats(dataset_id=args.dataset_id)
    final_stats["bootstrap_manifest"] = args.manifest
    final_stats["family"] = args.family
    final_stats["task"] = task
    final_stats["ingest_stats"] = dict(stats)

    await svc.finalize_dataset_stats(
        dataset_id=args.dataset_id,
        stats_json=final_stats,
        freeze=True,
    )

    print("DONE")
    print("DATASET_ID=", args.dataset_id)
    print("TASK=", task)
    print("INSERTED_COUNT=", len(inserted_ids))
    print("STATS=", json.dumps(dict(stats), indent=2))
    print("FINAL_STATS=", json.dumps(final_stats, indent=2))


if __name__ == "__main__":
    asyncio.run(main())