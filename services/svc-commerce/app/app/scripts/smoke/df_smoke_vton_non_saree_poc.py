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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

from azure.storage.blob import BlobSasPermissions, generate_blob_sas


# ----------------------------
# small utils
# ----------------------------

def _utc_stamp() -> str:
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
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read()
    if not data or len(data) < 1024:
        raise RuntimeError(f"Downloaded too little data ({len(data) if data else 0} bytes) from {url}")
    with open(dst, "wb") as f:
        f.write(data)


def _download(urls: Any, dst: str, timeout_s: int = 60, retries: int = 5) -> str:
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
    rc, out, err = _run(["curl", "-sS", "-I", "-L", url], capture=True)
    if rc != 0:
        return {"ok": False, "http_code": None, "content_type": None, "err": (err or out)[:500]}

    http_code: Optional[int] = None
    content_type: Optional[str] = None
    for line in out.splitlines():
        s = line.strip()
        if s.lower().startswith("http/"):
            parts = s.split()
            if len(parts) >= 2 and parts[1].isdigit():
                http_code = int(parts[1])
        if s.lower().startswith("content-type:"):
            content_type = s.split(":", 1)[1].strip()
    ok = (http_code is not None) and (200 <= http_code < 400)
    return {"ok": ok, "http_code": http_code, "content_type": content_type}


