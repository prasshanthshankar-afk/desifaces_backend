from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from app.services.azure_storage_service import AzureStorageConfig, AzureStorageService


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _as_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _as_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _fal_key() -> str:
    return (_env("FAL_KEY") or _env("FAL_API_KEY") or _env("COMMERCE_FAL_KEY")).strip()


def _http_json(
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    data: Optional[Dict[str, Any]] = None,
    timeout_s: int = 180,
) -> Dict[str, Any]:
    m = (method or "GET").strip().upper()
    hdrs: Dict[str, str] = {"Accept": "application/json"}
    hdrs.update(headers or {})

    body: Optional[bytes] = None
    if m not in ("GET", "HEAD") and data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = Request(url=url, method=m, headers=hdrs, data=body)

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
        raise RuntimeError(f"HTTPError code={e.code} url={url} body={txt[:800]}") from e
    except URLError as e:
        raise RuntimeError(f"URLError url={url} err={e}") from e


def _download_bytes(url: str, timeout_s: int = 180) -> bytes:
    req = Request(url=url, method="GET", headers={"User-Agent": "desifaces-svc-commerce/1.0"})
    with urlopen(req, timeout=timeout_s) as resp:
        return resp.read() or b""


def _parse_fal_images(out: Dict[str, Any]) -> List[Tuple[str, str]]:
    urls: List[Tuple[str, str]] = []
    imgs = out.get("images")
    if isinstance(imgs, list):
        for it in imgs:
            if isinstance(it, dict):
                u = it.get("url")
                ct = it.get("content_type") or "image/png"
                if isinstance(u, str) and u.startswith("http"):
                    urls.append((u, str(ct)))
    return urls


async def _fal_run_and_wait(
    *,
    base_url: str,
    model_id: str,
    input_json: Dict[str, Any],
    fal_key: str,
    poll_secs: float,
    poll_timeout_s: int,
    http_timeout_s: int,
    lifecycle_seconds: int = 7200,
) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Key {fal_key}",
        "X-Fal-Object-Lifecycle-Preference": json.dumps({"expiration_duration_seconds": lifecycle_seconds}),
    }

    post_url = f"{base_url.rstrip('/')}/{model_id.strip('/')}"
    submit = await asyncio.to_thread(_http_json, "POST", post_url, headers=headers, data=input_json, timeout_s=http_timeout_s)

    request_id = str(submit.get("request_id") or "").strip()
    if not request_id:
        raise RuntimeError(f"fal queue missing request_id. submit={submit}")

    status_url = str(submit.get("status_url") or "").strip()
    result_url = str(submit.get("response_url") or "").strip()
    if not status_url.startswith("http"):
        status_url = f"{base_url.rstrip('/')}/{model_id.strip('/')}/requests/{request_id}/status"
    if not result_url.startswith("http"):
        result_url = f"{base_url.rstrip('/')}/{model_id.strip('/')}/requests/{request_id}"

    t0 = time.time()
    while True:
        st = await asyncio.to_thread(_http_json, "GET", status_url, headers=headers, data=None, timeout_s=http_timeout_s)
        s = str(st.get("status") or "").upper()
        if s == "COMPLETED":
            break
        if time.time() - t0 > float(poll_timeout_s):
            raise RuntimeError(f"fal queue timeout waiting COMPLETED. request_id={request_id} last={st}")
        await asyncio.sleep(poll_secs)

    out = await asyncio.to_thread(_http_json, "GET", result_url, headers=headers, data=None, timeout_s=http_timeout_s)
    return out if isinstance(out, dict) else {"raw": out}


def _guess_ext(ct: str) -> str:
    ct = (ct or "").lower()
    if "png" in ct:
        return "png"
    if "webp" in ct:
        return "webp"
    return "jpg"


def _upload_json(storage: AzureStorageService, *, container: str, blob: str, obj: Any) -> str:
    data = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
    return storage.upload_bytes(data=data, blob_name=blob, content_type="application/json", container_name=container)


@dataclass(frozen=True)
class GenSpec:
    kind: str
    prompt: str
    image_size: Any
    output_format: str = "png"
    num_inference_steps: int = 8
    guidance_scale: float = 3.5
    enable_safety_checker: bool = True


