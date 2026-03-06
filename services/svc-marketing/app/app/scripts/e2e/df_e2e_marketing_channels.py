#!/usr/bin/env python3
# services/svc-marketing/app/app/scripts/e2e/df_e2e_marketing_channels.py
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


def _now_tag() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def _is_in_docker() -> bool:
    return os.path.exists("/.dockerenv")


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)


def _http_json(method: str, url: str, headers: Dict[str, str], body: Optional[Dict[str, Any]] = None, timeout_s: int = 30) -> Dict[str, Any]:
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = dict(headers)
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            raw = r.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {e.code} {method} {url} body={raw[:800]}") from e
    except Exception as e:
        raise RuntimeError(f"HTTP error {method} {url}: {e}") from e


def _try_get(url: str, timeout_s: int = 3) -> bool:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _discover_url(kind: str, explicit: Optional[str], candidates: List[str], must_have_openapi: bool = False) -> str:
    if explicit:
        return explicit.rstrip("/")

    for base in candidates:
        base = base.rstrip("/")
        if must_have_openapi:
            if _try_get(base + "/openapi.json", timeout_s=3):
                return base
        else:
            if _try_get(base + "/api/health", timeout_s=3) or _try_get(base + "/health", timeout_s=3) or _try_get(base + "/openapi.json", timeout_s=3):
                return base

    raise SystemExit(
        f"Could not discover {kind} URL. Set {kind}_URL env or pass --{kind}-url.\n"
        f"Tried: {candidates}"
    )


def _decode_jwt_sub(token: str) -> Optional[str]:
    # unsafe decode (no verification) just to extract "sub"
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        pad = "=" * (-len(payload) % 4)
        payload_bytes = base64.urlsafe_b64decode((payload + pad).encode("utf-8"))
        obj = json.loads(payload_bytes.decode("utf-8"))
        sub = obj.get("sub")
        return str(sub) if sub else None
    except Exception:
        return None


@dataclass
class Endpoints:
    create_path: str
    status_template: str  # must contain {run_id}


def _discover_marketing_endpoints(marketing_url: str, headers: Dict[str, str]) -> Endpoints:
    """
    Uses /openapi.json to locate:
      - POST create run endpoint (prefer /api/marketing/runs)
      - GET status endpoint (prefer /api/marketing/runs/{run_id}/status)
    Falls back to sensible defaults if not found.
    """
    openapi = _http_json("GET", marketing_url.rstrip("/") + "/openapi.json", headers=headers, timeout_s=30)
    paths = (openapi.get("paths") or {})

    # Preferred paths
    preferred_create = ["/api/marketing/runs", "/api/marketing/run", "/api/marketing/runs/create"]
    preferred_status = ["/api/marketing/runs/{run_id}/status", "/api/marketing/runs/{run_id}"]

    def has_method(p: str, m: str) -> bool:
        v = paths.get(p) or {}
        return m.lower() in v

    for p in preferred_create:
        if p in paths and has_method(p, "post"):
            create_path = p
            break
    else:
        # heuristic: any POST path containing "marketing" and "runs"
        create_path = ""
        for p, v in paths.items():
            if "post" in (v or {}) and ("marketing" in p) and ("runs" in p):
                create_path = p
                break
        if not create_path:
            create_path = "/api/marketing/runs"

    for p in preferred_status:
        if p in paths and has_method(p, "get"):
            status_path = p
            break
    else:
        status_path = ""
        for p, v in paths.items():
            if "get" in (v or {}) and ("marketing" in p) and ("runs" in p) and ("{run_id}" in p) and ("status" in p):
                status_path = p
                break
        if not status_path:
            # last resort
            status_path = "/api/marketing/runs/{run_id}/status"

    return Endpoints(create_path=create_path, status_template=status_path)


def _login(core_url: str, email: str, password: str) -> Tuple[str, str]:
    resp = _http_json(
        "POST",
        core_url.rstrip("/") + "/api/auth/login",
        headers={},
        body={"email": email, "password": password},
        timeout_s=30,
    )
    token = resp.get("access_token") or resp.get("token") or resp.get("bearer_token") or ""
    if not token:
        raise SystemExit(f"Login succeeded but no token in response keys={list(resp.keys())}")

    user_id = resp.get("user_id") or resp.get("x_user_id") or (resp.get("user") or {}).get("user_id")
    if not user_id:
        user_id = _decode_jwt_sub(token)

    if not user_id:
        raise SystemExit(f"Could not determine user_id from login response. keys={list(resp.keys())}")

    return str(token), str(user_id)


def _mk_auth_headers(token: str, user_id: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-User-Id": user_id,
    }


