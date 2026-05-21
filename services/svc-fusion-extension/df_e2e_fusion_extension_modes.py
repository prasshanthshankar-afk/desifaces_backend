#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import math
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

HEALTH_PATHS = ["/api/health", "/health"]
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "canceled", "cancelled", "completed", "success", "done"}
SUCCESS_JOB_STATUSES = {"succeeded", "completed", "success", "done"}
TERMINAL_PRICING_STATES = {
    "committed",
    "released",
    "reservation_failed",
    "commit_failed",
    "release_failed",
    "failed",
    "canceled",
    "cancelled",
}


def print_step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def ensure_dir(path: str) -> str:
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: str, payload: Any) -> None:
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)


def normalize_bearer(token_or_header: str) -> str:
    raw = (token_or_header or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("bearer "):
        return raw
    return f"Bearer {raw}"


def http_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Any] = None,
    timeout: int = 60,
    accepted_statuses: Iterable[int] = (200, 201, 202),
) -> Tuple[int, Dict[str, str], Any]:
    body = None
    merged_headers = {"Accept": "application/json"}
    if headers:
        merged_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        merged_headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=body, headers=merged_headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = int(getattr(resp, "status", 200))
            resp_headers = dict(resp.headers.items())
    except urllib.error.HTTPError as ex:
        raw = ex.read().decode("utf-8", errors="replace")
        status = int(ex.code)
        resp_headers = dict(ex.headers.items())
        if status not in {int(x) for x in accepted_statuses}:
            raise RuntimeError(f"{method.upper()} {url} failed [{status}]: {raw}") from ex
    except Exception as ex:
        raise RuntimeError(f"{method.upper()} {url} failed: {ex}") from ex

    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except Exception:
        parsed = {"raw": raw}
    return status, resp_headers, parsed


def service_healthy(base_url: str) -> bool:
    for path in HEALTH_PATHS:
        try:
            status, _, _ = http_json("GET", f"{base_url}{path}", timeout=8, accepted_statuses=(200,))
            if status == 200:
                return True
        except Exception:
            continue
    return False


def wait_for_service_health(name: str, base_url: str, timeout_seconds: int) -> None:
    print_step(f"Waiting for health: {name} {base_url}")
    deadline = time.time() + float(timeout_seconds)
    while time.time() < deadline:
        if service_healthy(base_url):
            return
        time.sleep(3)
    raise RuntimeError(f"Timed out waiting for health at {base_url}")


def first_present(values: Iterable[Any]) -> Optional[str]:
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def recursive_collect(obj: Any, keys: set[str]) -> List[Any]:
    out: List[Any] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k) in keys:
                out.append(v)
            out.extend(recursive_collect(v, keys))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(recursive_collect(item, keys))
    return out


def recursive_collect_first_str(obj: Any, keys: set[str]) -> Optional[str]:
    for value in recursive_collect(obj, keys):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def recursive_collect_first_dict(obj: Any, keys: set[str]) -> Optional[Dict[str, Any]]:
    for value in recursive_collect(obj, keys):
        if isinstance(value, dict):
            return value
    return None


