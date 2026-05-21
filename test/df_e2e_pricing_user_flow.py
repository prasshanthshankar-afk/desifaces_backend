#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

ALLOWED_TERMINAL_PRICING_STATES = {
    "committed",
    "released",
    "reservation_failed",
    "commit_failed",
    "release_failed",
    "failed",
    "canceled",
}
ALLOWED_TERMINAL_JOB_STATUSES = {"succeeded", "failed", "canceled"}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}

def as_list(x: Any) -> List[Any]:
    return x if isinstance(x, list) else []

def deep_find_first_artifact_id(payload: Any) -> Optional[str]:
    stack = [payload]
    seen = set()
    candidate_keys = ("artifact_id", "id")
    while stack:
        cur = stack.pop()
        oid = id(cur)
        if oid in seen:
            continue
        seen.add(oid)
        if isinstance(cur, dict):
            # strong signals
            for key in ("artifact_id", "audio_artifact_id", "face_artifact_id"):
                value = cur.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            # generic artifact dict
            kind = str(cur.get("kind") or cur.get("asset_kind") or "").lower()
            if "artifact" in cur or kind in {"audio", "voice", "face", "image"}:
                for key in candidate_keys:
                    value = cur.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None

def deep_find_artifact_ids(payload: Any) -> List[str]:
    out: List[str] = []
    stack = [payload]
    seen = set()
    while stack:
        cur = stack.pop()
        oid = id(cur)
        if oid in seen:
            continue
        seen.add(oid)
        if isinstance(cur, dict):
            for key in ("artifact_id", "audio_artifact_id", "face_artifact_id"):
                value = cur.get(key)
                if isinstance(value, str) and value.strip():
                    out.append(value.strip())
            kind = str(cur.get("kind") or cur.get("asset_kind") or "").lower()
            if kind in {"audio", "voice", "face", "image"}:
                for key in ("artifact_id", "id"):
                    value = cur.get(key)
                    if isinstance(value, str) and value.strip():
                        out.append(value.strip())
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    # stable dedupe
    seen_ids = set()
    deduped = []
    for x in out:
        if x not in seen_ids:
            seen_ids.add(x)
            deduped.append(x)
    return deduped