def _channel_specs() -> List[Dict[str, Any]]:
    """
    Generates one run per "channel output flavor".
    Publishing is controlled by --mode stage/publish.
    """
    return [
        {
            "name": "instagram_reel",
            "format_hint": "reel",
            "publish_targets": ["instagram_reel"],
        },
        {
            "name": "youtube_short",
            "format_hint": "yt_short",
            "publish_targets": ["youtube_short"],
        },
        {
            "name": "youtube_long",
            "format_hint": "yt_long",
            "publish_targets": ["youtube_long"],
        },
        # these generate assets; auto-publish may not be implemented for them yet
        {
            "name": "instagram_story_asset",
            "format_hint": "story",
            "publish_targets": [],
        },
        {
            "name": "instagram_carousel_asset",
            "format_hint": "carousel",
            "publish_targets": [],
        },
        # crosspost reel (one asset intended for IG+YT short)
        {
            "name": "crosspost_reel_ig_ytshort",
            "format_hint": "reel",
            "publish_targets": ["instagram_reel", "youtube_short"],
        },
    ]


def _build_run_payload(
    *,
    mode: str,
    recipe: str,
    industry: str,
    language_hint: str,
    channel: Dict[str, Any],
    persona: Optional[str] = None,
    season_event: Optional[str] = None,
    offer: Optional[str] = None,
    target_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "mode": mode,
        "recipe": recipe,
        "industry": industry,
        "language_hint": language_hint,
        "inputs": {
            "industry": industry,
            "format_hint": channel["format_hint"],
            "language_hint": language_hint,
            "publish_targets": channel.get("publish_targets") or [],
        },
    }
    if persona:
        payload["persona"] = persona
        payload["inputs"]["persona"] = persona
    if season_event:
        payload["season_event"] = season_event
        payload["inputs"]["season_event"] = season_event
    if offer:
        payload["offer"] = offer
        payload["inputs"]["offer"] = offer
    if target_seconds is not None:
        payload["target_seconds"] = int(target_seconds)
        payload["inputs"]["target_seconds"] = int(target_seconds)
    return payload


def _poll_run(
    marketing_url: str,
    endpoints: Endpoints,
    run_id: str,
    headers: Dict[str, str],
    timeout_s: int,
    interval_s: float,
) -> Dict[str, Any]:
    t0 = time.time()
    last: Dict[str, Any] = {}
    url = marketing_url.rstrip("/") + endpoints.status_template.format(run_id=run_id)

    while True:
        last = _http_json("GET", url, headers=headers, timeout_s=30)
        status = str(last.get("status") or "").lower()
        stage = str(last.get("stage") or "")

        if status in ("succeeded", "failed"):
            return last

        if (time.time() - t0) > float(timeout_s):
            return {"status": "timeout", "stage": stage, "status_url": url, "last": last}

        time.sleep(float(interval_s))


