#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    v = (os.getenv(name) or "").strip()
    if not v:
        return default
    try:
        return int(float(v))
    except Exception:
        return default


def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _now_tag() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


@dataclass(frozen=True)
class Candidate:
    name: str
    endpoint_id: str
    input_json: Dict[str, Any]
    parse: str  # "image" or "images"


def fal_queue_run_and_wait(
    *,
    base_url: str,
    fal_key: str,
    endpoint_id: str,
    input_json: Dict[str, Any],
    timeout_s: int,
    poll_s: float,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    headers = {"Authorization": f"Key {fal_key}", "Accept": "application/json"}
    post_url = f"{base_url.rstrip('/')}/{endpoint_id.strip('/')}"
    r = requests.post(post_url, json=input_json, headers=headers, timeout=timeout_s)
    r.raise_for_status()
    submit = r.json() if r.content else {}
    request_id = str(submit.get("request_id") or "").strip()
    if not request_id:
        raise RuntimeError(f"fal submit missing request_id: {submit}")

    status_url = str(submit.get("status_url") or "").strip()
    result_url = str(submit.get("response_url") or "").strip()
    if not status_url.startswith("http"):
        # status endpoint is typically "org/model"
        parts = [p for p in endpoint_id.split("/") if p]
        status_ep = "/".join(parts[:2]) if len(parts) >= 2 else endpoint_id
        status_url = f"{base_url.rstrip('/')}/{status_ep}/requests/{request_id}/status"
    if not result_url.startswith("http"):
        parts = [p for p in endpoint_id.split("/") if p]
        status_ep = "/".join(parts[:2]) if len(parts) >= 2 else endpoint_id
        result_url = f"{base_url.rstrip('/')}/{status_ep}/requests/{request_id}"

    t0 = time.time()
    last_status: Dict[str, Any] = {}
    while True:
        st = requests.get(status_url, headers=headers, timeout=timeout_s).json()
        last_status = _as_dict(st)
        s = str(last_status.get("status") or "").upper()
        if s == "COMPLETED":
            break
        if time.time() - t0 > float(timeout_s):
            raise RuntimeError(f"fal timeout waiting COMPLETED: request_id={request_id} last_status={last_status}")
        time.sleep(poll_s)

    out = requests.get(result_url, headers=headers, timeout=timeout_s).json()
    dbg = {
        "endpoint_id": endpoint_id,
        "request_id": request_id,
        "post_url": post_url,
        "status_url": status_url,
        "result_url": result_url,
        "last_status": last_status,
    }
    return _as_dict(out), dbg


def parse_urls(out: Dict[str, Any], mode: str) -> List[str]:
    if mode == "image":
        img = _as_dict(out.get("image"))
        u = img.get("url")
        return [u] if isinstance(u, str) and u.startswith("http") else []
    if mode == "images":
        imgs = out.get("images")
        urls: List[str] = []
        if isinstance(imgs, list):
            for it in imgs:
                d = _as_dict(it)
                u = d.get("url")
                if isinstance(u, str) and u.startswith("http"):
                    urls.append(u)
        return urls
    return []


async def run_one(c: Candidate, *, base_url: str, fal_key: str, timeout_s: int, poll_s: float) -> Dict[str, Any]:
    def _work():
        out, dbg = fal_queue_run_and_wait(
            base_url=base_url,
            fal_key=fal_key,
            endpoint_id=c.endpoint_id,
            input_json=c.input_json,
            timeout_s=timeout_s,
            poll_s=poll_s,
        )
        urls = parse_urls(out, c.parse)
        head_status = None
        if urls:
            try:
                head_status = requests.head(urls[0], timeout=20, allow_redirects=True).status_code
            except Exception:
                head_status = None
        return {
            "name": c.name,
            "endpoint_id": c.endpoint_id,
            "input": c.input_json,
            "urls": urls,
            "first_url_head_status": head_status,
            "debug": dbg,
        }

    return await asyncio.to_thread(_work)


async def main() -> int:
    fal_key = _env("FAL_KEY") or _env("FAL_API_KEY") or _env("COMMERCE_FAL_KEY")
    if not fal_key:
        raise SystemExit("Missing FAL_KEY (or FAL_API_KEY / COMMERCE_FAL_KEY)")

    base_url = _env("COMMERCE_FAL_BASE_URL", "https://queue.fal.run").rstrip("/")

    # Default to your current sample assets; override via env as needed
    person_url = _env("VTON_PERSON_URL", "https://desifacesstore.blob.core.windows.net/desifaces-temp/sample_image.jpeg")
    garment_url = _env("VTON_GARMENT_URL", "https://desifacesstore.blob.core.windows.net/desifaces-temp/sample_saree.jpeg")

    # Important: IDM-VTON requires a description string. :contentReference[oaicite:8]{index=8}
    garment_desc = _env("VTON_GARMENT_DESCRIPTION", "Traditional Indian saree with pleats and pallu, Nivi drape")

    num_samples = _env_int("VTON_NUM_SAMPLES", 4)
    allow_research = _env_bool("VTON_ALLOW_RESEARCH_MODELS", False)

    timeout_s = _env_int("VTON_TIMEOUT_S", 240)
    poll_s = float(_env("VTON_POLL_S", "1.5") or "1.5")

    candidates: List[Candidate] = [
        Candidate(
            name="fashn_v1_5_onepieces_quality",
            endpoint_id="fal-ai/fashn/tryon/v1.5",
            input_json={
                "model_image": person_url,
                "garment_image": garment_url,
                "category": "one-pieces",
                "mode": "quality",
                "garment_photo_type": "auto",
                "moderation_level": "permissive",
                "num_samples": num_samples,
                "segmentation_free": True,
                "output_format": "png",
            },
            parse="images",
        ),
        Candidate(
            name="fashn_v1_6_onepieces_quality",
            endpoint_id="fal-ai/fashn/tryon/v1.6",
            input_json={
                "model_image": person_url,
                "garment_image": garment_url,
                "category": "one-pieces",
                "mode": "quality",
                "garment_photo_type": "auto",
                "moderation_level": "permissive",
                "num_samples": num_samples,
                "segmentation_free": True,
                "output_format": "png",
            },
            parse="images",
        ),
        Candidate(
            name="leffa_dresses",
            endpoint_id="fal-ai/leffa/virtual-tryon",
            input_json={
                "human_image_url": person_url,
                "garment_image_url": garment_url,
                "garment_type": "dresses",
                "num_inference_steps": 50,
                "guidance_scale": 2.5,
                "output_format": "png",
                "enable_safety_checker": True,
            },
            parse="image",
        ),
        Candidate(
            name="kling_kolors_vto",
            endpoint_id="fal-ai/kling/v1-5/kolors-virtual-try-on",
            input_json={
                "human_image_url": person_url,
                "garment_image_url": garment_url,
            },
            parse="image",
        ),
        Candidate(
            name="image_apps_v2_virtual_try_on_preserve_pose",
            endpoint_id="fal-ai/image-apps-v2/virtual-try-on",
            input_json={
                "person_image_url": person_url,
                "clothing_image_url": garment_url,
                "preserve_pose": True,
                "aspect_ratio": {"ratio": "3:4"},
            },
            parse="images",
        ),
    ]

    # Evaluation-only (licensing / research flags)
    if allow_research:
        candidates.extend(
            [
                Candidate(
                    name="idm_vton_eval_only",
                    endpoint_id="fal-ai/idm-vton",
                    input_json={
                        "human_image_url": person_url,
                        "garment_image_url": garment_url,
                        "description": garment_desc,
                        "num_inference_steps": 30,
                        "seed": 42,
                    },
                    parse="image",
                ),
                Candidate(
                    name="cat_vton_research_only",
                    endpoint_id="fal-ai/cat-vton",
                    input_json={
                        "human_image_url": person_url,
                        "garment_image_url": garment_url,
                        "cloth_type": "overall",
                        "image_size": "portrait_4_3",
                        "num_inference_steps": 30,
                        "guidance_scale": 2.5,
                    },
                    parse="image",
                ),
            ]
        )

    run_dir = f"/tmp/df_vton_bakeoff_{_now_tag()}"
    os.makedirs(run_dir, exist_ok=True)

    print(f"✅ VTON bake-off starting. Run dir: {run_dir}")
    print(f"FAL_BASE_URL={base_url}")
    print(f"person_url={person_url}")
    print(f"garment_url={garment_url}")
    print(f"num_samples={num_samples} allow_research={allow_research}")

    # Run concurrently but keep it safe (avoid throttling)
    sem = asyncio.Semaphore(int(_env_int("VTON_CONCURRENCY", 3)))

    async def guarded(c: Candidate) -> Dict[str, Any]:
        async with sem:
            t0 = time.time()
            try:
                out = await run_one(c, base_url=base_url, fal_key=fal_key, timeout_s=timeout_s, poll_s=poll_s)
                out["elapsed_s"] = round(time.time() - t0, 2)
                out["ok"] = bool(out.get("urls"))
                return out
            except Exception as e:
                return {
                    "name": c.name,
                    "endpoint_id": c.endpoint_id,
                    "input": c.input_json,
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "elapsed_s": round(time.time() - t0, 2),
                }

    results = await asyncio.gather(*[guarded(c) for c in candidates])

    with open(os.path.join(run_dir, "results.json"), "w") as f:
        json.dump(results, f, indent=2)

    summary = {
        "run_dir": run_dir,
        "person_url": person_url,
        "garment_url": garment_url,
        "num_samples": num_samples,
        "allow_research": allow_research,
        "results": [
            {
                "name": r.get("name"),
                "endpoint_id": r.get("endpoint_id"),
                "ok": r.get("ok"),
                "elapsed_s": r.get("elapsed_s"),
                "url_count": len(r.get("urls") or []),
                "first_url": (r.get("urls") or [None])[0],
                "first_url_head_status": r.get("first_url_head_status"),
                "error": r.get("error"),
            }
            for r in results
        ],
    }
    with open(os.path.join(run_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print("✅ Bake-off complete.")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))