def maybe_job_id(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("job_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    job = as_dict(payload.get("job"))
    for key in ("job_id", "id"):
        value = job.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None

def get_pricing(payload: Dict[str, Any]) -> Dict[str, Any]:
    pricing = as_dict(payload.get("pricing"))
    if pricing:
        return pricing
    for container_key in ("job", "data", "result"):
        container = as_dict(payload.get(container_key))
        pricing = as_dict(container.get("pricing"))
        if pricing:
            return pricing
    return {}

def get_pricing_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    summary = as_dict(payload.get("pricing_summary"))
    if summary:
        return summary
    for container_key in ("job", "data", "result"):
        container = as_dict(payload.get(container_key))
        summary = as_dict(container.get("pricing_summary"))
        if summary:
            return summary
    return {}

def pricing_brief(payload: Dict[str, Any]) -> Dict[str, Any]:
    pricing = get_pricing(payload)
    summary = get_pricing_summary(payload)
    return {
        "state": pricing.get("state"),
        "enabled": pricing.get("enabled"),
        "billing_mode": pricing.get("billing_mode"),
        "settlement_mode": pricing.get("settlement_mode"),
        "tier_code": pricing.get("tier_code"),
        "quote_id": pricing.get("quote_id"),
        "reservation_id": pricing.get("reservation_id"),
        "variant_code": pricing.get("variant_code"),
        "sku_code": pricing.get("sku_code"),
        "estimated_units": pricing.get("estimated_units"),
        "actual_units": pricing.get("actual_units"),
        "billed_units": pricing.get("billed_units"),
        "final_amount": pricing.get("final_amount"),
        "currency": pricing.get("currency"),
        "ledger_entry_id": pricing.get("ledger_entry_id"),
        "source": pricing.get("source"),
        "reason": pricing.get("reason"),
        "summary": summary,
    }

def infer_job_status(payload: Dict[str, Any]) -> str:
    if isinstance(payload.get("status"), str):
        return str(payload["status"])
    job = as_dict(payload.get("job"))
    if isinstance(job.get("status"), str):
        return str(job["status"])
    return ""

class HttpClient:
    def __init__(self, headers: Optional[Dict[str, str]] = None):
        self.headers = headers or {}

    def request_json(self, method: str, url: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = None
        headers = {"accept": "application/json", **self.headers}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["content-type"] = "application/json"
        req = Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urlopen(req) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except HTTPError as e:
            try:
                raw = e.read().decode("utf-8", errors="replace")
                detail = json.loads(raw) if raw.strip() else {}
            except Exception:
                detail = {"raw": raw}
            raise RuntimeError(f"{method.upper()} {url} failed [{e.code}]: {json.dumps(detail)}") from e

@dataclass(frozen=True)
class ServiceConfig:
    name: str
    base_url: str
    preview_path: str
    generate_path: str
    status_template: str
    timeout_seconds: int = 900
    poll_seconds: int = 3

def path_join(base: str, path: str) -> str:
    return base.rstrip("/") + path

def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

def login(core_url: str, email: str, password: str) -> Dict[str, str]:
    client = HttpClient()
    payload = {"email": email, "password": password}
    auth = client.request_json("POST", path_join(core_url, "/api/auth/login"), payload)
    token = str(auth.get("access_token") or "").strip()
    user_id = str(auth.get("user_id") or auth.get("sub") or "").strip()
    if not token:
        raise RuntimeError(f"Login failed for {email}: {json.dumps(auth)}")
    return {
        "access_token": token,
        "x_user_id": user_id,
        "auth_payload": auth,
    }

def check_feature_allowed(preview_resp: Dict[str, Any]) -> Tuple[bool, str]:
    pricing = get_pricing(preview_resp)
    reason = str(pricing.get("reason") or "")
    state = str(pricing.get("state") or "")
    if reason and any(x in reason.lower() for x in ["blocked", "insufficient", "denied", "unknown", "inactive"]):
        return False, reason
    if state in {"reservation_failed", "commit_failed", "release_failed", "failed", "canceled"}:
        return False, state
    # if preview returns pricing, we consider the service at least preview-visible
    return True, reason or state or "preview_ok"

def build_face_payload(confirmed: bool) -> Dict[str, Any]:
    return {
        "mode": "text-to-image",
        "num_variants": 1,
        "gender": "female",
        "age_group": "adult",
        "style_preset": "studio_portrait",
        "country_code": "US",
        "channel": "web",
        "pricing_confirmation": {"confirmed": confirmed},
    }

def build_audio_payload(confirmed: bool, voice_id: str, locale: str) -> Dict[str, Any]:
    return {
        "text": "Welcome to DesiFaces. This is a pricing contract check for audio generation.",
        "voice_id": voice_id,
        "locale": locale,
        "channel": "web",
        "country_code": "US",
        "pricing_confirmation": {"confirmed": confirmed},
    }

def build_fusion_payload(confirmed: bool, face_artifact_id: str, audio_artifact_id: str) -> Dict[str, Any]:
    return {
        "face_artifact_id": face_artifact_id,
        "audio_artifact_id": audio_artifact_id,
        "external_provider_ok": True,
        "video": {"duration_sec": 10},
        "channel": "web",
        "country_code": "US",
        "pricing_confirmation": {"confirmed": confirmed},
    }

def request_preview_adaptive(client: HttpClient, service_name: str, preview_url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    candidates = [payload]
    # Most likely wrapper in this codebase
    candidates.append({"studio_input": payload})
    # Generic fallback sometimes used in preview endpoints
    candidates.append({"request": payload})

    last_error: Optional[Exception] = None
    seen = set()
    for cand in candidates:
        key = json.dumps(cand, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        try:
            return client.request_json("POST", preview_url, cand)
        except Exception as e:
            msg = str(e)
            last_error = e
            # If the endpoint explicitly says studio_input is missing, retry with that wrapper.
            if "studio_input" in msg or "Field required" in msg or "422" in msg:
                continue
            raise
    if last_error:
        raise last_error
    raise RuntimeError(f"Preview failed for {service_name}: no payload candidates attempted")

def poll_status(client: HttpClient, status_url: str, out_path: Path, timeout_seconds: int, poll_seconds: int) -> Dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        last = client.request_json("GET", status_url)
        write_json(out_path, last)
        pricing_state = str(get_pricing(last).get("state") or "")
        job_status = infer_job_status(last)
        if pricing_state in ALLOWED_TERMINAL_PRICING_STATES or job_status in ALLOWED_TERMINAL_JOB_STATUSES:
            return last
        time.sleep(poll_seconds)
    return last

def run_service(
    client: HttpClient,
    config: ServiceConfig,
    out_dir: Path,
    preview_payload: Dict[str, Any],
    generate_payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    svc_dir = out_dir / config.name
    svc_dir.mkdir(parents=True, exist_ok=True)

    preview_url = path_join(config.base_url, config.preview_path)
    preview_resp = request_preview_adaptive(client, config.name, preview_url, preview_payload)
    write_json(svc_dir / "preview.json", preview_resp)

    allowed, preview_reason = check_feature_allowed(preview_resp)
    result: Dict[str, Any] = {
        "service": config.name,
        "preview_ok": True,
        "allowed_by_preview": allowed,
        "preview_reason": preview_reason,
        "preview_pricing": pricing_brief(preview_resp),
        "generated": False,
        "job_id": None,
        "job_status": None,
        "final_pricing": None,
        "artifact_ids": [],
        "blocked_or_failed_reason": None,
    }

    if not allowed or generate_payload is None:
        result["blocked_or_failed_reason"] = preview_reason if not allowed else "generation_skipped"
        write_json(svc_dir / "result.json", result)
        return result

    try:
        generate_url = path_join(config.base_url, config.generate_path)
        generate_resp = client.request_json("POST", generate_url, generate_payload)
        write_json(svc_dir / "generate.json", generate_resp)
        job_id = maybe_job_id(generate_resp)
        result["job_id"] = job_id
        result["generate_pricing"] = pricing_brief(generate_resp)
        if not job_id:
            result["blocked_or_failed_reason"] = "missing_job_id"
            write_json(svc_dir / "result.json", result)
            return result

        status_url = path_join(config.base_url, config.status_template.format(job_id=job_id))
        final_status = poll_status(
            client,
            status_url,
            svc_dir / "status_last.json",
            timeout_seconds=config.timeout_seconds,
            poll_seconds=config.poll_seconds,
        )
        result["generated"] = True
        result["job_status"] = infer_job_status(final_status)
        result["final_pricing"] = pricing_brief(final_status)
        result["artifact_ids"] = deep_find_artifact_ids(final_status)
        pricing_reason = str(get_pricing(final_status).get("reason") or "")
        if result["job_status"] in {"failed", "canceled"} or (result["final_pricing"] or {}).get("state") in {"released", "reservation_failed", "commit_failed", "release_failed", "failed", "canceled"}:
            result["blocked_or_failed_reason"] = pricing_reason or result["job_status"] or (result["final_pricing"] or {}).get("state")
        write_json(svc_dir / "result.json", result)
        return result
    except Exception as e:
        result["blocked_or_failed_reason"] = str(e)
        write_json(svc_dir / "result.json", result)
        return result

def summarize_user(out_dir: Path, email: str, login_payload: Dict[str, Any], results: List[Dict[str, Any]]) -> Dict[str, Any]:
    tier_codes = [r.get("preview_pricing", {}).get("tier_code") for r in results if r.get("preview_pricing", {}).get("tier_code")]
    tier_code = tier_codes[0] if tier_codes else None
    billing_modes = sorted({str(r.get("preview_pricing", {}).get("billing_mode") or "") for r in results if r.get("preview_pricing")})
    settlement_modes = sorted({str(r.get("preview_pricing", {}).get("settlement_mode") or "") for r in results if r.get("preview_pricing")})
    entitlements = {
        r["service"]: {
            "preview_allowed": r.get("allowed_by_preview"),
            "reason": r.get("preview_reason"),
            "tier_code": r.get("preview_pricing", {}).get("tier_code"),
            "billing_mode": r.get("preview_pricing", {}).get("billing_mode"),
            "settlement_mode": r.get("preview_pricing", {}).get("settlement_mode"),
        }
        for r in results
    }
    summary = {
        "generated_at": now_iso(),
        "email": email,
        "user_id": login_payload.get("x_user_id") or "",
        "resolved_tier_code": tier_code,
        "billing_modes_seen": billing_modes,
        "settlement_modes_seen": settlement_modes,
        "entitlements": entitlements,
        "results": results,
    }
    write_json(out_dir / "summary.json", summary)
    return summary

def main() -> int:
    ap = argparse.ArgumentParser(description="DesiFaces pricing E2E runner: login once, preview entitlements, generate face/audio/fusion, record blocks.")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--core-url", default=os.getenv("CORE_URL", "http://localhost:8000"))
    ap.add_argument("--face-url", default=os.getenv("FACE_URL", "http://localhost:8003"))
    ap.add_argument("--audio-url", default=os.getenv("AUDIO_URL", "http://localhost:8007"))
    ap.add_argument("--fusion-url", default=os.getenv("FUSION_URL", "http://localhost:8002"))
    ap.add_argument("--audio-voice-id", default=os.getenv("AUDIO_VOICE_ID", "default"))
    ap.add_argument("--audio-locale", default=os.getenv("AUDIO_LOCALE", "en-IN"))
    ap.add_argument("--skip-face", action="store_true")
    ap.add_argument("--skip-audio", action="store_true")
    ap.add_argument("--skip-fusion", action="store_true")
    ap.add_argument("--out-dir", default="")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir or f"/tmp/df_e2e_pricing_{args.email.replace('@','_at_').replace('.','_')}_{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    login_payload = login(args.core_url.rstrip("/"), args.email, args.password)
    write_json(out_dir / "auth.json", as_dict(login_payload.get("auth_payload")))
    headers = {"authorization": f"Bearer {login_payload['access_token']}"}
    if login_payload.get("x_user_id"):
        headers["x-user-id"] = login_payload["x_user_id"]
    client = HttpClient(headers=headers)

    face_cfg = ServiceConfig(
        name="face",
        base_url=args.face_url.rstrip("/"),
        preview_path="/api/face/creator/pricing/preview",
        generate_path="/api/face/creator/generate",
        status_template="/api/face/creator/jobs/{job_id}/status",
    )
    audio_cfg = ServiceConfig(
        name="audio",
        base_url=args.audio_url.rstrip("/"),
        preview_path=os.getenv("AUDIO_PREVIEW_PATH", "/api/audio/tts/pricing/preview"),
        generate_path=os.getenv("AUDIO_GENERATE_PATH", "/api/audio/tts/generate"),
        status_template=os.getenv("AUDIO_STATUS_PATH_TEMPLATE", "/api/audio/jobs/{job_id}/status"),
    )
    fusion_cfg = ServiceConfig(
        name="fusion",
        base_url=args.fusion_url.rstrip("/"),
        preview_path=os.getenv("FUSION_PREVIEW_PATH", "/api/fusion/jobs/pricing/preview"),
        generate_path=os.getenv("FUSION_GENERATE_PATH", "/api/fusion/jobs"),
        status_template=os.getenv("FUSION_STATUS_PATH_TEMPLATE", "/api/fusion/jobs/{job_id}/status"),
        timeout_seconds=1200,
        poll_seconds=5,
    )

    results: List[Dict[str, Any]] = []

    face_result = None
    if not args.skip_face:
        face_result = run_service(
            client, face_cfg, out_dir,
            preview_payload=build_face_payload(False),
            generate_payload=build_face_payload(True),
        )
        results.append(face_result)

    audio_result = None
    if not args.skip_audio:
        audio_result = run_service(
            client, audio_cfg, out_dir,
            preview_payload=build_audio_payload(False, args.audio_voice_id, args.audio_locale),
            generate_payload=build_audio_payload(True, args.audio_voice_id, args.audio_locale),
        )
        results.append(audio_result)

    if not args.skip_fusion:
        face_artifact_id = None
        audio_artifact_id = None
        if face_result:
            ids = face_result.get("artifact_ids") or []
            face_artifact_id = ids[0] if ids else None
        if audio_result:
            ids = audio_result.get("artifact_ids") or []
            audio_artifact_id = ids[0] if ids else None

        fusion_generate_payload = None
        if face_artifact_id and audio_artifact_id:
            fusion_generate_payload = build_fusion_payload(True, face_artifact_id, audio_artifact_id)
            fusion_preview_payload = build_fusion_payload(False, face_artifact_id, audio_artifact_id)
        else:
            # still do preview-only if artifacts are missing, and record why
            fusion_preview_payload = build_fusion_payload(False, face_artifact_id or "MISSING_FACE_ARTIFACT_ID", audio_artifact_id or "MISSING_AUDIO_ARTIFACT_ID")

        fusion_result = run_service(
            client, fusion_cfg, out_dir,
            preview_payload=fusion_preview_payload,
            generate_payload=fusion_generate_payload,
        )
        if fusion_generate_payload is None:
            fusion_result["blocked_or_failed_reason"] = (
                fusion_result.get("blocked_or_failed_reason")
                or "fusion_generation_skipped_missing_face_or_audio_artifact"
            )
            write_json(out_dir / "fusion" / "result.json", fusion_result)
        results.append(fusion_result)

    summary = summarize_user(out_dir, args.email, login_payload, results)
    print(json.dumps(summary, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