def _pick_output_urls(status_payload: Dict[str, Any]) -> Dict[str, Any]:
    out = status_payload.get("output") if isinstance(status_payload.get("output"), dict) else {}
    return {
        "reel_url": out.get("reel_url"),
        "reel_cover_url": out.get("reel_cover_url"),
        "caption_url": out.get("caption_url"),
        "manifest_url": out.get("manifest_url"),
        "story_url": out.get("story_url"),
        "slide_01_url": out.get("slide_01_url"),
        "slide_02_url": out.get("slide_02_url"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default=os.getenv("DF_EMAIL") or os.getenv("ADMIN_MARKETING_EMAIL") or "", help="Login email (svc-core)")
    ap.add_argument("--password", default=os.getenv("DF_PASSWORD") or os.getenv("ADMIN_MARKETING_PASSWORD") or "", help="Login password (svc-core)")
    ap.add_argument("--core-url", default=os.getenv("CORE_URL") or os.getenv("SVC_CORE_URL") or os.getenv("SVC_CORE_URL_MARKETING") or "", help="svc-core base URL")
    ap.add_argument("--marketing-url", default=os.getenv("MARKETING_URL") or os.getenv("SVC_MARKETING_URL") or os.getenv("SVC_MARKETING_URL_MARKETING") or "", help="svc-marketing base URL")
    ap.add_argument("--mode", choices=["stage", "publish"], default=os.getenv("MARKETING_MODE") or "stage", help="stage=generate only, publish=auto publish")
    ap.add_argument("--recipe", default=os.getenv("MARKETING_RECIPE") or "FACE_AUDIO_VIDEO", help="RecipeKind value (e.g., FACE_AUDIO_VIDEO)")
    ap.add_argument("--industry", default=os.getenv("MARKETING_INDUSTRY") or "creator_tools", help="Industry hint for planner")
    ap.add_argument("--language", default=os.getenv("MARKETING_LANGUAGE") or "en", help="Language hint")
    ap.add_argument("--persona", default=os.getenv("MARKETING_PERSONA") or "", help="Optional Persona enum value")
    ap.add_argument("--season-event", default=os.getenv("MARKETING_SEASON_EVENT") or "", help="Optional season_event")
    ap.add_argument("--offer", default=os.getenv("MARKETING_OFFER") or "", help="Optional offer")
    ap.add_argument("--target-seconds", type=int, default=int(os.getenv("MARKETING_TARGET_SECONDS") or "10"), help="6..15 recommended")
    ap.add_argument("--timeout", type=int, default=int(os.getenv("MARKETING_E2E_TIMEOUT_S") or "1800"), help="Poll timeout seconds")
    ap.add_argument("--interval", type=float, default=float(os.getenv("MARKETING_E2E_POLL_INTERVAL_S") or "3"), help="Poll interval seconds")
    ap.add_argument("--only", default=os.getenv("MARKETING_ONLY") or "", help="Comma-separated subset of channel names")
    args = ap.parse_args()

    if not args.email or not args.password:
        print("ERROR: provide --email and --password (or set DF_EMAIL/DF_PASSWORD).", file=sys.stderr)
        return 2

    # discover URLs if not provided
    if _is_in_docker():
        core_candidates = ["http://svc-core:8000", "http://df-svc-core:8000"]
        marketing_candidates = ["http://svc-marketing:8000", "http://df-svc-marketing:8000"]
    else:
        core_candidates = ["http://localhost:8000"]
        marketing_candidates = [
            "http://localhost:8010",
            "http://localhost:8011",
            "http://localhost:8012",
            "http://localhost:8009",
            "http://localhost:8008",
        ]

    core_url = _discover_url("CORE", args.core_url or "", core_candidates, must_have_openapi=False)
    marketing_url = _discover_url("MARKETING", args.marketing_url or "", marketing_candidates, must_have_openapi=True)

    token, user_id = _login(core_url, args.email, args.password)
    headers = _mk_auth_headers(token, user_id)

    endpoints = _discover_marketing_endpoints(marketing_url, headers=headers)
    create_url = marketing_url.rstrip("/") + endpoints.create_path
    print(f"[e2e] core_url={core_url}")
    print(f"[e2e] marketing_url={marketing_url}")
    print(f"[e2e] create_url={create_url}")
    print(f"[e2e] status_template={endpoints.status_template}")
    print(f"[e2e] user_id={user_id}")
    print(f"[e2e] mode={args.mode} recipe={args.recipe}")

    allow = set([c["name"] for c in _channel_specs()])
    only = [s.strip() for s in (args.only or "").split(",") if s.strip()]
    if only:
        for o in only:
            if o not in allow:
                raise SystemExit(f"Unknown channel '{o}'. Allowed: {sorted(allow)}")
        channels = [c for c in _channel_specs() if c["name"] in set(only)]
    else:
        channels = _channel_specs()

    run_dir = f"/tmp/df_e2e_marketing_channels_{_now_tag()}"
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "config.json"), "w", encoding="utf-8") as f:
        f.write(_stable_json({
            "core_url": core_url,
            "marketing_url": marketing_url,
            "create_path": endpoints.create_path,
            "status_template": endpoints.status_template,
            "user_id": user_id,
            "mode": args.mode,
            "recipe": args.recipe,
            "industry": args.industry,
            "language": args.language,
            "channels": [c["name"] for c in channels],
        }))

    summary: Dict[str, Any] = {"run_dir": run_dir, "runs": []}

    for ch in channels:
        payload = _build_run_payload(
            mode=args.mode,
            recipe=args.recipe,
            industry=args.industry,
            language_hint=args.language,
            channel=ch,
            persona=args.persona or None,
            season_event=args.season_event or None,
            offer=args.offer or None,
            target_seconds=args.target_seconds,
        )

        print(f"\n[e2e] creating run for channel={ch['name']} format={ch['format_hint']} targets={ch.get('publish_targets')}")
        with open(os.path.join(run_dir, f"{ch['name']}_create_payload.json"), "w", encoding="utf-8") as f:
            f.write(_stable_json(payload))

        created = _http_json("POST", create_url, headers=headers, body=payload, timeout_s=60)
        run_id = created.get("run_id") or created.get("id")
        if not run_id:
            raise SystemExit(f"Create response missing run_id. keys={list(created.keys())}")

        with open(os.path.join(run_dir, f"{ch['name']}_create_response.json"), "w", encoding="utf-8") as f:
            f.write(_stable_json(created))

        print(f"[e2e] run_id={run_id} polling...")
        status = _poll_run(marketing_url, endpoints, str(run_id), headers, timeout_s=args.timeout, interval_s=args.interval)

        with open(os.path.join(run_dir, f"{ch['name']}_status.json"), "w", encoding="utf-8") as f:
            f.write(_stable_json(status))

        st = str(status.get("status") or "").lower()
        stage = str(status.get("stage") or "")
        err = (status.get("error_code"), status.get("error_message"))
        urls = _pick_output_urls(status)

        print(f"[e2e] channel={ch['name']} status={st} stage={stage}")
        if st == "failed":
            print(f"[e2e]   error_code={err[0]} error_message={(err[1] or '')[:200]}")
        print(f"[e2e]   outputs: {urls}")

        summary["runs"].append({
            "channel": ch["name"],
            "run_id": str(run_id),
            "status": st,
            "stage": stage,
            "error_code": status.get("error_code"),
            "error_message": status.get("error_message"),
            "outputs": urls,
        })

    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as f:
        f.write(_stable_json(summary))

    print(f"\n[e2e] DONE. artifacts in {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())