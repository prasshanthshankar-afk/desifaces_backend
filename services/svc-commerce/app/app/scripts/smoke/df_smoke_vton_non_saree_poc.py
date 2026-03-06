#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse


# ----------------------------
# small utils
# ----------------------------

def _utc_stamp() -> str:
    # Python 3.14+: utcnow() deprecated
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S")


def _mkdirp(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _read_json(path: str) -> Dict[str, Any]:
    return json.load(open(path, "r", encoding="utf-8"))


def _write_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True, ensure_ascii=False, default=str)


def _b64url_decode(s: str) -> bytes:
    s2 = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s2.encode("utf-8"))


def _jwt_sub(token: str) -> Optional[str]:
    """
    token payload has {"sub": "<uuid>"} in your svc-core JWT.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        sub = payload.get("sub")
        return str(sub) if sub else None
    except Exception:
        return None


def _run(cmd: List[str], *, capture: bool = True) -> Tuple[int, str, str]:
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )
    out = p.stdout or ""
    err = p.stderr or ""
    return p.returncode, out, err


def _curl_json(cmd: List[str]) -> Dict[str, Any]:
    rc, out, err = _run(cmd, capture=True)
    if rc != 0:
        raise RuntimeError(f"curl failed rc={rc}\nCMD: {' '.join(cmd)}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    try:
        return json.loads(out)
    except Exception as e:
        raise RuntimeError(f"curl returned non-JSON: {e}\nOUT:\n{out}\nERR:\n{err}")


def _commons_file(filename: str) -> str:
    """
    Wikimedia 'Special:FilePath' is far more stable than hardcoding upload.wikimedia.org paths.
    """
    fn = filename.replace(" ", "_")
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(fn)}"


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    return [x]


def _guess_ext_from_url(url: str, fallback: str = ".jpg") -> str:
    try:
        path = urlparse(url).path
        base = os.path.basename(path)
        if "." in base:
            ext = "." + base.split(".")[-1].lower()
            if 1 <= len(ext) <= 6:
                return ext
    except Exception:
        pass
    return fallback


def _download_one(url: str, dst: str, timeout_s: int = 60) -> None:
    """
    Single-attempt downloader:
    - browser-ish UA
    - follows redirects (urllib default handlers)
    """
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read()
    if not data or len(data) < 1024:
        # avoids saving tiny HTML error bodies as "images"
        raise RuntimeError(f"Downloaded too little data ({len(data) if data else 0} bytes) from {url}")
    with open(dst, "wb") as f:
        f.write(data)


def _download(urls: Any, dst: str, timeout_s: int = 60, retries: int = 5) -> str:
    """
    Robust downloader:
    - tries multiple candidate URLs (if provided)
    - retries each URL on transient errors
    Returns the URL that succeeded.
    """
    candidates = [str(u) for u in _as_list(urls) if u]
    if not candidates:
        raise RuntimeError("No download URLs provided")

    last_err: Optional[Exception] = None

    for url in candidates:
        for i in range(retries):
            try:
                _download_one(url, dst, timeout_s=timeout_s)
                return url
            except Exception as e:
                last_err = e
                time.sleep(1.5 * (i + 1))

    raise RuntimeError(f"Download failed after trying {len(candidates)} url(s) x {retries} retries.\nLast error: {last_err}")


def _probe_url_head(url: str) -> Dict[str, Any]:
    """
    HEAD probe (best-effort). Returns {ok, http_code, content_type}.
    """
    rc, out, err = _run(["curl", "-sS", "-I", "-L", url], capture=True)
    if rc != 0:
        return {"ok": False, "http_code": None, "content_type": None, "err": (err or out)[:500]}

    http_code: Optional[int] = None
    content_type: Optional[str] = None
    for line in out.splitlines():
        s = line.strip()
        if s.lower().startswith("http/"):
            # e.g. HTTP/2 200
            parts = s.split()
            if len(parts) >= 2 and parts[1].isdigit():
                http_code = int(parts[1])
        if s.lower().startswith("content-type:"):
            content_type = s.split(":", 1)[1].strip()
    ok = (http_code is not None) and (200 <= http_code < 400)
    return {"ok": ok, "http_code": http_code, "content_type": content_type}


def _collect_urls(obj: Any, *, limit: int = 50) -> List[str]:
    """
    Recursively collect any string values that look like URLs from nested dict/list payloads.
    """
    out: List[str] = []

    def rec(x: Any) -> None:
        if len(out) >= limit:
            return
        if isinstance(x, str):
            s = x.strip()
            if s.startswith("http://") or s.startswith("https://"):
                out.append(s)
            return
        if isinstance(x, dict):
            for _, v in x.items():
                rec(v)
            return
        if isinstance(x, list) or isinstance(x, tuple):
            for v in x:
                rec(v)
            return

    rec(obj)
    # de-dupe in order
    seen = set()
    uniq: List[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


# ----------------------------
# Default garment cases (Indian + Western)
# Uses Wikimedia Commons Special:FilePath to avoid 404s.
# ----------------------------

DEFAULT_CASES: List[Dict[str, Any]] = [
    # --------------------
    # WESTERN (tops/outer)
    # --------------------
    {
        "name": "hoodie_black",
        "component_code": "hoodie",
        "url": _commons_file("Hoodie m7agar.jpg"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "Hoodie should appear on platform model; sleeves/hood preserved."},
    },
    {
        "name": "blazer_rack",
        "component_code": "blazer",
        "url": _commons_file("Blazer Jackets on Rack (Unsplash).jpg"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "Blazer/jacket styling on platform model; lapels visible."},
    },
    {
        "name": "coat_red",
        "component_code": "coat",
        "url": _commons_file("Man-red-coat-mountains (Unsplash).jpg"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "Coat should be retained; avoid turning into hoodie/suit."},
    },
    {
        "name": "sweater",
        "component_code": "sweater",
        "url": _commons_file("Sweater (Unsplash).jpg"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "Sweater knit/fit should remain; avoid changing into jacket."},
    },
    {
        "name": "tshirt_red",
        "component_code": "tshirt",
        "url": _commons_file("Man in t-shirt posing (Unsplash).jpg"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "T-shirt should appear; minimal hallucinated layers."},
    },
    {
        "name": "shirt",
        "component_code": "shirt",
        "url": _commons_file("Man in shirt posing (Unsplash).jpg"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "Shirt collar/buttons plausible; avoid morphing into t-shirt."},
    },
    {
        "name": "suit_plaid",
        "component_code": "suit",
        "url": _commons_file("Suit and tie (Unsplash).jpg"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "Suit silhouette should remain; tie optional depending on provider."},
    },

    # --------------------
    # WESTERN (bottoms / full outfits)
    # --------------------
    {
        "name": "jeans_pair",
        "component_code": "jeans",
        "url": _commons_file("Jeans.jpg"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "Jeans texture retained; avoid turning into leggings."},
    },
    {
        "name": "skirt",
        "component_code": "skirt",
        "url": _commons_file("Woman holding out her skirt (Unsplash).jpg"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "Skirt length/flow retained; avoid turning into dress."},
    },
    {
        "name": "dress",
        "component_code": "dress",
        "url": _commons_file("Woman in a dress (Unsplash).jpg"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "Dress should stay a single-piece outfit; avoid turning into suit."},
    },
    {
        "name": "jumpsuit",
        "component_code": "jumpsuit",
        "url": _commons_file("Turquoise Jumpsuit (Unsplash).jpg"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "One-piece jumpsuit; avoid splitting into top+bottom mismatches."},
    },

    # --------------------
    # INDIAN (non-saree)
    # --------------------
    {
        "name": "kurta_blue_khadi",
        "component_code": "kurta",
        "url": _commons_file("Blue khadi kurta.jpg"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "Kurta length + collar retained; avoid western shirt conversion."},
    },
    {
        "name": "salwar_kameez",
        "component_code": "salwar_suit",
        "url": _commons_file("Shalwar kameez Colours.jpg"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "Salwar suit feel retained (kameez + salwar); dupatta optional."},
    },
    {
        "name": "lehenga_blue",
        "component_code": "lehenga",
        "url": _commons_file("Ethnic wear for women Blue Lehenga.png"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "Lehenga silhouette (skirt+blouse) retained; avoid gown/dress drift."},
    },
    {
        "name": "sherwani",
        "component_code": "sherwani",
        "url": _commons_file("Sherwani.jpg"),
        "role": "garment_full",
        "expected": {"min_outputs": 1, "desired_outputs": 4, "notes": "Sherwani long-coat look retained; avoid generic blazer."},
    },
]


def _login(core_url: str, email: str, password: str) -> Tuple[str, str]:
    """
    Returns (access_token, user_id).
    svc-core doesn't always return user_id explicitly, so we parse JWT sub.
    """
    body = {"email": email, "password": password}
    cmd = [
        "curl", "-sS",
        "-X", "POST", f"{core_url.rstrip('/')}/api/auth/login",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(body),
    ]
    auth_out = _curl_json(cmd)
    token = auth_out.get("access_token") or auth_out.get("token") or ""
    if not token:
        raise RuntimeError(f"Login failed: missing access_token. Response={auth_out}")

    user_id = auth_out.get("user_id") or auth_out.get("x_user_id") or _jwt_sub(token)
    if not user_id:
        raise RuntimeError(f"Login response missing user_id and JWT sub not parseable: {auth_out}")

    return token, str(user_id)


def _upload_asset(commerce_url: str, token: str, user_id: str, role: str, file_path: str) -> Dict[str, Any]:
    cmd = [
        "curl", "-sS",
        "-X", "POST", f"{commerce_url.rstrip('/')}/api/commerce/assets/upload",
        "-H", f"Authorization: Bearer {token}",
        "-H", f"X-User-Id: {user_id}",
        "-F", f"role={role}",
        "-F", f"file=@{file_path}",
    ]
    return _curl_json(cmd)


def _quote(commerce_url: str, token: str, user_id: str, mode: str, garment_preview_url: str, component_code: str) -> Dict[str, Any]:
    """
    vendor-only flow: platform_models (no model_ref)
    NOTE: customer_tryon likely needs a human/model input; this smoke POC is primarily for platform_models.
    """
    body: Dict[str, Any] = {
        "mode": mode,
        "product_type": "apparel",
        "outputs": {"num_images": 4, "num_videos": 0},
        "views": {"full_body": True, "half_body": False},
        "provider_policy": "auto",
        "product_assets": {
            "items": [
                {
                    "component_code": component_code,
                    "kind": "garment",
                    "image_url": garment_preview_url,
                    "is_primary": True,
                    "dominance_rank": 0,
                    "meta": {"source": "smoke_poc"},
                }
            ],
            "dominant_component_code": component_code,
            # Back-compat fields (some server variants read these)
            "garment_image_url": garment_preview_url,
            "primary_image_url": garment_preview_url,
        },
    }

    cmd = [
        "curl", "-sS",
        "-X", "POST", f"{commerce_url.rstrip('/')}/api/commerce/quote",
        "-H", "Content-Type: application/json",
        "-H", f"Authorization: Bearer {token}",
        "-H", f"X-User-Id: {user_id}",
        "-d", json.dumps(body),
    ]
    return _curl_json(cmd)


def _confirm(commerce_url: str, token: str, user_id: str, quote_id: str) -> Dict[str, Any]:
    body = {"quote_id": quote_id}
    cmd = [
        "curl", "-sS",
        "-X", "POST", f"{commerce_url.rstrip('/')}/api/commerce/confirm",
        "-H", "Content-Type: application/json",
        "-H", f"Authorization: Bearer {token}",
        "-H", f"X-User-Id: {user_id}",
        "-d", json.dumps(body),
    ]
    return _curl_json(cmd)


def _status(commerce_url: str, token: str, user_id: str, studio_job_id: str, include_payload: int = 1) -> Dict[str, Any]:
    url = f"{commerce_url.rstrip('/')}/api/commerce/jobs/{studio_job_id}/status?include_payload={include_payload}"
    cmd = [
        "curl", "-sS",
        "-X", "GET", url,
        "-H", f"Authorization: Bearer {token}",
        "-H", f"X-User-Id: {user_id}",
    ]
    return _curl_json(cmd)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core-url", default=os.environ.get("CORE_URL", "http://localhost:8000"))
    ap.add_argument("--commerce-url", default=os.environ.get("COMMERCE_URL", "http://localhost:8008"))
    ap.add_argument("--email", default=os.environ.get("DF_EMAIL", "user2@desifaces.ai"))
    ap.add_argument("--password", default=os.environ.get("DF_PASSWORD", ""))
    ap.add_argument("--mode", default="platform_models", choices=["platform_models", "customer_tryon"])
    ap.add_argument("--manifest", default="", help="Optional JSON manifest of cases. If omitted, uses built-in cases.")
    ap.add_argument("--max-cases", type=int, default=0, help="0=all, else limit number of cases")
    ap.add_argument("--poll-timeout-s", type=int, default=360, help="Max seconds to poll each job")
    ap.add_argument("--poll-interval-s", type=int, default=5, help="Polling interval in seconds")
    args = ap.parse_args()

    if not args.password:
        raise SystemExit("ERROR: --password (or DF_PASSWORD env var) is required")

    run_dir = f"/tmp/df_vton_non_saree_poc_{_utc_stamp()}"
    _mkdirp(run_dir)
    print("RUN_DIR=", run_dir)

    # Load cases
    if args.manifest:
        m = _read_json(args.manifest)
        cases: List[Dict[str, Any]] = list(m.get("cases") or [])
    else:
        cases = list(DEFAULT_CASES)

    if args.max_cases and args.max_cases > 0:
        cases = cases[: args.max_cases]

    # Login
    token, user_id = _login(args.core_url, args.email, args.password)
    _write_json(os.path.join(run_dir, "auth.json"), {"access_token": token, "user_id": user_id})
    print("Auth OK. X_USER_ID=", user_id)

    summary: Dict[str, Any] = {
        "run_dir": run_dir,
        "core_url": args.core_url,
        "commerce_url": args.commerce_url,
        "mode": args.mode,
        "cases": [],
    }

    for idx, c in enumerate(cases):
        name = str(c.get("name") or f"case_{idx}")
        role = str(c.get("role") or "garment_full")
        component_code = str(c.get("component_code") or "garment")
        urls_any = c.get("url")
        expected = c.get("expected") if isinstance(c.get("expected"), dict) else {}

        print(f"\n=== CASE {idx+1}/{len(cases)}: {name} ({component_code}) ===")

        # Choose extension for local filename (best-effort)
        # If multiple URLs exist, use the first for ext guess.
        first_url = str(_as_list(urls_any)[0]) if _as_list(urls_any) else ""
        ext = _guess_ext_from_url(first_url, fallback=".jpg")
        local_path = os.path.join(run_dir, f"{name}{ext}")

        # Download garment image
        print("[download]", urls_any)
        used_url = _download(urls_any, local_path)
        print("[download] OK from:", used_url, "->", local_path)

        # Upload to commerce
        up = _upload_asset(args.commerce_url, token, user_id, role, local_path)
        _write_json(os.path.join(run_dir, f"{name}_upload.json"), up)
        preview_url = up.get("preview_url") or ""
        if not preview_url:
            raise RuntimeError(f"Upload missing preview_url. resp={up}")
        print("[upload] OK preview_url=", preview_url)

        # Quote
        q = _quote(args.commerce_url, token, user_id, args.mode, preview_url, component_code)
        _write_json(os.path.join(run_dir, f"{name}_quote.json"), q)
        quote_id = q.get("quote_id")
        if not quote_id:
            raise RuntimeError(f"Quote missing quote_id. resp={q}")
        print("[quote] OK quote_id=", quote_id)

        # Confirm
        conf = _confirm(args.commerce_url, token, user_id, quote_id)
        _write_json(os.path.join(run_dir, f"{name}_confirm.json"), conf)
        studio_job_id = conf.get("studio_job_id")
        if not studio_job_id:
            raise RuntimeError(f"Confirm missing studio_job_id. resp={conf}")
        print("[confirm] OK studio_job_id=", studio_job_id)

        # Poll status
        t0 = time.time()
        last: Dict[str, Any] = {}
        while True:
            st = _status(args.commerce_url, token, user_id, studio_job_id, include_payload=1)
            last = st if isinstance(st, dict) else {"raw": st}
            status = str(last.get("status") or last.get("stage") or "").lower()
            print("[poll]", "status=", status)

            if status in ("succeeded", "failed", "aborted"):
                break
            if time.time() - t0 > args.poll_timeout_s:
                raise RuntimeError(f"Timeout polling status > {args.poll_timeout_s}s")
            time.sleep(args.poll_interval_s)

        _write_json(os.path.join(run_dir, f"{name}_status.json"), last)
        status = str(last.get("status") or last.get("stage") or "").lower()

        # Collect output URLs (robust)
        found_urls = _collect_urls(last, limit=80)
        # filter to plausible outputs (not everything)
        found_urls = [u for u in found_urls if u.startswith("http")]

        # Probe first few
        probes: List[Dict[str, Any]] = []
        for u in found_urls[:6]:
            probes.append({"url": u, **_probe_url_head(u)})

        ok_count = sum(1 for p in probes if p.get("ok"))

        if status != "succeeded":
            print("❌ FAILED case:", name)
            print(json.dumps(last, indent=2)[:2000])
        else:
            print("✅ SUCCEEDED:", name)
            if found_urls:
                print("URLs (first 6):")
                for u in found_urls[:6]:
                    print(" -", u)
                print(f"HEAD probes OK: {ok_count}/{len(probes)}")

        summary["cases"].append(
            {
                "name": name,
                "component_code": component_code,
                "status": status,
                "source_url_used": used_url,
                "garment_preview_url": preview_url,
                "expected": expected,
                "outputs_found": len(found_urls),
                "output_urls": found_urls[:12],
                "output_probes": probes,
                "output_probe_ok_count": ok_count,
            }
        )

    _write_json(os.path.join(run_dir, "summary.json"), summary)
    print("\nDONE. summary.json written.")
    print("RUN_DIR=", run_dir)


if __name__ == "__main__":
    main()