def _collect_urls(obj: Any, *, limit: int = 50) -> List[str]:
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
    seen = set()
    uniq: List[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _parse_conn_str(conn_str: str) -> Dict[str, str]:
    parts: Dict[str, str] = {}
    for seg in conn_str.split(";"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            parts[k.strip()] = v.strip()
    return parts


def _parse_az_uri(uri: str) -> Tuple[str, str]:
    if not uri.startswith("az://"):
        raise RuntimeError(f"Not an az:// URI: {uri}")
    rest = uri[len("az://") :]
    parts = rest.split("/", 1)
    if len(parts) != 2:
        raise RuntimeError(f"Invalid az:// URI: {uri}")
    return parts[0], parts[1]


def _make_read_sas_url(
    *,
    account_name: str,
    account_key: str,
    container: str,
    blob_name: str,
    ttl_days: int = 7,
) -> str:
    sas = generate_blob_sas(
        account_name=account_name,
        container_name=container,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=dt.datetime.now(dt.UTC) + dt.timedelta(days=ttl_days),
    )
    return f"https://{account_name}.blob.core.windows.net/{container}/{blob_name}?{sas}"


def _resolve_model_url_for_request(url: str, ttl_days: int = 7) -> str:
    """
    Quote API expects http/https for model_ref URLs.
    Convert az://... into a temporary read SAS URL.
    """
    if not url:
        return ""

    s = str(url).strip()
    if s.startswith("http://") or s.startswith("https://"):
        return s

    if not s.startswith("az://"):
        raise RuntimeError(f"Unsupported model URL scheme: {s}")

    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not conn_str:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is required to convert az:// model URLs to SAS")

    conn = _parse_conn_str(conn_str)
    account_name = conn.get("AccountName")
    account_key = conn.get("AccountKey")
    if not account_name or not account_key:
        raise RuntimeError("Could not parse AccountName/AccountKey from AZURE_STORAGE_CONNECTION_STRING")

    container, blob_name = _parse_az_uri(s)
    return _make_read_sas_url(
        account_name=account_name,
        account_key=account_key,
        container=container,
        blob_name=blob_name,
        ttl_days=ttl_days,
    )


# ----------------------------
# Default garment cases
# These are selector-friendly garment kinds.
# ----------------------------

DEFAULT_CASES: List[Dict[str, Any]] = [
    {
        "name": "hoodie_black",
        "garment_kind": "upper_body",
        "outfit_kind": "upper_body",
        "component_code": "hoodie",
        "url": _commons_file("Hoodie m7agar.jpg"),
        "role": "garment_full",
    },
    {
        "name": "blazer_rack",
        "garment_kind": "upper_body",
        "outfit_kind": "upper_body",
        "component_code": "blazer",
        "url": _commons_file("Blazer Jackets on Rack (Unsplash).jpg"),
        "role": "garment_full",
    },
    {
        "name": "jeans_pair",
        "garment_kind": "lower_body",
        "outfit_kind": "lower_body",
        "component_code": "jeans",
        "url": _commons_file("Jeans.jpg"),
        "role": "garment_full",
    },
    {
        "name": "dress",
        "garment_kind": "dresses",
        "outfit_kind": "dresses",
        "component_code": "dress",
        "url": _commons_file("Woman in a dress (Unsplash).jpg"),
        "role": "garment_full",
    },
    {
        "name": "kurta_blue_khadi",
        "garment_kind": "kurta_pyjama",
        "outfit_kind": "kurta_pyjama",
        "component_code": "kurta",
        "url": _commons_file("Blue khadi kurta.jpg"),
        "role": "garment_full",
    },
    {
        "name": "salwar_kameez",
        "garment_kind": "salwar_suit",
        "outfit_kind": "salwar_suit",
        "component_code": "salwar_suit",
        "url": _commons_file("Shalwar kameez Colours.jpg"),
        "role": "garment_full",
    },
    {
        "name": "lehenga_blue",
        "garment_kind": "lehenga_set",
        "outfit_kind": "lehenga_set",
        "component_code": "lehenga",
        "url": _commons_file("Ethnic wear for women Blue Lehenga.png"),
        "role": "garment_full",
    },
    {
        "name": "sherwani",
        "garment_kind": "sherwani",
        "outfit_kind": "sherwani",
        "component_code": "sherwani",
        "url": _commons_file("Sherwani.jpg"),
        "role": "garment_full",
    },
]


def _login(core_url: str, email: str, password: str) -> Tuple[str, str]:
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


def _quote(
    commerce_url: str,
    token: str,
    user_id: str,
    *,
    mode: str,
    garment_preview_url: str,
    component_code: str,
    garment_kind: str,
    outfit_kind: str,
    model_url: str = "",
    preferred_tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    mode=platform_models:
      - if model_url is provided, sends explicit model_ref
      - else relies on vendor-only selector auto-pick
    """
    body: Dict[str, Any] = {
        "mode": mode,
        "provider_kind": mode,
        "product_type": "apparel",
        "outfit_kind": outfit_kind,
        "garment_kind": garment_kind,
        "outputs": {"num_images": 4, "num_videos": 0},
        "views": {"full_body": True, "half_body": False},
        "provider_policy": "auto",
        "product_assets": {
            "items": [
                {
                    "component_code": component_code,
                    "garment_kind": garment_kind,
                    "kind": "garment",
                    "image_url": garment_preview_url,
                    "is_primary": True,
                    "dominance_rank": 0,
                    "meta": {"source": "catalog_smoke", "views": ["full_body"]},
                }
            ],
            "dominant_component_code": component_code,
            "garment_image_url": garment_preview_url,
            "primary_image_url": garment_preview_url,
        },
        "meta": {"source": "catalog_smoke"},
    }

    if preferred_tags:
        body["meta"]["preferred_tags"] = preferred_tags

    if model_url:
        body["model_ref"] = {
            "url": model_url,
            "human_image_url": model_url,
            "meta": {"views": ["full_body"]},
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


def _cases_from_manifest(manifest_path: str) -> List[Dict[str, Any]]:
    m = _read_json(manifest_path)
    if isinstance(m.get("cases"), list):
        return list(m["cases"])
    if isinstance(m.get("items"), list):
        out: List[Dict[str, Any]] = []
        for item in m["items"]:
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "name": item.get("filename") or item.get("outfit_kind") or "case",
                    "garment_kind": item.get("outfit_kind") or item.get("component_code") or "upper_body",
                    "outfit_kind": item.get("outfit_kind") or item.get("component_code") or "upper_body",
                    "component_code": item.get("component_code") or "garment",
                    "url": item.get("image_url"),
                    "role": "garment_full",
                }
            )
        return out
    raise RuntimeError(f"Manifest must contain cases[] or items[]: {manifest_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core-url", default=os.environ.get("CORE_URL", "http://localhost:8000"))
    ap.add_argument("--commerce-url", default=os.environ.get("COMMERCE_URL", "http://localhost:8008"))
    ap.add_argument("--email", default=os.environ.get("DF_EMAIL", "user2@desifaces.ai"))
    ap.add_argument("--password", default=os.environ.get("DF_PASSWORD", ""))
    ap.add_argument("--mode", default="platform_models", choices=["platform_models", "customer_tryon"])
    ap.add_argument("--manifest", default="", help="Optional JSON manifest. Supports {cases:[...]} or {items:[...]}.")

    # one-off local garment test
    ap.add_argument("--garment-file", default="", help="Optional local garment image to test one case.")
    ap.add_argument("--garment-kind", default="", help="Required with --garment-file, e.g. saree_set, salwar_suit, lehenga_set, kurta_pyjama, sherwani, upper_body")
    ap.add_argument("--outfit-kind", default="", help="Optional; defaults to garment-kind")
    ap.add_argument("--component-code", default="", help="Optional display/component code")
    ap.add_argument("--case-name", default="single_case", help="Optional case name for --garment-file")

    # model selector / explicit model
    ap.add_argument("--model-url", default=os.environ.get("MODEL_URL", ""), help="Optional explicit az:// or https human model asset")
    ap.add_argument("--preferred-tags", default="", help="Comma-separated preferred tags for vendor-only autopick, e.g. maharashtra")

    ap.add_argument("--max-cases", type=int, default=0, help="0=all, else limit number of cases")
    ap.add_argument("--poll-timeout-s", type=int, default=360, help="Max seconds to poll each job")
    ap.add_argument("--poll-interval-s", type=int, default=5, help="Polling interval in seconds")
    args = ap.parse_args()

    if not args.password:
        raise SystemExit("ERROR: --password (or DF_PASSWORD env var) is required")

    preferred_tags = [x.strip() for x in args.preferred_tags.split(",") if x.strip()]
    resolved_model_url = _resolve_model_url_for_request(args.model_url) if args.model_url else ""

    run_dir = f"/tmp/df_vton_catalog_poc_{_utc_stamp()}"
    _mkdirp(run_dir)
    print("RUN_DIR=", run_dir)

    # Build cases
    if args.garment_file:
        if not args.garment_kind:
            raise SystemExit("ERROR: --garment-kind is required when using --garment-file")
        cases: List[Dict[str, Any]] = [
            {
                "name": args.case_name,
                "garment_kind": args.garment_kind,
                "outfit_kind": args.outfit_kind or args.garment_kind,
                "component_code": args.component_code or args.garment_kind,
                "local_file": args.garment_file,
                "role": "garment_full",
            }
        ]
    elif args.manifest:
        cases = _cases_from_manifest(args.manifest)
    else:
        cases = list(DEFAULT_CASES)

    if args.max_cases and args.max_cases > 0:
        cases = cases[: args.max_cases]

    token, user_id = _login(args.core_url, args.email, args.password)
    _write_json(os.path.join(run_dir, "auth.json"), {"access_token": token, "user_id": user_id})
    print("Auth OK. X_USER_ID=", user_id)
    if resolved_model_url:
        print("Resolved model URL=", resolved_model_url)

    summary: Dict[str, Any] = {
        "run_dir": run_dir,
        "core_url": args.core_url,
        "commerce_url": args.commerce_url,
        "mode": args.mode,
        "model_url": resolved_model_url or None,
        "preferred_tags": preferred_tags,
        "cases": [],
    }

    for idx, c in enumerate(cases):
        name = str(c.get("name") or f"case_{idx}")
        role = str(c.get("role") or "garment_full")
        component_code = str(c.get("component_code") or c.get("garment_kind") or "garment")
        garment_kind = str(c.get("garment_kind") or component_code)
        outfit_kind = str(c.get("outfit_kind") or garment_kind)

        print(f"\n=== CASE {idx+1}/{len(cases)}: {name} ({garment_kind}) ===")

        if c.get("local_file"):
            local_path = str(c["local_file"])
            if not os.path.exists(local_path):
                raise RuntimeError(f"Local garment file not found: {local_path}")
            used_url = None
            print("[local-file]", local_path)
        else:
            urls_any = c.get("url")
            first_url = str(_as_list(urls_any)[0]) if _as_list(urls_any) else ""
            ext = _guess_ext_from_url(first_url, fallback=".jpg")
            local_path = os.path.join(run_dir, f"{name}{ext}")
            print("[download]", urls_any)
            used_url = _download(urls_any, local_path)
            print("[download] OK from:", used_url, "->", local_path)

        up = _upload_asset(args.commerce_url, token, user_id, role, local_path)
        _write_json(os.path.join(run_dir, f"{name}_upload.json"), up)

        preview_url = (
            up.get("preview_url")
            or up.get("url")
            or up.get("asset_url")
            or up.get("blob_url")
            or ""
        )
        if not preview_url:
            raise RuntimeError(f"Upload missing preview_url/url. resp={up}")
        print("[upload] OK preview_url=", preview_url)

        q = _quote(
            args.commerce_url,
            token,
            user_id,
            mode=args.mode,
            garment_preview_url=preview_url,
            component_code=component_code,
            garment_kind=garment_kind,
            outfit_kind=outfit_kind,
            model_url=resolved_model_url,
            preferred_tags=preferred_tags,
        )
        _write_json(os.path.join(run_dir, f"{name}_quote.json"), q)

        quote_id = q.get("quote_id")
        if not quote_id:
            raise RuntimeError(f"Quote missing quote_id. resp={q}")
        print("[quote] OK quote_id=", quote_id)

        conf = _confirm(args.commerce_url, token, user_id, quote_id)
        _write_json(os.path.join(run_dir, f"{name}_confirm.json"), conf)

        studio_job_id = conf.get("studio_job_id") or conf.get("job_id")
        if not studio_job_id:
            raise RuntimeError(f"Confirm missing studio_job_id/job_id. resp={conf}")
        print("[confirm] OK studio_job_id=", studio_job_id)

        t0 = time.time()
        last: Dict[str, Any] = {}
        while True:
            st = _status(args.commerce_url, token, user_id, studio_job_id, include_payload=1)
            last = st if isinstance(st, dict) else {"raw": st}
            status = str(last.get("status") or last.get("stage") or "").lower()
            print("[poll]", "status=", status)

            if status in ("succeeded", "failed", "aborted", "canceled", "cancelled"):
                break
            if time.time() - t0 > args.poll_timeout_s:
                raise RuntimeError(f"Timeout polling status > {args.poll_timeout_s}s")
            time.sleep(args.poll_interval_s)

        _write_json(os.path.join(run_dir, f"{name}_status.json"), last)
        status = str(last.get("status") or last.get("stage") or "").lower()

        found_urls = _collect_urls(last, limit=80)
        found_urls = [u for u in found_urls if u.startswith("http")]
        probes: List[Dict[str, Any]] = []
        for u in found_urls[:6]:
            probes.append({"url": u, **_probe_url_head(u)})

        ok_count = sum(1 for p in probes if p.get("ok"))

        if status != "succeeded":
            print("❌ FAILED case:", name)
            print(json.dumps(last, indent=2)[:2500])
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
                "garment_kind": garment_kind,
                "outfit_kind": outfit_kind,
                "status": status,
                "source_url_used": used_url,
                "local_file": c.get("local_file"),
                "garment_preview_url": preview_url,
                "model_url": resolved_model_url or None,
                "preferred_tags": preferred_tags,
                "outputs_found": len(found_urls),
                "output_urls": found_urls[:12],
                "output_probes": probes,
                "output_probe_ok_count": ok_count,
                "quote_id": q.get("quote_id"),
                "studio_job_id": studio_job_id,
            }
        )

    _write_json(os.path.join(run_dir, "summary.json"), summary)
    print("\nDONE. summary.json written.")
    print("RUN_DIR=", run_dir)


if __name__ == "__main__":
    main()