def _make_person_prompts(rng: random.Random, n: int) -> List[str]:
    genders = ["woman", "man"]
    ages = ["young adult", "adult", "middle-aged adult"]
    outfits = [
        "fitted neutral athletic wear (leggings and fitted top)",
        "fitted neutral bodysuit",
        "simple fitted t-shirt and fitted jeans",
    ]
    poses = [
        "standing front-facing, arms slightly away from the body",
        "standing 3/4 view, arms relaxed at sides",
        "standing front-facing, hands relaxed, no accessories",
    ]
    skin = [
        "South Asian skin tones",
        "diverse skin tones",
        "medium-brown skin tone",
    ]

    out: List[str] = []
    for _ in range(n):
        g = rng.choice(genders)
        a = rng.choice(ages)
        o = rng.choice(outfits)
        p = rng.choice(poses)
        st = rng.choice(skin)
        out.append(
            f"Photorealistic full-body studio photo of a fictional {a} {g}, {st}. "
            f"{p}. Wearing {o}. Plain light gray seamless backdrop. Soft even studio lighting. "
            "High detail. Realistic anatomy. No text. No watermark. Not a celebrity."
        )
    return out


def _make_fabric_prompts(rng: random.Random, n: int, kind: str) -> List[str]:
    saree_types = ["Kanjivaram silk", "Banarasi silk", "cotton saree fabric", "georgette saree fabric", "chiffon saree fabric"]
    motifs = ["paisley", "floral", "temple border", "zari brocade", "geometric", "butta motifs"]
    colors = ["rich red and gold", "emerald green and gold", "royal blue and silver", "magenta and gold", "mustard and maroon"]

    out: List[str] = []
    for _ in range(n):
        t = rng.choice(saree_types)
        m = rng.choice(motifs)
        c = rng.choice(colors)

        if kind == "pallu":
            out.append(
                f"High-resolution textile design for an Indian saree pallu. {t}. {m}. {c}. "
                "Long vertical strip composition with ornate border and dense patterning. "
                "Flat-lay, no people, no mannequin, no folds, even lighting, ultra sharp, no text, no watermark."
            )
        elif kind == "blouse":
            out.append(
                f"Photorealistic flat-lay product photo of an Indian saree blouse. Matching {t} style. {c}. "
                "Short sleeves. Front view. On a clean white background. No person. No mannequin head. "
                "Studio lighting. No text, no watermark."
            )
        else:
            out.append(
                f"High-resolution flat-lay textile swatch for an Indian saree body fabric. {t}. {m}. {c}. "
                "No people. No folds. Even lighting. Ultra sharp. No text. No watermark."
            )
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()

    ap.add_argument("--batch_id", default=f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}")
    ap.add_argument("--fal_base_url", default=_env("COMMERCE_FAL_BASE_URL", "https://queue.fal.run"))
    ap.add_argument("--model_id", default=_env("DF_TRAIN_T2I_MODEL_ID", "fal-ai/flux-2-pro"))

    ap.add_argument("--num_persons", type=int, default=_as_int(_env("DF_POOL_PERSONS", "200"), 200))
    ap.add_argument("--num_sarees", type=int, default=_as_int(_env("DF_POOL_SAREES", "200"), 200))
    ap.add_argument("--num_blouses", type=int, default=_as_int(_env("DF_POOL_BLOUSES", "200"), 200))
    ap.add_argument("--num_pallus", type=int, default=_as_int(_env("DF_POOL_PALLUS", "200"), 200))

    ap.add_argument("--concurrency", type=int, default=_as_int(_env("DF_POOL_CONCURRENCY", "2"), 2))
    ap.add_argument("--seed", type=int, default=_as_int(_env("DF_POOL_SEED", "123"), 123))

    ap.add_argument("--image_size_person", default=_env("DF_POOL_IMAGE_SIZE_PERSON", "portrait_4_3"))
    ap.add_argument("--image_size_fabric", default=_env("DF_POOL_IMAGE_SIZE_FABRIC", "portrait_4_3"))
    ap.add_argument("--output_format", default=_env("DF_POOL_OUTPUT_FORMAT", "png"))
    ap.add_argument("--steps", type=int, default=_as_int(_env("DF_POOL_STEPS", "8"), 8))
    ap.add_argument("--guidance", type=float, default=_as_float(_env("DF_POOL_GUIDANCE", "3.5"), 3.5))
    ap.add_argument("--no_safety", action="store_true", default=False)

    ap.add_argument("--container", default=_env("DF_TRAINING_CONTAINER", "commerce-training"))
    ap.add_argument("--prefix", default=_env("DF_POOL_PREFIX", "pools"))  # root prefix, not including batch_id

    args = ap.parse_args()

    fal_key = _fal_key()
    if not fal_key:
        raise SystemExit("Missing FAL_KEY (or FAL_API_KEY / COMMERCE_FAL_KEY)")

    conn = _env("AZURE_STORAGE_CONNECTION_STRING") or _env("COMMERCE_AZURE_STORAGE_CONNECTION_STRING")
    if not conn:
        raise SystemExit("Missing AZURE_STORAGE_CONNECTION_STRING")

    storage = AzureStorageService(
        config=AzureStorageConfig(connection_string=conn, container=args.container, default_sas_hours=24)
    )

    # final prefix layout: <prefix>/<batch_id>/...
    root = (args.prefix or "pools").strip().strip("/")
    base_prefix = f"{root}/{args.batch_id}".strip("/")

    poll_secs = _as_float(_env("DF_POOL_POLL_SECS", "1.2"), 1.2)
    poll_timeout_s = _as_int(_env("DF_POOL_POLL_TIMEOUT_S", "900"), 900)
    http_timeout_s = _as_int(_env("DF_POOL_HTTP_TIMEOUT_S", "180"), 180)

    rng = random.Random(args.seed)

    specs: List[GenSpec] = []
    for p in _make_person_prompts(rng, args.num_persons):
        specs.append(
            GenSpec(
                kind="persons",
                prompt=p,
                image_size=args.image_size_person,
                output_format=args.output_format,
                num_inference_steps=args.steps,
                guidance_scale=args.guidance,
                enable_safety_checker=(not args.no_safety),
            )
        )
    for p in _make_fabric_prompts(rng, args.num_sarees, "saree"):
        specs.append(
            GenSpec(
                kind="sarees",
                prompt=p,
                image_size=args.image_size_fabric,
                output_format=args.output_format,
                num_inference_steps=args.steps,
                guidance_scale=args.guidance,
                enable_safety_checker=(not args.no_safety),
            )
        )
    for p in _make_fabric_prompts(rng, args.num_blouses, "blouse"):
        specs.append(
            GenSpec(
                kind="blouses",
                prompt=p,
                image_size=args.image_size_fabric,
                output_format=args.output_format,
                num_inference_steps=args.steps,
                guidance_scale=args.guidance,
                enable_safety_checker=(not args.no_safety),
            )
        )
    for p in _make_fabric_prompts(rng, args.num_pallus, "pallu"):
        specs.append(
            GenSpec(
                kind="pallus",
                prompt=p,
                image_size=args.image_size_fabric,
                output_format=args.output_format,
                num_inference_steps=args.steps,
                guidance_scale=args.guidance,
                enable_safety_checker=(not args.no_safety),
            )
        )

    sem = asyncio.Semaphore(max(1, args.concurrency))
    items_by_kind: Dict[str, List[Dict[str, Any]]] = {"persons": [], "sarees": [], "blouses": [], "pallus": []}
    failures: List[Dict[str, Any]] = []

    async def run_one(ix: int, spec: GenSpec) -> None:
        async with sem:
            try:
                seed = int(_sha256_str(f"{args.batch_id}:{ix}:{spec.kind}")[:8], 16) & 0x7FFFFFFF

                payload = {
                    "prompt": spec.prompt,
                    "image_size": spec.image_size,
                    "seed": seed,
                    "num_images": 1,
                    "output_format": "png" if (spec.output_format or "").lower() == "png" else "jpeg",
                    "num_inference_steps": int(spec.num_inference_steps),
                    "guidance_scale": float(spec.guidance_scale),
                    "enable_safety_checker": bool(spec.enable_safety_checker),
                }

                out = await _fal_run_and_wait(
                    base_url=args.fal_base_url,
                    model_id=args.model_id,
                    input_json=payload,
                    fal_key=fal_key,
                    poll_secs=poll_secs,
                    poll_timeout_s=poll_timeout_s,
                    http_timeout_s=http_timeout_s,
                    lifecycle_seconds=7200,
                )

                urls = _parse_fal_images(out)
                if not urls:
                    raise RuntimeError(f"no images in output: keys={list(out.keys())}")

                src_url, content_type = urls[0]
                img_bytes = await asyncio.to_thread(_download_bytes, src_url, 180)
                if not img_bytes or len(img_bytes) < 2048:
                    raise RuntimeError("downloaded image too small/empty")

                ext = _guess_ext(content_type)
                blob = f"{base_prefix}/{spec.kind}/{ix:06d}_{seed}.{ext}"

                sas_url = storage.upload_bytes(
                    data=img_bytes,
                    blob_name=blob,
                    content_type=content_type or f"image/{ext}",
                    container_name=args.container,
                )

                items_by_kind[spec.kind].append(
                    {
                        "container": args.container,
                        "blob": blob,
                        "url": sas_url,  # convenience
                        "content_type": content_type or f"image/{ext}",
                        "bytes": len(img_bytes),
                        "sha256": _sha256_bytes(img_bytes),
                        "seed": seed,
                    }
                )

            except Exception as e:
                failures.append({"i": ix, "kind": spec.kind, "err": f"{type(e).__name__}: {e}"})

    await asyncio.gather(*[run_one(i, s) for i, s in enumerate(specs)])

    # stable ordering
    for k in list(items_by_kind.keys()):
        items_by_kind[k] = sorted(items_by_kind[k], key=lambda x: str(x.get("blob") or ""))

    # Upload JSON pools + manifest to Azure (production-grade)
    persons_blob = f"{base_prefix}/persons.json"
    sarees_blob = f"{base_prefix}/sarees.json"
    blouses_blob = f"{base_prefix}/blouses.json"
    pallus_blob = f"{base_prefix}/pallus.json"
    manifest_blob = f"{base_prefix}/source_pools.json"
    summary_blob = f"{base_prefix}/summary.json"

    persons_url = _upload_json(storage, container=args.container, blob=persons_blob, obj=items_by_kind["persons"])
    sarees_url = _upload_json(storage, container=args.container, blob=sarees_blob, obj=items_by_kind["sarees"])
    blouses_url = _upload_json(storage, container=args.container, blob=blouses_blob, obj=items_by_kind["blouses"])
    pallus_url = _upload_json(storage, container=args.container, blob=pallus_blob, obj=items_by_kind["pallus"])

    manifest = {
        "batch_id": args.batch_id,
        "container": args.container,
        "prefix": base_prefix,
        "counts": {k: len(v) for k, v in items_by_kind.items()},
        "az": {
            "persons": f"az://{args.container}/{persons_blob}",
            "sarees": f"az://{args.container}/{sarees_blob}",
            "blouses": f"az://{args.container}/{blouses_blob}",
            "pallus": f"az://{args.container}/{pallus_blob}",
        },
        "urls": {
            "persons": persons_url,
            "sarees": sarees_url,
            "blouses": blouses_url,
            "pallus": pallus_url,
        },
    }
    manifest_url = _upload_json(storage, container=args.container, blob=manifest_blob, obj=manifest)

    summary = {
        "batch_id": args.batch_id,
        "model_id": args.model_id,
        "fal_base_url": args.fal_base_url,
        "container": args.container,
        "prefix": base_prefix,
        "counts": {k: len(v) for k, v in items_by_kind.items()},
        "failures_sample": failures[:30],
        "az": {
            "manifest": f"az://{args.container}/{manifest_blob}",
            "persons": f"az://{args.container}/{persons_blob}",
            "sarees": f"az://{args.container}/{sarees_blob}",
            "blouses": f"az://{args.container}/{blouses_blob}",
            "pallus": f"az://{args.container}/{pallus_blob}",
        },
        "urls": {
            "manifest": manifest_url,
            "persons": persons_url,
            "sarees": sarees_url,
            "blouses": blouses_url,
            "pallus": pallus_url,
        },
    }
    _upload_json(storage, container=args.container, blob=summary_blob, obj=summary)

    print("✅ Pools created + uploaded.")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))