def jwt_sub(token_or_header: str) -> Optional[str]:
    raw = (token_or_header or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
        parsed = json.loads(decoded.decode("utf-8"))
    except Exception:
        return None
    sub = parsed.get("sub") or parsed.get("user_id")
    return str(sub).strip() if sub else None


def common_headers(access_token: str, user_id: str) -> Dict[str, str]:
    return {
        "Authorization": normalize_bearer(access_token),
        "X-User-Id": str(user_id),
    }

def _looks_like_http_asset_url(value: str) -> bool:
    s = (value or "").strip().lower()
    if not s.startswith(("http://", "https://")):
        return False
    return any(token in s for token in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", "/audio-output/", "/artifacts/", "/assets/"))


def _looks_like_service_base_url(value: str) -> bool:
    s = (value or "").strip().lower()
    if not s.startswith(("http://", "https://")):
        return False
    if _looks_like_http_asset_url(s):
        return False
    return True


def normalize_audio_args(args: argparse.Namespace) -> argparse.Namespace:
    raw_audio_url = str(getattr(args, "audio_url", "") or "").strip()
    explicit_service = str(getattr(args, "audio_service_url", "") or "").strip()
    explicit_input_url = str(getattr(args, "existing_audio_url", "") or "").strip()

    if not explicit_input_url and _looks_like_http_asset_url(raw_audio_url):
        args.existing_audio_url = raw_audio_url

    if explicit_service:
        args.audio_service_url = explicit_service.rstrip("/")
    elif _looks_like_service_base_url(raw_audio_url):
        args.audio_service_url = raw_audio_url.rstrip("/")
    else:
        args.audio_service_url = "http://localhost:8004"

    if getattr(args, "existing_audio_url", None):
        args.existing_audio_url = str(args.existing_audio_url).strip()

    if getattr(args, "existing_audio_artifact_id", None):
        args.existing_audio_artifact_id = str(args.existing_audio_artifact_id).strip()

    return args



def lower_ascii(text: str) -> str:
    return (text or "").lower().strip()

def _scenario_field(scenario: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = scenario.get(key)
        if value is None:
            continue
        s = str(value).strip()
        if s:
            return s
    return None


def _normalize_quality_tier(value: Any) -> str:
    s = lower_ascii(str(value or ""))
    if s in {"economy", "eco", "fast", "budget", "veed", "veed_fabric"}:
        return "economy"
    return "premium"


def _talking_video_segment_count(duration_sec: int, bucket_max_sec: int = 30) -> int:
    sec = max(1, int(duration_sec or 0))
    limit = max(10, int(bucket_max_sec or 30))
    return max(1, int(math.ceil(float(sec) / float(limit))))


def _economy_bucket_expectations(duration_sec: int) -> Tuple[str, str]:
    sec = max(1, int(duration_sec or 0))
    if sec <= 10:
        return "TALKING_VIDEO_ECONOMY_10S", "LONGFORM_TALK_ECONOMY_10S"
    if sec <= 20:
        return "TALKING_VIDEO_ECONOMY_20S", "LONGFORM_TALK_ECONOMY_20S"
    # 30s is the per-segment/provider bucket. Longer runs must segment and stitch,
    # not hard-fail on the client.
    return "TALKING_VIDEO_ECONOMY_30S", "LONGFORM_TALK_ECONOMY_30S"


def _premium_bucket_expectations(duration_sec: int) -> Tuple[str, str]:
    sec = max(1, int(duration_sec or 0))
    if sec <= 10:
        return "TALKING_VIDEO_PREMIUM_10S", "LONGFORM_TALK_PREMIUM_10S"
    if sec <= 20:
        return "TALKING_VIDEO_PREMIUM_20S", "LONGFORM_TALK_PREMIUM_20S"
    # 30s is the per-segment/provider bucket. Longer runs must segment and stitch,
    # not hard-fail on the client.
    return "TALKING_VIDEO_PREMIUM_30S", "LONGFORM_TALK_PREMIUM_30S"


def get_pricing(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("pricing"), dict):
        return payload.get("pricing") or {}
    for container_key in ("job", "data", "result"):
        container = payload.get(container_key)
        if isinstance(container, dict) and isinstance(container.get("pricing"), dict):
            return container.get("pricing") or {}
    found = recursive_collect_first_dict(payload, {"pricing"})
    return found or {}


def get_pricing_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("pricing_summary"), dict):
        return payload.get("pricing_summary") or {}
    for container_key in ("job", "data", "result"):
        container = payload.get(container_key)
        if isinstance(container, dict) and isinstance(container.get("pricing_summary"), dict):
            return container.get("pricing_summary") or {}
    found = recursive_collect_first_dict(payload, {"pricing_summary"})
    return found or {}


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
        "amount": pricing.get("amount"),
        "final_amount": pricing.get("final_amount"),
        "currency": pricing.get("currency"),
        "ledger_entry_id": pricing.get("ledger_entry_id"),
        "billing_account_id": pricing.get("billing_account_id"),
        "source": pricing.get("source"),
        "reason": pricing.get("reason"),
        "summary": summary,
    }


def infer_job_status(payload: Dict[str, Any]) -> str:
    for key in ("status", "stage"):
        value = payload.get(key)
        if isinstance(value, str):
            return value.strip()
    job = payload.get("job")
    if isinstance(job, dict):
        for key in ("status", "stage"):
            value = job.get(key)
            if isinstance(value, str):
                return value.strip()
    return ""


def maybe_job_id(payload: Dict[str, Any]) -> Optional[str]:
    return first_present([
        payload.get("job_id"),
        payload.get("id"),
        recursive_collect_first_str(payload, {"job_id", "id"}),
    ])


def deep_find_artifact_ids(payload: Any) -> List[str]:
    out: List[str] = []
    stack = [payload]
    seen = set()
    candidate_keys = ("artifact_id", "audio_artifact_id", "face_artifact_id", "media_asset_id", "id")
    while stack:
        cur = stack.pop()
        oid = id(cur)
        if oid in seen:
            continue
        seen.add(oid)
        if isinstance(cur, dict):
            kind = str(cur.get("kind") or cur.get("asset_kind") or "").lower()
            for key in candidate_keys:
                value = cur.get(key)
                if isinstance(value, str) and value.strip():
                    if key in {"audio_artifact_id", "face_artifact_id", "artifact_id", "media_asset_id"} or kind in {"audio", "voice", "face", "image", "artifact"}:
                        out.append(value.strip())
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    deduped: List[str] = []
    seen_ids = set()
    for x in out:
        if x not in seen_ids:
            seen_ids.add(x)
            deduped.append(x)
    return deduped


def extract_face_asset_from_status(payload: Dict[str, Any], job_id: str) -> Dict[str, Optional[str]]:
    face_image_url = first_present([
        recursive_collect_first_str(payload, {"image_url"}),
        recursive_collect_first_str(payload, {"face_image_url", "preview_url", "signed_url", "url", "sas_url", "blob_url", "storage_ref"}),
    ])
    media_asset_id = recursive_collect_first_str(payload, {"media_asset_id"})
    artifact_id = recursive_collect_first_str(payload, {"face_artifact_id", "artifact_id", "selected_face_artifact_id"})
    return {
        "source": "generated",
        "source_job_id": job_id,
        "face_artifact_id": artifact_id,
        "face_media_asset_id": media_asset_id,
        "face_image_url": face_image_url,
    }


def extract_audio_asset_from_status(payload: Dict[str, Any], job_id: str) -> Dict[str, Optional[str]]:
    audio_url = first_present([
        recursive_collect_first_str(payload, {"audio_url"}),
        recursive_collect_first_str(payload, {"url", "signed_url", "sas_url", "blob_url", "storage_ref"}),
    ])
    artifact_id = recursive_collect_first_str(payload, {"artifact_id", "audio_artifact_id"})
    return {
        "source": "generated",
        "source_job_id": job_id,
        "audio_artifact_id": artifact_id,
        "audio_url": audio_url,
    }


def check_feature_allowed(preview_resp: Dict[str, Any]) -> Tuple[bool, str]:
    pricing = get_pricing(preview_resp)
    reason = str(pricing.get("reason") or "")
    state = str(pricing.get("state") or "")
    enabled = pricing.get("enabled")
    if enabled is False:
        return False, reason or "pricing_disabled"
    if reason and any(x in reason.lower() for x in ["blocked", "deny", "disabled", "insufficient", "not_entitled", "inactive", "unknown"]):
        return False, reason
    if state in {"reservation_failed", "commit_failed", "release_failed", "failed", "canceled", "cancelled"}:
        return False, reason or state
    return True, reason or state or "preview_ok"


def login(core_url: str, email: str, password: str) -> Dict[str, Any]:
    _, _, payload = http_json(
        "POST",
        f"{core_url}/api/auth/login",
        payload={"email": email, "password": password},
        timeout=30,
    )
    token = normalize_bearer(str(payload.get("access_token") or payload.get("token") or ""))
    if not token:
        raise RuntimeError(f"Login succeeded but no access token found: {payload}")
    user_id = first_present([payload.get("user_id"), payload.get("id"), jwt_sub(token)])
    if not user_id:
        raise RuntimeError("Could not resolve user_id from login response/JWT")
    return {"access_token": token, "user_id": user_id, "email": email, "raw": payload}


def _iter_voice_records(obj: Any) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    seen = set()

    def visit(value: Any) -> None:
        oid = id(value)
        if oid in seen:
            return
        seen.add(oid)
        if isinstance(value, dict):
            voice_id = first_present([
                value.get("voice_id"),
                value.get("id"),
                value.get("short_name"),
                value.get("voice"),
                value.get("name"),
            ])
            locale = first_present([
                value.get("locale"),
                value.get("locale_code"),
                value.get("target_locale"),
                value.get("language_locale"),
                value.get("language"),
                value.get("lang"),
            ])
            gender = first_present([value.get("gender"), value.get("sex")])
            display_name = first_present([value.get("display_name"), value.get("name"), value.get("short_name")])
            if voice_id and (locale or gender or display_name or "voice" in value or "voice_id" in value or "short_name" in value):
                records.append({
                    "voice_id": str(voice_id).strip(),
                    "locale": str(locale).strip() if locale else "",
                    "gender": lower_ascii(str(gender or "")),
                    "display_name": str(display_name).strip() if display_name else "",
                    "raw": value,
                })
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(obj)
    deduped: List[Dict[str, Any]] = []
    seen_keys = set()
    for row in records:
        key = (row.get("voice_id"), row.get("locale"), row.get("gender"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(row)
    return deduped



def _iter_locale_records(obj: Any) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            locale = first_present([
                value.get("locale"),
                value.get("locale_code"),
                value.get("target_locale"),
                value.get("language_locale"),
            ])
            default_voice = first_present([
                value.get("default_voice"),
                value.get("voice"),
                value.get("voice_id"),
                value.get("short_name"),
                value.get("name"),
            ])
            display_name = first_present([value.get("display_name"), value.get("native_name"), value.get("name")])
            if locale and default_voice:
                records.append({
                    "locale": str(locale).strip(),
                    "default_voice": str(default_voice).strip(),
                    "display_name": str(display_name).strip() if display_name else "",
                    "raw": value,
                })
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(obj)
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for row in records:
        key = (row.get("locale"), row.get("default_voice"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def resolve_audio_voice(args: argparse.Namespace, auth: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    requested_voice = str(getattr(args, "audio_voice", "") or "").strip()
    if requested_voice:
        result = {
            "resolved": True,
            "source": "arg",
            "voice_id": requested_voice,
            "locale": str(getattr(args, "audio_locale", "") or ""),
            "gender": lower_ascii(str(getattr(args, "face_gender", "") or "")),
            "catalog_count": 0,
        }
        write_json(os.path.join(out_dir, "audio", "voice_resolution.json"), result)
        return result

    ensure_dir(os.path.join(out_dir, "audio"))
    headers = common_headers(auth["access_token"], auth["user_id"])
    preferred_locale = str(getattr(args, "audio_locale", "") or "").strip()
    preferred_gender = lower_ascii(str(getattr(args, "face_gender", "") or ""))
    base_audio_url = str(getattr(args, "audio_service_url", "") or getattr(args, "audio_url", "") or "").rstrip("/")
    locales_url = f"{base_audio_url}/api/audio/catalog/locales"
    sync_url = f"{base_audio_url}/api/audio/catalog/sync"
    voices_url = f"{base_audio_url}/api/audio/catalog/voices?{urllib.parse.urlencode({'locale': preferred_locale})}"
    raw_payloads: Dict[str, Any] = {}
    errors: List[str] = []

    def try_fetch(url: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        try:
            _, _, resp_payload = http_json(method, url, headers=headers, payload=payload, timeout=120, accepted_statuses=(200, 201, 202))
            return resp_payload if isinstance(resp_payload, dict) else {"data": resp_payload}
        except Exception as ex:
            errors.append(f"{method.upper()} {url} failed: {ex}")
            return None

    locales_payload = try_fetch(locales_url, "GET")
    if locales_payload is None:
        sync_payload = try_fetch(sync_url, "POST", {})
        if sync_payload is not None:
            raw_payloads["sync"] = sync_payload
        locales_payload = try_fetch(locales_url, "GET")

    if locales_payload is not None:
        raw_payloads["locales"] = locales_payload

    locale_records = _iter_locale_records(locales_payload or raw_payloads)
    selected_locale = None
    for row in locale_records:
        if lower_ascii(str(row.get("locale") or "")) == lower_ascii(preferred_locale):
            selected_locale = row
            break

    if selected_locale and selected_locale.get("default_voice"):
        selected_voice = str(selected_locale["default_voice"]).strip()
        args.audio_voice = selected_voice
        result = {
            "resolved": True,
            "source": "locales_default_voice",
            "voice_id": selected_voice,
            "locale": selected_locale.get("locale") or preferred_locale,
            "gender": preferred_gender,
            "display_name": selected_locale.get("display_name"),
            "catalog_count": len(locale_records),
            "errors": errors,
        }
        write_json(os.path.join(out_dir, "audio", "voice_catalog_raw.json"), raw_payloads)
        write_json(os.path.join(out_dir, "audio", "voice_resolution.json"), result)
        return result

    voices_payload = try_fetch(voices_url, "GET")
    if voices_payload is not None:
        raw_payloads["voices"] = voices_payload

    records = _iter_voice_records(voices_payload or raw_payloads)
    preferred_lang = preferred_locale.split("-")[0].lower() if preferred_locale else ""

    def score(row: Dict[str, Any]) -> Tuple[int, int, int, str]:
        locale = str(row.get("locale") or "").strip()
        locale_lower = locale.lower()
        gender = lower_ascii(str(row.get("gender") or ""))
        exact_locale = 1 if preferred_locale and locale_lower == preferred_locale.lower() else 0
        lang_match = 1 if preferred_lang and locale_lower.startswith(preferred_lang) else 0
        gender_match = 1 if preferred_gender and gender == preferred_gender else 0
        return (exact_locale, lang_match, gender_match, str(row.get("voice_id") or ""))

    selected = max(records, key=score) if records else None
    result = {
        "resolved": bool(selected),
        "source": "voices_locale_query",
        "voice_id": selected.get("voice_id") if selected else None,
        "locale": selected.get("locale") if selected else preferred_locale,
        "gender": selected.get("gender") if selected else preferred_gender,
        "display_name": selected.get("display_name") if selected else None,
        "catalog_count": len(records),
        "errors": errors,
    }
    write_json(os.path.join(out_dir, "audio", "voice_catalog_raw.json"), raw_payloads)
    write_json(os.path.join(out_dir, "audio", "voice_resolution.json"), result)
    if not selected:
        raise RuntimeError(f"Unable to auto-resolve audio voice from svc-audio catalog for locale={preferred_locale}")
    args.audio_voice = str(selected.get("voice_id") or "").strip()
    return result

def get_balance(pricing_url: str, access_token: str, user_id: str) -> Dict[str, Any]:
    _, _, payload = http_json(
        "GET",
        f"{pricing_url}/api/credits/balance",
        headers=common_headers(access_token, user_id),
        timeout=30,
        accepted_statuses=(200,),
    )
    return payload if isinstance(payload, dict) else {"raw": payload}


def run(cmd: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            + " ".join(cmd)
            + f"\n\nSTDOUT:\n{p.stdout}\n\nSTDERR:\n{p.stderr}"
        )
    return p


def discover_db_container(explicit_name: str) -> Optional[str]:
    if explicit_name:
        return explicit_name
    try:
        out = run(["docker", "ps", "--format", "{{.Names}}"]).stdout or ""
    except Exception:
        return None
    names = [x.strip() for x in out.splitlines() if x.strip()]
    for name in names:
        if name == "desifaces-db":
            return name
    for name in names:
        if "db" in name and "desifaces" in name:
            return name
    return None


def psql_json_lines(container_name: str, sql: str) -> List[Dict[str, Any]]:
    shell = (
        "psql -U \"$POSTGRES_USER\" -d \"${POSTGRES_DB:-postgres}\" -At <<'SQL'\n"
        + sql
        + "\nSQL"
    )
    p = run(["docker", "exec", "-i", container_name, "bash", "-lc", shell], check=True)
    rows: List[Dict[str, Any]] = []
    for line in [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]:
        try:
            rows.append(json.loads(line))
        except Exception:
            rows.append({"raw": line})
    return rows


def query_user_entitlements(container_name: str, user_id: str) -> Dict[str, Any]:
    sql = f"""
with pue as (
  select json_build_object(
    'tier_code', tier_code,
    'billing_account_id', billing_account_id,
    'metadata_json', metadata_json
  ) as row_json
  from pricing_user_entitlements
  where user_id = '{user_id}'
  limit 1
),
be as (
  select json_build_object(
    'tier_code', tier_code,
    'plan_code', plan_code,
    'billing_mode', billing_mode,
    'settlement_mode', settlement_mode,
    'included_credits_total', included_credits_total,
    'included_credits_remaining', included_credits_remaining,
    'overage_allowed', overage_allowed,
    'wallet_topup_allowed', wallet_topup_allowed,
    'hard_stop_on_insufficient_balance', hard_stop_on_insufficient_balance,
    'source', source,
    'metadata_json', metadata_json
  ) as row_json
  from billing_entitlements
  where user_id = '{user_id}'
  order by updated_at desc
  limit 1
)
select json_build_object(
  'pricing_user_entitlements', (select row_json from pue),
  'billing_entitlements', (select row_json from be)
)::text;
"""
    rows = psql_json_lines(container_name, sql)
    return rows[0] if rows else {}


def query_feature_flags(container_name: str, tier_code: str) -> List[Dict[str, Any]]:
    sql = """
select json_build_object(
  'code', code,
  'enabled', enabled,
  'billing_mode', billing_mode,
  'metadata_json', metadata_json
)::text
from pricing_feature_flags
where code in ('FACE_STUDIO', 'AUDIO_STUDIO', 'TALKING_VIDEO', 'CINEMATIC_VIDEO_DIRECTION')
order by code;
"""
    rows = psql_json_lines(container_name, sql)
    result: List[Dict[str, Any]] = []
    for row in rows:
        metadata = row.get("metadata_json") if isinstance(row, dict) else {}
        allowed = None
        reason = "unknown"
        if isinstance(metadata, dict):
            allowed_tiers = metadata.get("allowed_tiers") or []
            denied_tiers = metadata.get("default_denied_tiers") or []
            if tier_code and tier_code in denied_tiers:
                allowed = False
                reason = "default_denied_tiers"
            elif tier_code and allowed_tiers:
                allowed = tier_code in allowed_tiers
                reason = "allowed_tiers"
        row["tier_allowed"] = allowed
        row["tier_reason"] = reason
        result.append(row)
    return result


def query_latest_face_jobs(container_name: str, user_id: str, limit: int = 25) -> List[Dict[str, Any]]:
    sql = f"""
select json_build_object(
  'id', id,
  'studio_type', studio_type,
  'status', status,
  'created_at', created_at,
  'payload_json', coalesce(payload_json, '{{}}'::jsonb),
  'meta_json', coalesce(meta_json, '{{}}'::jsonb)
)::text
from public.studio_jobs
where user_id = '{user_id}'
  and lower(studio_type) in ('face')
  and lower(status) in ('succeeded','completed','success')
order by created_at desc
limit {int(limit)};
"""
    return psql_json_lines(container_name, sql)


def capture_pricing_rows(container_name: Optional[str], job_id: Optional[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if not container_name or not job_id:
        return {"rows": []}, {"rows": []}

    reservations_sql = f"""
select json_build_object(
  'id', id,
  'user_id', user_id,
  'job_ref', job_ref,
  'status', status,
  'variant_code', coalesce(quote_json->>'variant_code', quote_json->>'variantCode'),
  'sku_code', sku_code,
  'reserved_credits', reserved_credits,
  'estimated_money', estimated_money,
  'currency', currency,
  'billing_account_id', billing_account_id,
  'settlement_mode', settlement_mode,
  'service_name', service_name,
  'service_action', service_action,
  'invoice_id', invoice_id,
  'created_at', created_at,
  'updated_at', updated_at
)::text
from pricing_credit_reservations
where job_ref = '{job_id}'
order by created_at desc
limit 10;
"""

    ledger_sql = f"""
select json_build_object(
  'id', le.id,
  'user_id', le.user_id,
  'studio_job_id', le.studio_job_id,
  'reservation_id', le.reservation_id,
  'event_type', le.event_type,
  'sku_code', le.sku_code,
  'credits_delta', le.credits_delta,
  'money_amount', le.money_amount,
  'currency', le.currency,
  'billing_account_id', le.billing_account_id,
  'settlement_mode', le.settlement_mode,
  'service_name', le.service_name,
  'service_action', le.service_action,
  'created_at', le.created_at
)::text
from pricing_credit_ledger_events le
where (
    le.studio_job_id = '{job_id}'::uuid
    or le.reservation_id in (
        select r.id
        from pricing_credit_reservations r
        where r.job_ref = '{job_id}'
    )
)
order by le.created_at desc
limit 20;
"""

    try:
        return (
            {"rows": psql_json_lines(container_name, reservations_sql)},
            {"rows": psql_json_lines(container_name, ledger_sql)},
        )
    except Exception as ex:
        return {"error": str(ex), "rows": []}, {"error": str(ex), "rows": []}


def extract_face_asset_from_jobs(rows: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    id_keys = {"media_asset_id", "face_artifact_id", "artifact_id", "image_artifact_id", "selected_face_artifact_id"}
    url_keys = {"face_image_url", "image_url", "preview_url", "signed_url", "url", "storage_ref", "sas_url", "blob_url"}
    for row in rows:
        artifact_id = recursive_collect_first_str(row, id_keys)
        image_url = recursive_collect_first_str(row, url_keys)
        if artifact_id or image_url:
            return {
                "source": "database",
                "source_job_id": str(row.get("id") or ""),
                "face_artifact_id": artifact_id,
                "face_image_url": image_url,
            }
    return {"source": None, "source_job_id": None, "face_artifact_id": None, "face_image_url": None}


def maybe_lookup_db_face(user_id: str, db_lookup_first: bool, container_name: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    meta: Dict[str, Any] = {"enabled": db_lookup_first, "container_name": container_name}
    if not db_lookup_first or not container_name:
        return None, meta
    try:
        face_rows = query_latest_face_jobs(container_name, user_id, limit=25)
        meta["face_row_count"] = len(face_rows)
        return extract_face_asset_from_jobs(face_rows), meta
    except Exception as ex:
        meta["note"] = f"db lookup failed: {ex}"
        return None, meta


def build_face_preview_payload(args: argparse.Namespace) -> Dict[str, Any]:
    creator = {
        "language": "en",
        "user_prompt": args.face_prompt,
        "num_variants": max(1, args.face_num_variants),
        "mode": "text-to-image",
        "age_range_code": args.face_age_range_code,
        "skin_tone_code": args.face_skin_tone_code,
        "region_code": args.face_region_code,
        "gender": args.face_gender,
        "image_format_code": args.face_image_format_code,
        "use_case_code": args.face_use_case_code,
        "style_code": args.face_style_code,
        "context_code": args.face_context_code,
    }
    return {"studio": "face", "action": "generate", "studio_input": creator}


def build_face_generate_payload(args: argparse.Namespace, preview_resp: Dict[str, Any]) -> Dict[str, Any]:
    studio_input = build_face_preview_payload(args)["studio_input"]
    pricing = get_pricing(preview_resp)
    quote_id = first_present([
        preview_resp.get("quote_id"),
        pricing.get("quote_id") if isinstance(pricing, dict) else None,
        recursive_collect_first_str(preview_resp, {"quote_id"}),
    ])
    preview_fp = first_present([
        preview_resp.get("preview_fingerprint"),
        pricing.get("preview_fingerprint") if isinstance(pricing, dict) else None,
        recursive_collect_first_str(preview_resp, {"preview_fingerprint"}),
    ])
    payload: Dict[str, Any] = {"studio_input": studio_input, "pricing_confirmation": {}}
    if quote_id:
        payload["pricing_confirmation"]["quote_id"] = quote_id
    if preview_fp:
        payload["pricing_confirmation"]["preview_fingerprint"] = preview_fp
    return payload


def build_audio_payload(args: argparse.Namespace, preview_resp: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "text": args.audio_text,
        "target_locale": args.audio_locale,
        "source_language": args.audio_source_language,
        "translate": args.audio_translate,
        "voice": args.audio_voice,
        "style": args.audio_style or None,
        "style_degree": args.audio_style_degree if args.audio_style_degree != 0 else None,
        "rate": args.audio_rate if args.audio_rate != 0 else None,
        "pitch": args.audio_pitch if args.audio_pitch != 0 else None,
        "volume": args.audio_volume if args.audio_volume != 0 else None,
        "context": args.audio_context or None,
        "output_format": args.audio_output_format,
    }
    if preview_resp is not None:
        pricing = get_pricing(preview_resp)
        quote_id = first_present([
            preview_resp.get("quote_id"),
            pricing.get("quote_id") if isinstance(pricing, dict) else None,
            recursive_collect_first_str(preview_resp, {"quote_id"}),
        ])
        preview_fp = first_present([
            preview_resp.get("preview_fingerprint"),
            pricing.get("preview_fingerprint") if isinstance(pricing, dict) else None,
            recursive_collect_first_str(preview_resp, {"preview_fingerprint"}),
        ])
        if quote_id or preview_fp:
            payload["pricing_confirmation"] = {}
            if quote_id:
                payload["pricing_confirmation"]["quote_id"] = quote_id
            if preview_fp:
                payload["pricing_confirmation"]["preview_fingerprint"] = preview_fp
    return payload


def build_video_scenarios(args: argparse.Namespace) -> List[Dict[str, Any]]:
    raw = (args.video_scenarios or '').strip()
    requested_duration_sec = max(1, int(args.video_duration_sec or 0))
    economy_variant_code, economy_sku_code = _economy_bucket_expectations(requested_duration_sec)
    premium_variant_code, premium_sku_code = _premium_bucket_expectations(requested_duration_sec)
    economy_segment_count = _talking_video_segment_count(requested_duration_sec, 30)
    premium_segment_count = _talking_video_segment_count(requested_duration_sec, 30)
    default_specs: List[Dict[str, Any]] = [
        {
            "name": "talking_video_economy",
            "api_mode": "legacy",
            "logical_mode": "talking_video",
            "quality_tier": "economy",
            "provider_hint": "veed_fabric",
            "provider": "veed_fabric",
            "output_profile": "economy",
            "title_suffix": "Talking Video Economy",
            "goal_suffix": "Use the economy talking video path for Fusion Studio.",
            "expected_variant_code": economy_variant_code,
            "expected_sku_code": economy_sku_code,
            "expected_segment_count": economy_segment_count,
            "expected_segmented": economy_segment_count > 1,
            "expected_bucket_max_sec": 30,
        },
        {
            "name": "talking_video_premium",
            "api_mode": "legacy",
            "logical_mode": "talking_video",
            "quality_tier": "premium",
            "provider_hint": "kling",
            "provider": "kling",
            "output_profile": "premium",
            "title_suffix": "Talking Video Premium",
            "goal_suffix": "Use the premium talking video KLING direct-avatar path for Fusion Studio with audio-driven performance. Long runs must segment and stitch.",
            "expected_variant_code": premium_variant_code,
            "expected_sku_code": premium_sku_code,
            "expected_segment_count": premium_segment_count,
            "expected_segmented": premium_segment_count > 1,
            "expected_bucket_max_sec": 30,
        },
        {
            "name": "cinematic_fast",
            "api_mode": "directed",
            "logical_mode": "cinematic_video_direction",
            "quality_tier": "premium",
            "provider_hint": args.video_provider,
            "provider": args.video_provider,
            "output_profile": "fast",
            "title_suffix": "Cinematic Fast",
            "goal_suffix": "Use the fast cinematic path for Fusion Studio.",
            "expected_variant_code": "CINEMATIC_VIDEO_DIRECTION",
            "expected_sku_code": "LONGFORM_CINEMATIC_MIN",
        },
        {
            "name": "cinematic_premium",
            "api_mode": "directed",
            "logical_mode": "cinematic_video_direction",
            "quality_tier": "premium",
            "provider_hint": args.video_provider,
            "provider": args.video_provider,
            "output_profile": "premium",
            "title_suffix": "Cinematic Premium",
            "goal_suffix": "Use the premium cinematic path for Fusion Studio.",
            "expected_variant_code": "CINEMATIC_VIDEO_DIRECTION",
            "expected_sku_code": "LONGFORM_CINEMATIC_MIN",
        },
    ]
    if not raw:
        return default_specs

    default_by_name = {spec["name"]: dict(spec) for spec in default_specs}
    alias_map: Dict[str, str] = {
        'talking_video_economy': 'talking_video_economy',
        'talking_economy': 'talking_video_economy',
        'economy': 'talking_video_economy',
        'veed': 'talking_video_economy',
        'veed_fabric': 'talking_video_economy',
        'talking_video': 'talking_video_premium',
        'talking': 'talking_video_premium',
        'tv': 'talking_video_premium',
        'talking_video_premium': 'talking_video_premium',
        'premium_talking_video': 'talking_video_premium',
        'cinematic_fast': 'cinematic_fast',
        'cinematic-video-fast': 'cinematic_fast',
        'fast': 'cinematic_fast',
        'cinematic_premium': 'cinematic_premium',
        'cinematic-video-premium': 'cinematic_premium',
        'premium': 'cinematic_premium',
    }

    def normalize_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        name = _scenario_field(item, 'name') or ''
        aliased = alias_map.get(lower_ascii(name)) if name else None
        base = dict(default_by_name.get(aliased or name) or {})
        if not base:
            base_name = lower_ascii(_scenario_field(item, 'logical_mode', 'mode') or '')
            if base_name == 'talking_video' and _normalize_quality_tier(item.get('quality_tier')) == 'economy':
                base = dict(default_by_name['talking_video_economy'])
            elif base_name == 'talking_video':
                base = dict(default_by_name['talking_video_premium'])
            elif base_name == 'cinematic_video_direction':
                base = dict(default_by_name['cinematic_premium'])
        if not base:
            return None
        base['name'] = _scenario_field(item, 'name') or base['name']
        base['api_mode'] = lower_ascii(_scenario_field(item, 'api_mode', 'mode') or base.get('api_mode') or 'legacy')
        base['logical_mode'] = lower_ascii(_scenario_field(item, 'logical_mode') or base.get('logical_mode') or 'talking_video')
        base['quality_tier'] = _normalize_quality_tier(item.get('quality_tier') or base.get('quality_tier'))
        base['provider_hint'] = _scenario_field(item, 'provider_hint', 'provider') or base.get('provider_hint') or args.video_provider
        base['provider'] = _scenario_field(item, 'provider') or base.get('provider') or base['provider_hint']
        base['output_profile'] = _scenario_field(item, 'output_profile', 'profile') or base.get('output_profile') or ''
        base['title_suffix'] = _scenario_field(item, 'title_suffix') or base.get('title_suffix') or base['name']
        base['goal_suffix'] = _scenario_field(item, 'goal_suffix') or base.get('goal_suffix') or ''
        base['expected_variant_code'] = _scenario_field(item, 'expected_variant_code') or base.get('expected_variant_code')
        base['expected_sku_code'] = _scenario_field(item, 'expected_sku_code') or base.get('expected_sku_code')
        try:
            if item.get('expected_segment_count') is not None:
                base['expected_segment_count'] = max(1, int(item.get('expected_segment_count')))
        except Exception:
            pass
        if item.get('expected_segmented') is not None:
            base['expected_segmented'] = str(item.get('expected_segmented')).strip().lower() in {'1', 'true', 'yes', 'on'}
        try:
            if item.get('expected_bucket_max_sec') is not None:
                base['expected_bucket_max_sec'] = max(10, int(item.get('expected_bucket_max_sec')))
        except Exception:
            pass
        return base

    if raw.startswith('['):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                out: List[Dict[str, Any]] = []
                seen = set()
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    normalized = normalize_item(item)
                    if not normalized:
                        continue
                    key = str(normalized.get('name') or '').strip().lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(normalized)
                if out:
                    return out
        except Exception:
            pass

    out: List[Dict[str, Any]] = []
    seen = set()
    for token in [x.strip() for x in raw.split(',') if x.strip()]:
        canonical = alias_map.get(lower_ascii(token), lower_ascii(token))
        spec = default_by_name.get(canonical)
        if not spec:
            continue
        key = spec['name']
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(spec))
    return out or default_specs


def build_fusion_extension_payload(
    args: argparse.Namespace,
    scenario: Dict[str, Any],
    face_artifact_id: Optional[str],
    audio_artifact_id: Optional[str],
    face_image_url: Optional[str] = None,
    audio_url: Optional[str] = None,
) -> Dict[str, Any]:
    if not face_artifact_id:
        raise RuntimeError("svc-fusion-extension requires face_artifact_id as the primary face input")

    scenario_name = str(scenario.get('name') or '').strip() or 'video'
    api_mode = lower_ascii(str(scenario.get('api_mode') or scenario.get('mode') or 'legacy'))
    logical_mode = lower_ascii(str(scenario.get('logical_mode') or 'talking_video'))
    output_profile = str(scenario.get('output_profile') or '').strip()
    quality_tier = _normalize_quality_tier(scenario.get('quality_tier') or 'premium')
    provider_hint = first_present([scenario.get('provider_hint'), scenario.get('provider')])
    provider_name = first_present([scenario.get('provider'), provider_hint, args.video_provider]) or 'omnihuman_v15'

    goal_text = first_present([
        args.video_goal,
        args.video_script_text,
        args.audio_text,
        args.face_prompt,
    ]) or "Create a short, high-quality DesiFaces talking video."

    scenario_goal = first_present([scenario.get('goal_suffix')])
    title_suffix = first_present([scenario.get('title_suffix'), scenario_name]) or scenario_name

    requested_duration_sec = max(1, int(float(args.video_duration_sec or 0)))
    requested_units = max(1, int(math.ceil(float(requested_duration_sec) / 60.0)))
    script_text_value = first_present([
        args.video_script_text,
        args.audio_text,
        args.video_goal,
        goal_text,
    ])

    is_talking_premium = logical_mode == 'talking_video' and quality_tier == 'premium'
    camera_motion_style = str(args.video_camera_motion_style or '').strip()
    if is_talking_premium and not camera_motion_style:
        camera_motion_style = 'slow_push_in'

    payload: Dict[str, Any] = {
        "mode": api_mode,
        "scenario_name": scenario_name,
        "title": f"{args.video_title} - {title_suffix}",
        "goal": f"{goal_text} {scenario_goal}".strip(),
        "duration_sec": requested_duration_sec,
        "requested_duration_sec": requested_duration_sec,
        "pricing_duration_sec": requested_duration_sec,
        "video_duration_sec": requested_duration_sec,
        "video": {
            "duration_sec": requested_duration_sec,
            "requested_duration_sec": requested_duration_sec,
            "pricing_duration_sec": requested_duration_sec,
        },
        "intent": {
            "goal": f"{goal_text} {scenario_goal}".strip(),
            "duration_sec": requested_duration_sec,
        },
        "minutes": requested_units,
        "requested_units": requested_units,
        "face_artifact_id": face_artifact_id,
        "consent": {"external_provider_ok": True},
        "provider": provider_name,
        "longform_profile": logical_mode,
        "quality_tier": quality_tier,
        "provider_hint": provider_hint or provider_name,
        "tags": {
            "source": "df_e2e_fusion_extension_modes_v11",
            "longform_profile": logical_mode,
            "requested_longform_profile": logical_mode,
            "quality_tier": quality_tier,
            "requested_quality_tier": quality_tier,
            "provider_hint": provider_hint or provider_name,
            "client_surface": "fusion_studio",
            "fusion_studio_mode": scenario_name,
            "scenario_name": scenario_name,
            "output_profile": output_profile,
            "minutes": requested_units,
            "requested_units": requested_units,
            "duration_sec": requested_duration_sec,
            "requested_duration_sec": requested_duration_sec,
            "pricing_duration_sec": requested_duration_sec,
            "video_duration_sec": requested_duration_sec,
        },
    }

    if logical_mode == 'cinematic_video_direction' or is_talking_premium:
        payload['background_mode'] = 'movement_based'
    else:
        payload['background_mode'] = 'fixed'

    if script_text_value:
        payload["script_text"] = script_text_value
    if args.video_aspect_ratio:
        payload["aspect_ratio"] = args.video_aspect_ratio
    if args.video_camera_angle:
        payload["camera_angle"] = args.video_camera_angle
    if args.video_camera_framing:
        payload["camera_framing"] = args.video_camera_framing
    if camera_motion_style:
        payload["camera_motion_style"] = camera_motion_style
    if output_profile:
        payload["output_profile"] = output_profile

    if audio_artifact_id:
        payload["voice_audio"] = {"audio_artifact_id": audio_artifact_id}
    elif audio_url:
        payload["voice_audio"] = {"audio_url": audio_url}

    if is_talking_premium:
        payload["provider_options"] = {
            "provider_hint": "veed_fabric",
            "fusion_provider": "veed_fabric",
            "presenter_provider": "veed_fabric",
            "quality_tier": "premium",
            "output_profile": "premium",
            "background_mode": "movement_based",
            "background_provider": "luma",
            "background_profile": {
                "provider": "luma",
                "resolution": "540p",
                "duration_sec": 5,
                "loop": True,
                "apply_film": True,
                "apply_upscaler": False,
                "motion_level": "noticeable",
                "motion_intent": "ambient_realism",
                "camera_motion_style": camera_motion_style or 'slow_push_in',
            },
            "presenter_with_motion_bg": {"enabled": True},
            "motion_intent": "ambient_realism",
            "ambient_wind": True,
            "dynamic_lighting": True,
            "parallax_strength": "medium",
            "camera_motion_style": camera_motion_style or 'slow_push_in',
        }
        payload["tags"].update({
            "background_mode": "movement_based",
            "presenter_with_motion_bg": True,
            "dynamic_background_enabled": True,
            "background_provider": "luma",
            "background_resolution": "540p",
            "background_duration_sec": 5,
            "background_loop": True,
            "motion_level": "noticeable",
            "motion_intent": "ambient_realism",
            "camera_motion_style": camera_motion_style or 'slow_push_in',
        })
    elif logical_mode == 'cinematic_video_direction':
        payload["tags"].update({
            "background_mode": "movement_based",
        })

    if face_image_url:
        payload["tags"]["face_image_url_hint"] = face_image_url

    return payload


def try_face_preview(args: argparse.Namespace, auth: Dict[str, Any]) -> Dict[str, Any]:
    _, _, resp = http_json(
        "POST",
        f"{args.face_url}{args.face_preview_path}",
        headers=common_headers(auth["access_token"], auth["user_id"]),
        payload=build_face_preview_payload(args),
        timeout=60,
    )
    return resp if isinstance(resp, dict) else {"raw": resp}


def try_face_generate(args: argparse.Namespace, auth: Dict[str, Any], preview_resp: Dict[str, Any]) -> Dict[str, Any]:
    _, _, resp = http_json(
        "POST",
        f"{args.face_url}{args.face_generate_path}",
        headers=common_headers(auth["access_token"], auth["user_id"]),
        payload=build_face_generate_payload(args, preview_resp),
        timeout=90,
    )
    return resp if isinstance(resp, dict) else {"raw": resp}


def try_audio_preview(args: argparse.Namespace, auth: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not args.audio_preview_path:
        return None, "preview_disabled"
    try:
        _, _, resp = http_json(
            "POST",
            f"{args.audio_service_url}{args.audio_preview_path}",
            headers=common_headers(auth["access_token"], auth["user_id"]),
            payload=build_audio_payload(args),
            timeout=60,
        )
        return (resp if isinstance(resp, dict) else {"raw": resp}), None
    except Exception as ex:
        msg = str(ex)
        if "[404]" in msg or '"detail":"Not Found"' in msg:
            return None, "preview_unsupported_404"
        raise


def try_audio_generate(args: argparse.Namespace, auth: Dict[str, Any], preview_resp: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    _, _, resp = http_json(
        "POST",
        f"{args.audio_service_url}{args.audio_generate_path}",
        headers=common_headers(auth["access_token"], auth["user_id"]),
        payload=build_audio_payload(args, preview_resp),
        timeout=90,
    )
    return resp if isinstance(resp, dict) else {"raw": resp}


def try_fusion_extension_preview(args: argparse.Namespace, auth: Dict[str, Any], scenario: Dict[str, Any], face_artifact_id: Optional[str], audio_artifact_id: Optional[str], face_image_url: Optional[str] = None, audio_url: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    payload = build_fusion_extension_payload(args, scenario, face_artifact_id, audio_artifact_id, face_image_url=face_image_url, audio_url=audio_url)
    _, _, resp = http_json(
        "POST",
        f"{args.video_url}{args.video_preview_path}",
        headers=common_headers(auth["access_token"], auth["user_id"]),
        payload=payload,
        timeout=60,
    )
    return (resp if isinstance(resp, dict) else {"raw": resp}), None


def try_fusion_extension_generate(args: argparse.Namespace, auth: Dict[str, Any], scenario: Dict[str, Any], face_artifact_id: Optional[str], audio_artifact_id: Optional[str], face_image_url: Optional[str] = None, audio_url: Optional[str] = None) -> Dict[str, Any]:
    _, _, resp = http_json(
        "POST",
        f"{args.video_url}{args.video_generate_path}",
        headers=common_headers(auth["access_token"], auth["user_id"]),
        payload=build_fusion_extension_payload(args, scenario, face_artifact_id, audio_artifact_id, face_image_url=face_image_url, audio_url=audio_url),
        timeout=90,
    )
    return resp if isinstance(resp, dict) else {"raw": resp}


def get_job_with_reauth(args: argparse.Namespace, current_auth: Dict[str, Any], job_base_url: str, path_template: str, job_id: str) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
    url = f"{job_base_url}{path_template.format(job_id=urllib.parse.quote(job_id))}"
    try:
        _, _, payload = http_json("GET", url, headers=common_headers(current_auth["access_token"], current_auth["user_id"]), timeout=60)
        return current_auth, (payload if isinstance(payload, dict) else {"raw": payload}), url
    except Exception as ex:
        msg = str(ex)
        if "[401]" not in msg and "401 Unauthorized" not in msg:
            raise
        refreshed = login(args.core_url, args.email, args.password)
        _, _, payload = http_json("GET", url, headers=common_headers(refreshed["access_token"], refreshed["user_id"]), timeout=60)
        return refreshed, (payload if isinstance(payload, dict) else {"raw": payload}), url


def poll_job(args: argparse.Namespace, auth: Dict[str, Any], base_url: str, status_path: str, job_id: str, out_path: str) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
    deadline = time.time() + float(args.job_timeout_seconds)
    last_payload: Dict[str, Any] = {}
    last_exception: Optional[str] = None
    current_auth = dict(auth)
    while time.time() < deadline:
        try:
            current_auth, payload, url = get_job_with_reauth(args, current_auth, base_url, status_path, job_id)
            last_payload = payload
            write_json(out_path, payload)
            pricing_state = lower_ascii(str(get_pricing(payload).get("state") or ""))
            job_status = lower_ascii(infer_job_status(payload))
            if pricing_state in TERMINAL_PRICING_STATES or job_status in TERMINAL_JOB_STATUSES:
                return current_auth, url, payload
        except Exception as ex:
            last_exception = str(ex)
        time.sleep(args.poll_seconds)
    raise RuntimeError(f"Timed out polling job {job_id}. last_payload={last_payload} last_exception={last_exception}")


def run_face(args: argparse.Namespace, auth: Dict[str, Any], out_dir: str, container_name: Optional[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    svc_dir = ensure_dir(os.path.join(out_dir, "face"))
    result: Dict[str, Any] = {
        "service": "face",
        "preview_ok": False,
        "allowed_by_preview": False,
        "preview_reason": None,
        "preview_pricing": {},
        "generated": False,
        "job_id": None,
        "job_status": None,
        "final_pricing": {},
        "artifact_ids": [],
        "blocked_or_failed_reason": None,
    }

    try:
        preview_resp = try_face_preview(args, auth)
        result["preview_ok"] = True
        result["allowed_by_preview"] = True
        result["preview_reason"] = "preview_ok"
        result["preview_pricing"] = pricing_brief(preview_resp)
        write_json(os.path.join(svc_dir, "preview.json"), preview_resp)

        generate_resp = try_face_generate(args, auth, preview_resp)
        write_json(os.path.join(svc_dir, "generate.json"), generate_resp)
        result["generate_pricing"] = pricing_brief(generate_resp)

        job_id = maybe_job_id(generate_resp)
        result["job_id"] = job_id
        if not job_id:
            result["blocked_or_failed_reason"] = "missing_job_id_in_generate_response"
            write_json(os.path.join(svc_dir, "result.json"), result)
            return auth, result

        auth, status_url, final_status = poll_job(args, auth, args.face_url, args.face_status_path, job_id, os.path.join(svc_dir, "status_last.json"))
        result["generated"] = True
        result["status_url"] = status_url
        result["job_status"] = infer_job_status(final_status)
        result["final_pricing"] = pricing_brief(final_status)

        face_asset = extract_face_asset_from_status(final_status, job_id)
        artifact_ids: List[str] = []
        if face_asset.get("face_artifact_id"):
            artifact_ids.append(str(face_asset["face_artifact_id"]))
        if not artifact_ids and container_name:
            db_face, _ = maybe_lookup_db_face(auth["user_id"], True, container_name)
            if db_face and db_face.get("face_artifact_id"):
                artifact_ids = [db_face["face_artifact_id"]]
            if db_face and not face_asset.get("face_image_url"):
                face_asset["face_image_url"] = db_face.get("face_image_url")
        result["artifact_ids"] = artifact_ids
        result["face_image_url"] = face_asset.get("face_image_url")
        result["face_media_asset_id"] = face_asset.get("face_media_asset_id")

        if result.get('expected_final_variant_code'):
            result['actual_final_variant_code'] = first_present([result['final_pricing'].get('variant_code')])
        if result.get('expected_final_sku_code'):
            result['actual_final_sku_code'] = first_present([result['final_pricing'].get('sku_code')])

        reservations, ledger = capture_pricing_rows(container_name, job_id)
        write_json(os.path.join(svc_dir, "pricing_reservations.json"), reservations)
        write_json(os.path.join(svc_dir, "pricing_ledger.json"), ledger)

        if lower_ascii(result["job_status"] or "") in {"failed", "canceled", "cancelled"}:
            result["blocked_or_failed_reason"] = first_present([get_pricing(final_status).get("reason"), result["job_status"]])
        write_json(os.path.join(svc_dir, "result.json"), result)
        return auth, result

    except Exception as ex:
        result["blocked_or_failed_reason"] = str(ex)
        write_json(os.path.join(svc_dir, "result.json"), result)
        return auth, result


def run_audio(args: argparse.Namespace, auth: Dict[str, Any], out_dir: str, container_name: Optional[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    svc_dir = ensure_dir(os.path.join(out_dir, "audio"))
    result: Dict[str, Any] = {
        "service": "audio",
        "preview_ok": False,
        "allowed_by_preview": False,
        "preview_reason": None,
        "preview_pricing": {},
        "generated": False,
        "job_id": None,
        "job_status": None,
        "final_pricing": {},
        "artifact_ids": [],
        "blocked_or_failed_reason": None,
    }

    try:
        voice_resolution = resolve_audio_voice(args, auth, out_dir)
        result["voice_resolution"] = voice_resolution
    except Exception as ex:
        result["blocked_or_failed_reason"] = f"audio_voice_resolution_failed: {ex}"
        write_json(os.path.join(svc_dir, "result.json"), result)
        return auth, result

    try:
        preview_resp, preview_note = try_audio_preview(args, auth)
        if preview_resp is not None:
            result["preview_ok"] = True
            result["allowed_by_preview"] = True
            result["preview_reason"] = "preview_ok"
            result["preview_pricing"] = pricing_brief(preview_resp)
            write_json(os.path.join(svc_dir, "preview.json"), preview_resp)
        else:
            result["preview_ok"] = False
            result["allowed_by_preview"] = True
            result["preview_reason"] = preview_note
            write_json(os.path.join(svc_dir, "preview.json"), {"note": preview_note})

        generate_resp = try_audio_generate(args, auth, preview_resp)
        write_json(os.path.join(svc_dir, "generate.json"), generate_resp)
        result["generate_pricing"] = pricing_brief(generate_resp)

        job_id = maybe_job_id(generate_resp)
        result["job_id"] = job_id
        if not job_id:
            result["blocked_or_failed_reason"] = "missing_job_id_in_generate_response"
            write_json(os.path.join(svc_dir, "result.json"), result)
            return auth, result

        auth, status_url, final_status = poll_job(args, auth, args.audio_service_url, args.audio_status_path, job_id, os.path.join(svc_dir, "status_last.json"))
        result["generated"] = True
        result["status_url"] = status_url
        result["job_status"] = infer_job_status(final_status)
        result["final_pricing"] = pricing_brief(final_status)
        audio_asset = extract_audio_asset_from_status(final_status, job_id)
        artifact_ids = deep_find_artifact_ids(final_status)
        if not artifact_ids and audio_asset.get("audio_artifact_id"):
            artifact_ids = [str(audio_asset["audio_artifact_id"])]
        result["artifact_ids"] = artifact_ids
        result["audio_url"] = audio_asset.get("audio_url")

        reservations, ledger = capture_pricing_rows(container_name, job_id)
        write_json(os.path.join(svc_dir, "pricing_reservations.json"), reservations)
        write_json(os.path.join(svc_dir, "pricing_ledger.json"), ledger)

        if lower_ascii(result["job_status"] or "") in {"failed", "canceled", "cancelled"}:
            result["blocked_or_failed_reason"] = first_present([get_pricing(final_status).get("reason"), result["job_status"]])
        write_json(os.path.join(svc_dir, "result.json"), result)
        return auth, result

    except Exception as ex:
        result["blocked_or_failed_reason"] = str(ex)
        write_json(os.path.join(svc_dir, "result.json"), result)
        return auth, result


def run_fusion_extension(
    args: argparse.Namespace,
    auth: Dict[str, Any],
    out_dir: str,
    container_name: Optional[str],
    scenario: Dict[str, Any],
    face_artifact_id: Optional[str],
    audio_artifact_id: Optional[str],
    face_image_url: Optional[str] = None,
    audio_url: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    scenario_name = str(scenario.get('name') or scenario.get('mode') or 'video').strip() or 'video'
    svc_dir = ensure_dir(os.path.join(out_dir, 'fusion_extension', scenario_name))
    result: Dict[str, Any] = {
        'service': 'fusion_extension',
        'scenario_name': scenario_name,
        'mode': scenario.get('logical_mode') or scenario.get('api_mode') or scenario.get('mode'),
        'api_mode': scenario.get('api_mode') or scenario.get('mode'),
        'quality_tier': scenario.get('quality_tier'),
        'provider_hint': scenario.get('provider_hint') or scenario.get('provider'),
        'output_profile': scenario.get('output_profile'),
        'preview_ok': False,
        'allowed_by_preview': False,
        'preview_reason': None,
        'preview_pricing': {},
        'generated': False,
        'job_id': None,
        'job_status': None,
        'final_pricing': {},
        'artifact_ids': [],
        'blocked_or_failed_reason': None,
        'expected_segment_count': scenario.get('expected_segment_count'),
        'expected_segmented': scenario.get('expected_segmented'),
        'expected_bucket_max_sec': scenario.get('expected_bucket_max_sec'),
    }

    if not face_artifact_id:
        result['blocked_or_failed_reason'] = 'fusion_extension_skipped_missing_face_artifact_id'
        write_json(os.path.join(svc_dir, 'result.json'), result)
        return auth, result
    if not (audio_artifact_id or audio_url):
        result['blocked_or_failed_reason'] = 'fusion_extension_skipped_missing_audio_artifact_or_url'
        write_json(os.path.join(svc_dir, 'result.json'), result)
        return auth, result

    try:
        preview_resp, preview_note = try_fusion_extension_preview(args, auth, scenario, face_artifact_id, audio_artifact_id, face_image_url=face_image_url, audio_url=audio_url)
        if preview_resp is not None:
            result['preview_ok'] = True
            allowed, preview_reason = check_feature_allowed(preview_resp)
            result['allowed_by_preview'] = allowed
            result['preview_reason'] = preview_reason
            result['preview_pricing'] = pricing_brief(preview_resp)
            expected_variant = first_present([scenario.get('expected_variant_code')])
            expected_sku = first_present([scenario.get('expected_sku_code')])
            actual_preview_variant = first_present([result['preview_pricing'].get('variant_code')])
            actual_preview_sku = first_present([result['preview_pricing'].get('sku_code')])
            if expected_variant:
                result['expected_preview_variant_code'] = expected_variant
                result['actual_preview_variant_code'] = actual_preview_variant
            if expected_sku:
                result['expected_preview_sku_code'] = expected_sku
                result['actual_preview_sku_code'] = actual_preview_sku
            write_json(os.path.join(svc_dir, 'preview.json'), preview_resp)
            if not allowed:
                result['blocked_or_failed_reason'] = preview_reason
                write_json(os.path.join(svc_dir, 'result.json'), result)
                return auth, result
        else:
            result['preview_ok'] = False
            result['allowed_by_preview'] = True
            result['preview_reason'] = preview_note
            write_json(os.path.join(svc_dir, 'preview.json'), {'note': preview_note})

        generate_resp = try_fusion_extension_generate(args, auth, scenario, face_artifact_id, audio_artifact_id, face_image_url=face_image_url, audio_url=audio_url)
        write_json(os.path.join(svc_dir, 'generate.json'), generate_resp)
        result['generate_pricing'] = pricing_brief(generate_resp)

        job_id = maybe_job_id(generate_resp)
        result['job_id'] = job_id
        if not job_id:
            result['blocked_or_failed_reason'] = 'missing_job_id_in_generate_response'
            write_json(os.path.join(svc_dir, 'result.json'), result)
            return auth, result

        auth, status_url, final_status = poll_job(args, auth, args.video_url, args.video_status_path, job_id, os.path.join(svc_dir, 'status_last.json'))
        result['generated'] = True
        result['status_url'] = status_url
        result['job_status'] = infer_job_status(final_status)
        result['final_pricing'] = pricing_brief(final_status)
        expected_variant = first_present([scenario.get('expected_variant_code')])
        expected_sku = first_present([scenario.get('expected_sku_code')])
        actual_final_variant = first_present([result['final_pricing'].get('variant_code')])
        actual_final_sku = first_present([result['final_pricing'].get('sku_code')])
        if expected_variant:
            result['expected_final_variant_code'] = expected_variant
            result['actual_final_variant_code'] = actual_final_variant
        if expected_sku:
            result['expected_final_sku_code'] = expected_sku
            result['actual_final_sku_code'] = actual_final_sku
        audio_asset = extract_audio_asset_from_status(final_status, job_id)
        artifact_ids = deep_find_artifact_ids(final_status)
        if not artifact_ids and audio_asset.get('audio_artifact_id'):
            artifact_ids = [str(audio_asset['audio_artifact_id'])]
        result['artifact_ids'] = artifact_ids
        result['audio_url'] = audio_asset.get('audio_url')
        result['final_video_url'] = first_present([
            final_status.get('final_video_url') if isinstance(final_status, dict) else None,
            final_status.get('output_video_url') if isinstance(final_status, dict) else None,
            final_status.get('share_url') if isinstance(final_status, dict) else None,
            recursive_collect_first_str(final_status, {'final_video_url', 'output_video_url', 'share_url'}) if isinstance(final_status, dict) else None,
        ])
        if isinstance(final_status, dict):
            result['actual_total_segments'] = final_status.get('total_segments')
            result['actual_completed_segments'] = final_status.get('completed_segments')
            result['actual_stage'] = final_status.get('stage')
            result['actual_status'] = final_status.get('status')

        pricing_state = lower_ascii(str((result['final_pricing'] or {}).get('state') or ''))
        if lower_ascii(result['job_status'] or '') in SUCCESS_JOB_STATUSES and pricing_state not in TERMINAL_PRICING_STATES:
            extra_deadline = time.time() + max(30, int(getattr(args, 'video_pricing_timeout_seconds', 120)))
            while time.time() < extra_deadline:
                time.sleep(5)
                auth, status_url, final_status = poll_job(args, auth, args.video_url, args.video_status_path, job_id, os.path.join(svc_dir, 'status_last.json'))
                result['status_url'] = status_url
                result['job_status'] = infer_job_status(final_status)
                result['final_pricing'] = pricing_brief(final_status)
                result['final_video_url'] = first_present([
                    final_status.get('final_video_url') if isinstance(final_status, dict) else None,
                    final_status.get('output_video_url') if isinstance(final_status, dict) else None,
                    final_status.get('share_url') if isinstance(final_status, dict) else None,
                    recursive_collect_first_str(final_status, {'final_video_url', 'output_video_url', 'share_url'}) if isinstance(final_status, dict) else None,
                ])
                pricing_state = lower_ascii(str((result['final_pricing'] or {}).get('state') or ''))
                if pricing_state in TERMINAL_PRICING_STATES:
                    break

        reservations, ledger = capture_pricing_rows(container_name, job_id)
        write_json(os.path.join(svc_dir, 'pricing_reservations.json'), reservations)
        write_json(os.path.join(svc_dir, 'pricing_ledger.json'), ledger)

        if lower_ascii(result['job_status'] or '') in {'failed', 'canceled', 'cancelled'}:
            result['blocked_or_failed_reason'] = first_present([get_pricing(final_status).get('reason'), result['job_status']])
        write_json(os.path.join(svc_dir, 'result.json'), result)
        return auth, result

    except Exception as ex:
        result['blocked_or_failed_reason'] = str(ex)
        write_json(os.path.join(svc_dir, 'result.json'), result)
        return auth, result


def summarize_user(out_dir: str, auth: Dict[str, Any], db_snapshot: Dict[str, Any], feature_flags: List[Dict[str, Any]], balance_before: Optional[Dict[str, Any]], balance_after: Optional[Dict[str, Any]], results: List[Dict[str, Any]]) -> Dict[str, Any]:
    tier_codes = [r.get("preview_pricing", {}).get("tier_code") for r in results if r.get("preview_pricing", {}).get("tier_code")]
    pue = db_snapshot.get("pricing_user_entitlements") if isinstance(db_snapshot, dict) else {}
    be = db_snapshot.get("billing_entitlements") if isinstance(db_snapshot, dict) else {}
    resolved_tier = tier_codes[0] if tier_codes else first_present([
        (pue or {}).get("tier_code") if isinstance(pue, dict) else None,
        (be or {}).get("tier_code") if isinstance(be, dict) else None,
    ])
    summary = {
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "email": auth.get("email"),
        "user_id": auth.get("user_id"),
        "resolved_tier_code": resolved_tier,
        "db_entitlements": db_snapshot,
        "feature_flags_for_tier": feature_flags,
        "balance_before": balance_before,
        "balance_after": balance_after,
        "service_results": results,
    }
    write_json(os.path.join(out_dir, "summary.json"), summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Face + Audio + Fusion Extension modes E2E using svc-fusion-extension as the only client-facing video API.")
    parser.add_argument("--email", default=os.getenv("DF_EMAIL", "user2@desifaces.ai"))
    parser.add_argument("--password", default=os.getenv("DF_PASSWORD", "password2"))
    parser.add_argument("--core-url", default=os.getenv("CORE_URL", "http://localhost:8000").rstrip("/"))
    parser.add_argument("--face-url", default=os.getenv("FACE_URL", "http://localhost:8003").rstrip("/"))
    parser.add_argument("--audio-url", default=os.getenv("AUDIO_URL", "http://localhost:8004").rstrip("/"))
    parser.add_argument("--audio-service-url", default=os.getenv("AUDIO_SERVICE_URL", os.getenv("SVC_AUDIO_URL", os.getenv("DF_AUDIO_URL", ""))).rstrip("/"))
    parser.add_argument("--existing-audio-url", default=os.getenv("EXISTING_AUDIO_URL", os.getenv("INPUT_AUDIO_URL", "")).strip())
    parser.add_argument("--existing-audio-artifact-id", default=os.getenv("EXISTING_AUDIO_ARTIFACT_ID", os.getenv("INPUT_AUDIO_ARTIFACT_ID", "")).strip())
    parser.add_argument("--video-url", default=os.getenv("FUSION_EXTENSION_URL", os.getenv("VIDEO_URL", "http://localhost:8006")).rstrip("/"))
    parser.add_argument("--pricing-url", default=os.getenv("PRICING_URL", "http://localhost:8009").rstrip("/"))
    parser.add_argument("--out-dir", default=os.getenv("OUT_DIR", f"/tmp/df_e2e_fusion_extension_modes_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"))
    parser.add_argument("--health-timeout-seconds", type=int, default=int(os.getenv("HEALTH_TIMEOUT_SECONDS", "180")))
    parser.add_argument("--job-timeout-seconds", type=int, default=int(os.getenv("JOB_TIMEOUT_SECONDS", "1200")))
    parser.add_argument("--poll-seconds", type=float, default=float(os.getenv("POLL_SECONDS", "4")))
    parser.add_argument("--db-container", default=os.getenv("DB_CONTAINER", ""))
    parser.add_argument("--db-lookup-first", action="store_true", default=os.getenv("DB_LOOKUP_FIRST", "1").strip().lower() not in {"0", "false", "no"})
    parser.add_argument("--skip-face", action="store_true")
    parser.add_argument("--skip-audio", action="store_true")
    parser.add_argument("--skip-video", action="store_true")

    parser.add_argument("--face-preview-path", default=os.getenv("FACE_PREVIEW_PATH", "/api/face/creator/pricing/preview"))
    parser.add_argument("--face-generate-path", default=os.getenv("FACE_GENERATE_PATH", "/api/face/creator/generate"))
    parser.add_argument("--face-status-path", default=os.getenv("FACE_STATUS_PATH", "/api/face/creator/jobs/{job_id}/status"))

    parser.add_argument("--audio-preview-path", default=os.getenv("AUDIO_PREVIEW_PATH", "/api/audio/tts/pricing/preview"))
    parser.add_argument("--audio-generate-path", default=os.getenv("AUDIO_GENERATE_PATH", "/api/audio/tts"))
    parser.add_argument("--audio-status-path", default=os.getenv("AUDIO_STATUS_PATH", "/api/audio/jobs/{job_id}/status"))

    parser.add_argument("--video-preview-path", default=os.getenv("FUSION_EXTENSION_PREVIEW_PATH", "/api/longform/pricing/preview"))
    parser.add_argument("--video-generate-path", default=os.getenv("FUSION_EXTENSION_GENERATE_PATH", "/api/longform/jobs"))
    parser.add_argument("--video-status-path", default=os.getenv("FUSION_EXTENSION_STATUS_PATH", "/api/longform/jobs/{job_id}"))

    parser.add_argument("--face-prompt", default=os.getenv("FACE_T2I_PROMPT", "attractive female from kerala and hailing from interior village of the state in village clothes, outdoor lighting. Therre are many people in the background working in the field."))

    
    parser.add_argument("--face-gender", default=os.getenv("FACE_GENDER", "female"))
    parser.add_argument("--face-num-variants", type=int, default=int(os.getenv("FACE_NUM_VARIANTS", "1")))
    parser.add_argument("--face-age-range-code", default=os.getenv("FACE_AGE_RANGE_CODE", "established_professional"))
    parser.add_argument("--face-skin-tone-code", default=os.getenv("FACE_SKIN_TONE_CODE", "medium_brown"))
    parser.add_argument("--face-region-code", default=os.getenv("FACE_REGION_CODE", "kerala"))
    parser.add_argument("--face-image-format-code", default=os.getenv("FACE_IMAGE_FORMAT_CODE", "instagram_portrait"))
    parser.add_argument("--face-use-case-code", default=os.getenv("FACE_USE_CASE_CODE", "brand_ambassador"))
    parser.add_argument("--face-style-code", default=os.getenv("FACE_STYLE_CODE", "professional"))
    parser.add_argument("--face-context-code", default=os.getenv("FACE_CONTEXT_CODE", "studio_headshot"))

    parser.add_argument("--audio-text", default=os.getenv("AUDIO_TEXT", "I didn’t expect AI to actually make me money. I used to spend hours creating content — recording, editing, fixing everything. Then I found DesiFaces.ai. Now I can create full videos — face, voice, everything — in minutes. I started posting more consistently, got my first paid collab. But honestly? The best part wasn’t the money. It was the time I got back. Now I use that time to learn, build, and invest in myself. It’s not just about creating content… it’s about what you do with the time it gives you. DesiFaces.ai — create, express, grow."))
    parser.add_argument("--audio-voice", default=os.getenv("AUDIO_VOICE", os.getenv("AUDIO_VOICE_ID", "")))
    parser.add_argument("--audio-locale", default=os.getenv("AUDIO_LOCALE", "hi-IN"))
    parser.add_argument("--audio-source-language", default=os.getenv("AUDIO_SOURCE_LANGUAGE", "en"))
    parser.add_argument("--audio-translate", action="store_true", default=os.getenv("AUDIO_TRANSLATE", "1").strip().lower() not in {"0", "false", "no"})
    parser.add_argument("--audio-style", default=os.getenv("AUDIO_STYLE", ""))
    parser.add_argument("--audio-style-degree", type=float, default=float(os.getenv("AUDIO_STYLE_DEGREE", "0") or 0))
    parser.add_argument("--audio-rate", type=float, default=float(os.getenv("AUDIO_RATE", "0") or 0))
    parser.add_argument("--audio-pitch", type=float, default=float(os.getenv("AUDIO_PITCH", "0") or 0))
    parser.add_argument("--audio-volume", type=float, default=float(os.getenv("AUDIO_VOLUME", "0") or 0))
    parser.add_argument("--audio-context", default=os.getenv("AUDIO_CONTEXT", ""))
    parser.add_argument("--audio-output-format", default=os.getenv("AUDIO_OUTPUT_FORMAT", "mp3"))

    parser.add_argument("--video-duration-sec", type=int, default=int(os.getenv("VIDEO_DURATION_SEC", os.getenv("FUSION_DURATION_SEC", "10"))))
    parser.add_argument("--video-provider", default=os.getenv("VIDEO_PROVIDER", os.getenv("FUSION_PROVIDER", "omnihuman_v15")).strip().lower() or "omnihuman_v15")
    parser.add_argument("--video-scenarios", default=os.getenv("VIDEO_SCENARIOS", "talking_video_economy,talking_video_premium,cinematic_fast,cinematic_premium"))
    parser.add_argument("--video-pricing-timeout-seconds", type=int, default=int(os.getenv("VIDEO_PRICING_TIMEOUT_SECONDS", "120")))
    parser.add_argument("--video-title", default=os.getenv("VIDEO_TITLE", "DesiFaces Fusion Studio E2E"))
    parser.add_argument("--video-goal", default=os.getenv("VIDEO_GOAL", "Create a short talking video using Face + Audio inputs through svc-fusion-extension."))
    parser.add_argument("--video-script-text", default=os.getenv("VIDEO_SCRIPT_TEXT", ""))
    parser.add_argument("--video-aspect-ratio", default=os.getenv("VIDEO_ASPECT_RATIO", "16:9"))
    parser.add_argument("--video-camera-angle", default=os.getenv("VIDEO_CAMERA_ANGLE", ""))
    parser.add_argument("--video-camera-framing", default=os.getenv("VIDEO_CAMERA_FRAMING", ""))
    parser.add_argument("--video-camera-motion-style", default=os.getenv("VIDEO_CAMERA_MOTION_STYLE", ""))
    return parser.parse_args()


def main() -> int:
    args = normalize_audio_args(parse_args())
    ensure_dir(args.out_dir)

    wait_for_service_health("svc-core", args.core_url, args.health_timeout_seconds)
    wait_for_service_health("svc-face", args.face_url, args.health_timeout_seconds)
    if not args.skip_audio:
        wait_for_service_health("svc-audio", args.audio_service_url, args.health_timeout_seconds)
    wait_for_service_health("svc-fusion-extension", args.video_url, args.health_timeout_seconds)
    wait_for_service_health("svc-pricing", args.pricing_url, args.health_timeout_seconds)

    print_step("Login")
    auth = login(args.core_url, args.email, args.password)
    write_json(os.path.join(args.out_dir, "auth.json"), auth["raw"])

    print_step("Resolve tier / billing / entitlements")
    container_name = discover_db_container(args.db_container)
    db_snapshot: Dict[str, Any] = {"available": False, "container_name": container_name}
    feature_flags: List[Dict[str, Any]] = []
    if container_name:
        try:
            db_snapshot = query_user_entitlements(container_name, auth["user_id"])
            tier_code = first_present([
                (db_snapshot.get("pricing_user_entitlements") or {}).get("tier_code") if isinstance(db_snapshot.get("pricing_user_entitlements"), dict) else None,
                (db_snapshot.get("billing_entitlements") or {}).get("tier_code") if isinstance(db_snapshot.get("billing_entitlements"), dict) else None,
            ]) or ""
            feature_flags = query_feature_flags(container_name, tier_code)
        except Exception as ex:
            db_snapshot = {"available": False, "container_name": container_name, "error": str(ex)}
    write_json(os.path.join(args.out_dir, "db_entitlements.json"), db_snapshot)
    write_json(os.path.join(args.out_dir, "feature_flags_for_tier.json"), feature_flags)

    balance_before = get_balance(args.pricing_url, auth["access_token"], auth["user_id"])
    write_json(os.path.join(args.out_dir, "balance_before.json"), balance_before)

    results: List[Dict[str, Any]] = []
    face_result: Optional[Dict[str, Any]] = None
    audio_result: Optional[Dict[str, Any]] = None

    db_face, db_face_meta = maybe_lookup_db_face(auth["user_id"], args.db_lookup_first, container_name)
    write_json(os.path.join(args.out_dir, "db_face_lookup.json"), db_face_meta)

    if not args.skip_face:
        print_step("Run Face")
        auth, face_result = run_face(args, auth, args.out_dir, container_name)
        results.append(face_result)

    if not args.skip_audio:
        print_step("Run Audio")
        auth, audio_result = run_audio(args, auth, args.out_dir, container_name)
        results.append(audio_result)

    if not args.skip_video:
        face_artifact_id = None
        face_image_url = None
        audio_artifact_id = first_present([args.existing_audio_artifact_id]) or None
        audio_url = first_present([args.existing_audio_url]) or None
        if face_result and face_result.get("artifact_ids"):
            face_artifact_id = face_result["artifact_ids"][0]
        if face_result and face_result.get("face_image_url"):
            face_image_url = face_result.get("face_image_url")
        elif db_face and db_face.get("face_image_url"):
            face_image_url = db_face.get("face_image_url")
        elif db_face and db_face.get("face_artifact_id") and not face_artifact_id:
            face_artifact_id = db_face["face_artifact_id"]

        if audio_result and audio_result.get("artifact_ids"):
            audio_artifact_id = audio_result["artifact_ids"][0]
        if audio_result and audio_result.get("audio_url"):
            audio_url = audio_result.get("audio_url")

        for scenario in build_video_scenarios(args):
            print_step(f"Run Fusion Extension [{scenario.get('name')}]")
            auth, fusion_result = run_fusion_extension(
                args,
                auth,
                args.out_dir,
                container_name,
                scenario,
                face_artifact_id,
                audio_artifact_id,
                face_image_url=face_image_url,
                audio_url=audio_url,
            )
            results.append(fusion_result)

    balance_after = get_balance(args.pricing_url, auth["access_token"], auth["user_id"])
    write_json(os.path.join(args.out_dir, "balance_after.json"), balance_after)

    final_summary = summarize_user(args.out_dir, auth, db_snapshot, feature_flags, balance_before, balance_after, results)
    print(json.dumps(final_summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as ex:
        out_dir = os.getenv("OUT_DIR", "/tmp/df_e2e_fusion_extension_modes_failed")
        ensure_dir(out_dir)
        failure = {
            "out_dir": out_dir,
            "error": str(ex),
            "completed_at": dt.datetime.utcnow().isoformat() + "Z",
        }
        write_json(os.path.join(out_dir, "summary.json"), failure)
        print(json.dumps(failure, indent=2, ensure_ascii=False), file=sys.stderr)
        raise
