#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

HEALTH_PATHS = ["/api/health", "/health"]
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "canceled", "cancelled", "completed", "success", "done"}
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
    headers = {
        "Authorization": normalize_bearer(access_token),
        "X-User-Id": str(user_id),
    }
    country_code = (os.getenv("X_COUNTRY_CODE") or os.getenv("DF_COUNTRY_CODE") or "").strip().upper()
    if country_code:
        headers["X-Country-Code"] = country_code
    return headers


def lower_ascii(text: str) -> str:
    return (text or "").lower().strip()

def is_uuid_like(value: Any) -> bool:
    """Return True only for real UUID strings/objects, never URLs."""
    if value is None:
        return False
    try:
        import uuid
        uuid.UUID(str(value).strip())
        return True
    except Exception:
        return False


def uuid_or_none(value: Any) -> Optional[str]:
    """Normalize a UUID-like value to string, otherwise None."""
    if not is_uuid_like(value):
        return None
    return str(value).strip()



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
    """Extract the best Face handoff values from svc-face status.

    svc-face currently exposes generated image assets mostly under variants_state:
      - image_url: signed preview URL
      - media_asset_id: UUID suitable for downstream artifact/asset handoff
      - face_profile_id: profile UUID

    IMPORTANT: never treat an image URL as face_artifact_id. Longform persists
    face_artifact_id into a UUID column.
    """
    face_image_url = first_present([
        recursive_collect_first_str(payload, {"image_url"}),
        recursive_collect_first_str(payload, {"face_image_url", "preview_url", "signed_url", "url", "sas_url", "blob_url", "storage_ref"}),
    ])

    media_asset_id = first_present([
        recursive_collect_first_str(payload, {"media_asset_id"}),
        recursive_collect_first_str(payload, {"image_asset_id"}),
    ])
    face_profile_id = recursive_collect_first_str(payload, {"face_profile_id"})

    artifact_id = recursive_collect_first_str(payload, {"face_artifact_id", "artifact_id", "selected_face_artifact_id"})
    if not uuid_or_none(artifact_id):
        artifact_id = None

    return {
        "source": "generated",
        "source_job_id": job_id,
        "face_artifact_id": uuid_or_none(artifact_id),
        "face_media_asset_id": uuid_or_none(media_asset_id),
        "face_profile_id": uuid_or_none(face_profile_id),
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
    """Extract a reusable face asset from recent Face studio rows.

    Prefer UUIDs from variants_state.media_asset_id over URL fallbacks.
    """
    id_keys = {"face_artifact_id", "artifact_id", "image_artifact_id", "selected_face_artifact_id"}
    media_id_keys = {"media_asset_id", "image_asset_id"}
    profile_id_keys = {"face_profile_id"}
    url_keys = {"face_image_url", "image_url", "preview_url", "signed_url", "url", "storage_ref", "sas_url", "blob_url"}

    for row in rows:
        artifact_id = recursive_collect_first_str(row, id_keys)
        media_asset_id = recursive_collect_first_str(row, media_id_keys)
        face_profile_id = recursive_collect_first_str(row, profile_id_keys)
        image_url = recursive_collect_first_str(row, url_keys)

        artifact_id = uuid_or_none(artifact_id)
        media_asset_id = uuid_or_none(media_asset_id)
        face_profile_id = uuid_or_none(face_profile_id)

        if artifact_id or media_asset_id or image_url:
            return {
                "source": "database",
                "source_job_id": str(row.get("id") or ""),
                "face_artifact_id": artifact_id,
                "face_media_asset_id": media_asset_id,
                "face_profile_id": face_profile_id,
                "face_image_url": image_url,
            }
    return {
        "source": None,
        "source_job_id": None,
        "face_artifact_id": None,
        "face_media_asset_id": None,
        "face_profile_id": None,
        "face_image_url": None,
    }


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


def build_fusion_payload(
    args: argparse.Namespace,
    face_artifact_id: Optional[str],
    audio_artifact_id: Optional[str],
    face_image_url: Optional[str] = None,
    audio_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a svc-fusion-extension longform talking-video request.

    This intentionally sends both durable UUIDs and direct URL aliases when
    available. Durable UUIDs are used for DB fields; URL aliases help workers
    and provider adapters resolve media without additional lookups.

    The pricing architecture is:
      - parent svc-fusion-extension job is billable
      - child svc-fusion render jobs must be internal/pricing-suppressed

    The child-pricing suppression fields below are harmless hints unless the
    backend/worker honors them; they make the desired contract explicit in the
    persisted request and debug payload.
    """
    script_text = (args.audio_text or "").strip()
    if not script_text:
        script_text = "Create a short DesiFaces talking video with natural expression and clear delivery."

    requested_sec = max(1, int(args.fusion_duration_sec or 10))
    if requested_sec <= 10:
        pricing_variant_code = "TALKING_VIDEO_ECONOMY_10S"
        bucket_max_sec = 10
    elif requested_sec <= 20:
        pricing_variant_code = "TALKING_VIDEO_ECONOMY_20S"
        bucket_max_sec = 20
    else:
        pricing_variant_code = "TALKING_VIDEO_ECONOMY_30S"
        bucket_max_sec = 30

    provider = (args.fusion_provider or "veed_fabric").strip().lower() or "veed_fabric"
    country_code = (
        os.getenv("X_COUNTRY_CODE")
        or os.getenv("DF_COUNTRY_CODE")
        or os.getenv("COUNTRY_CODE")
        or ""
    ).strip().upper()

    normalized_face_id = uuid_or_none(face_artifact_id)
    normalized_audio_id = uuid_or_none(audio_artifact_id)

    payload: Dict[str, Any] = {
        # Required by svc-fusion-extension legacy longform mode.
        "script_text": script_text,
        "script": script_text,

        # Product routing. Keep multiple aliases because longform code has evolved.
        "mode": "talking_video",
        "video_mode": "talking_video",
        "longform_profile": "talking_video",
        "scenario_name": "talking_video_economy",
        "selected_mode": "talking_video_economy",
        "quality_tier": "economy",
        "output_profile": "economy",
        "background_mode": "fixed",

        # Audio-driven talking video.
        "voice_mode": "audio",

        # Duration aliases. Note: longform may still rebucket by actual audio/script
        # duration; these fields express the requested cap/intent.
        "duration_sec": requested_sec,
        "requested_duration_sec": requested_sec,
        "segment_seconds": requested_sec,
        "max_segment_seconds": requested_sec,
        "video": {
            "duration_sec": requested_sec,
            "requested_duration_sec": requested_sec,
            "aspect_ratio": "9:16",
            "resolution": "720p",
        },

        "consent": {
            "external_provider_ok": True,
            "user_consent": True,
            "provider_consent": True,
        },
        "external_provider_ok": True,

        # Force low-cost launch provider path unless caller overrides.
        "provider": provider,
        "provider_hint": provider,
        "fusion_provider": provider,
        "execution_provider_family": "veed_fabric",
        "provider_options": {
            "provider_hint": provider,
            "fusion_provider": provider,
            "presenter_provider": provider,
            "execution_provider_family": "veed_fabric",
            "quality_tier": "economy",
            "output_profile": "economy",
            "background_mode": "fixed",
            "resolution": "720p",

            # Contract for the worker: parent longform is billable; child
            # svc-fusion jobs must not perform a second reservation.
            "child_pricing_suppressed": True,
            "suppress_child_pricing": True,
            "child_billing_mode": "internal",
        },

        # Parent pricing is intentionally enabled. Do not set top-level
        # pricing.enabled=false here, because that would suppress the parent
        # billable longform job. These child_* fields are scoped to children.
        "pricing_variant_code": pricing_variant_code,
        "pricing_context": {
            "pricing_variant_code": pricing_variant_code,
            "requested_duration_sec": requested_sec,
            "bucket_max_sec": bucket_max_sec,
            "country_code": country_code or None,
            "parent_billable": True,
            "child_pricing_suppressed": True,
            "child_billing_mode": "internal",
        },
        "child_job_options": {
            "pricing_suppressed": True,
            "suppress_pricing": True,
            "internal_job": True,
            "billing_mode": "internal",
            "reason": "child_job_of_billable_longform_parent",
        },
        "client_context": {
            "country_code": country_code or None,
            "channel": os.getenv("DF_CHANNEL", "web"),
            "source": "df_e2e_pricing_user_flow",
        },
        "country_code": country_code or None,

        "tags": {
            "source": "df_e2e_pricing_user_flow",
            "mode": "talking_video",
            "scenario_name": "talking_video_economy",
            "selected_mode": "talking_video_economy",
            "quality_tier": "economy",
            "output_profile": "economy",
            "provider_family": "veed_fabric",
            "provider_hint": provider,
            "execution_provider_family": "veed_fabric",
            "pricing_variant_code": pricing_variant_code,
            "country_code": country_code or None,

            # Same child-pricing contract, repeated in tags so it survives
            # services that preserve tags but reshape provider_options.
            "child_pricing_suppressed": True,
            "suppress_child_pricing": True,
            "child_billing_mode": "internal",
        },
    }

    # Prefer UUID handoff. Also include direct URL aliases when available for
    # provider/worker media resolution. Backend must keep image_ref/face_image_url
    # in JSON payload fields, never in UUID columns.
    if normalized_face_id:
        payload["face_artifact_id"] = normalized_face_id
        if face_image_url:
            payload["image_ref"] = face_image_url
            payload["face_image_url"] = face_image_url
    elif face_image_url:
        payload["image_ref"] = face_image_url
        payload["face_image_url"] = face_image_url

    # Send both nested and top-level audio aliases for longform compatibility.
    # This prevents the longform worker from losing the generated audio URL even
    # when it also has a UUID artifact ID.
    if normalized_audio_id:
        payload["audio_artifact_id"] = normalized_audio_id
        payload["voice_audio_artifact_id"] = normalized_audio_id
        payload["voice_audio"] = {
            "audio_artifact_id": normalized_audio_id,
            "artifact_id": normalized_audio_id,
        }
        if audio_url:
            payload["audio_ref"] = audio_url
            payload["audio_url"] = audio_url
            payload["voice_audio_url"] = audio_url
            payload["voice_audio"]["audio_url"] = audio_url
            payload["voice_audio"]["url"] = audio_url
    elif audio_url:
        payload["audio_ref"] = audio_url
        payload["audio_url"] = audio_url
        payload["voice_audio_url"] = audio_url
        payload["voice_audio"] = {
            "audio_url": audio_url,
            "url": audio_url,
        }

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
            f"{args.audio_url}{args.audio_preview_path}",
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
        f"{args.audio_url}{args.audio_generate_path}",
        headers=common_headers(auth["access_token"], auth["user_id"]),
        payload=build_audio_payload(args, preview_resp),
        timeout=90,
    )
    return resp if isinstance(resp, dict) else {"raw": resp}


def try_fusion_preview(args: argparse.Namespace, auth: Dict[str, Any], face_artifact_id: Optional[str], audio_artifact_id: Optional[str], face_image_url: Optional[str] = None, audio_url: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    paths = [p for p in [
        args.fusion_preview_path,
        "/api/longform/jobs/pricing/preview",
        "/api/longform/pricing/preview",
        "/jobs/pricing/preview",
        "/api/fusion/jobs/pricing/preview",
        "/api/fusion/pricing/preview",
    ] if p]
    seen = set()
    payload = build_fusion_payload(args, face_artifact_id, audio_artifact_id, face_image_url=face_image_url, audio_url=audio_url)
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        try:
            _, _, resp = http_json(
                "POST",
                f"{args.fusion_url}{p}",
                headers=common_headers(auth["access_token"], auth["user_id"]),
                payload=payload,
                timeout=60,
            )
            return (resp if isinstance(resp, dict) else {"raw": resp}), None
        except Exception as ex:
            msg = str(ex)
            if "[404]" in msg or '"detail":"Not Found"' in msg:
                continue
            raise
    return None, "preview_unsupported_404"


def try_fusion_generate(args: argparse.Namespace, auth: Dict[str, Any], face_artifact_id: Optional[str], audio_artifact_id: Optional[str], face_image_url: Optional[str] = None, audio_url: Optional[str] = None) -> Dict[str, Any]:
    _, _, resp = http_json(
        "POST",
        f"{args.fusion_url}{args.fusion_generate_path}",
        headers=common_headers(auth["access_token"], auth["user_id"]),
        payload=build_fusion_payload(args, face_artifact_id, audio_artifact_id, face_image_url=face_image_url, audio_url=audio_url),
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
        # Prefer UUIDs for Fusion handoff. svc-face usually exposes media_asset_id
        # in variants_state rather than a canonical face_artifact_id.
        media_asset_id = uuid_or_none(face_asset.get("face_media_asset_id"))
        if not artifact_ids and media_asset_id:
            artifact_ids = [media_asset_id]

        result["artifact_ids"] = artifact_ids
        result["face_image_url"] = face_asset.get("face_image_url")
        result["face_media_asset_id"] = media_asset_id
        result["face_profile_id"] = face_asset.get("face_profile_id")

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

    if not args.audio_voice:
        result["blocked_or_failed_reason"] = "missing_audio_voice"
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

        auth, status_url, final_status = poll_job(args, auth, args.audio_url, args.audio_status_path, job_id, os.path.join(svc_dir, "status_last.json"))
        result["generated"] = True
        result["status_url"] = status_url
        result["job_status"] = infer_job_status(final_status)
        result["final_pricing"] = pricing_brief(final_status)
        result["pricing_summary"] = get_pricing_summary(final_status)
        result["error_code"] = final_status.get("error_code")
        result["error_message"] = final_status.get("error_message")
        result["stage"] = final_status.get("stage")
        result["provider_hint"] = final_status.get("provider_hint")
        result["quality_tier"] = final_status.get("quality_tier")
        result["longform_profile"] = final_status.get("longform_profile")

        # Extract child svc-fusion job id from longform error messages when present.
        err_text = str(final_status.get("error_message") or "")
        m = re.search(r"svc-fusion job ([0-9a-fA-F-]{36})", err_text)
        if m:
            result["child_fusion_job_id"] = m.group(1)

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


def run_fusion(args: argparse.Namespace, auth: Dict[str, Any], out_dir: str, container_name: Optional[str], face_artifact_id: Optional[str], audio_artifact_id: Optional[str], face_image_url: Optional[str] = None, audio_url: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    svc_dir = ensure_dir(os.path.join(out_dir, "fusion"))
    result: Dict[str, Any] = {
        "service": "fusion",
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

    result["handoff"] = {
        "face_artifact_id": face_artifact_id,
        "has_face_image_url": bool(face_image_url),
        "audio_artifact_id": audio_artifact_id,
        "has_audio_url": bool(audio_url),
    }

    if not (face_artifact_id or face_image_url) or not (audio_artifact_id or audio_url):
        result["blocked_or_failed_reason"] = "fusion_generation_skipped_missing_face_or_audio_artifact"
        write_json(os.path.join(svc_dir, "result.json"), result)
        return auth, result

    try:
        request_payload = build_fusion_payload(
            args,
            face_artifact_id,
            audio_artifact_id,
            face_image_url=face_image_url,
            audio_url=audio_url,
        )
        write_json(os.path.join(svc_dir, "request_payload.json"), request_payload)

        preview_resp, preview_note = try_fusion_preview(args, auth, face_artifact_id, audio_artifact_id, face_image_url=face_image_url, audio_url=audio_url)
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

        generate_resp = try_fusion_generate(args, auth, face_artifact_id, audio_artifact_id, face_image_url=face_image_url, audio_url=audio_url)
        write_json(os.path.join(svc_dir, "generate.json"), generate_resp)
        result["generate_pricing"] = pricing_brief(generate_resp)

        job_id = maybe_job_id(generate_resp)
        result["job_id"] = job_id
        if not job_id:
            result["blocked_or_failed_reason"] = "missing_job_id_in_generate_response"
            write_json(os.path.join(svc_dir, "result.json"), result)
            return auth, result

        auth, status_url, final_status = poll_job(args, auth, args.fusion_url, args.fusion_status_path, job_id, os.path.join(svc_dir, "status_last.json"))
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
            result["blocked_or_failed_reason"] = first_present([
                final_status.get("error_message"),
                final_status.get("error_code"),
                get_pricing(final_status).get("reason"),
                result["job_status"],
            ])
        write_json(os.path.join(svc_dir, "result.json"), result)
        return auth, result

    except Exception as ex:
        result["blocked_or_failed_reason"] = str(ex)
        write_json(os.path.join(svc_dir, "result.json"), result)
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
    parser = argparse.ArgumentParser(description="Unified Face/Audio/Fusion pricing E2E using confirmed routes and schemas.")
    parser.add_argument("--email", default=os.getenv("DF_EMAIL", "user1@desifaces.ai"))
    parser.add_argument("--password", default=os.getenv("DF_PASSWORD", "password1"))
    parser.add_argument("--core-url", default=os.getenv("CORE_URL", "http://localhost:8000").rstrip("/"))
    parser.add_argument("--face-url", default=os.getenv("FACE_URL", "http://localhost:8003").rstrip("/"))
    parser.add_argument("--audio-url", default=os.getenv("AUDIO_URL", "http://localhost:8004").rstrip("/"))
    parser.add_argument("--fusion-url", default=os.getenv("FUSION_URL", "http://localhost:8006").rstrip("/"))
    parser.add_argument("--pricing-url", default=os.getenv("PRICING_URL", "http://localhost:8009").rstrip("/"))
    parser.add_argument("--out-dir", default=os.getenv("OUT_DIR", f"/tmp/df_e2e_pricing_user_flow_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"))
    parser.add_argument("--health-timeout-seconds", type=int, default=int(os.getenv("HEALTH_TIMEOUT_SECONDS", "180")))
    parser.add_argument("--job-timeout-seconds", type=int, default=int(os.getenv("JOB_TIMEOUT_SECONDS", "1200")))
    parser.add_argument("--poll-seconds", type=float, default=float(os.getenv("POLL_SECONDS", "4")))
    parser.add_argument("--db-container", default=os.getenv("DB_CONTAINER", ""))
    parser.add_argument("--db-lookup-first", action="store_true", default=os.getenv("DB_LOOKUP_FIRST", "1").strip().lower() not in {"0", "false", "no"})
    parser.add_argument("--skip-face", action="store_true")
    parser.add_argument("--skip-audio", action="store_true")
    parser.add_argument("--skip-fusion", action="store_true")

    parser.add_argument("--face-preview-path", default=os.getenv("FACE_PREVIEW_PATH", "/api/face/creator/pricing/preview"))
    parser.add_argument("--face-generate-path", default=os.getenv("FACE_GENERATE_PATH", "/api/face/creator/generate"))
    parser.add_argument("--face-status-path", default=os.getenv("FACE_STATUS_PATH", "/api/face/creator/jobs/{job_id}/status"))

    parser.add_argument("--audio-preview-path", default=os.getenv("AUDIO_PREVIEW_PATH", "/api/audio/tts/pricing/preview"))
    parser.add_argument("--audio-generate-path", default=os.getenv("AUDIO_GENERATE_PATH", "/api/audio/tts"))
    parser.add_argument("--audio-status-path", default=os.getenv("AUDIO_STATUS_PATH", "/api/audio/jobs/{job_id}/status"))

    parser.add_argument("--fusion-preview-path", default=os.getenv("FUSION_PREVIEW_PATH", ""))
    parser.add_argument("--fusion-generate-path", default=os.getenv("FUSION_GENERATE_PATH", "/api/longform/jobs"))
    parser.add_argument("--fusion-status-path", default=os.getenv("FUSION_STATUS_PATH", "/api/longform/jobs/{job_id}"))

    parser.add_argument("--face-prompt", default=os.getenv("FACE_T2I_PROMPT", "attractive mizoram female in village clothes, outdoor lighting, sharp focus, beautiful, detailed, realistic"))
    parser.add_argument("--face-gender", default=os.getenv("FACE_GENDER", "female"))
    parser.add_argument("--face-num-variants", type=int, default=int(os.getenv("FACE_NUM_VARIANTS", "2")))
    parser.add_argument("--face-age-range-code", default=os.getenv("FACE_AGE_RANGE_CODE", "established_professional"))
    parser.add_argument("--face-skin-tone-code", default=os.getenv("FACE_SKIN_TONE_CODE", "medium_brown"))
    parser.add_argument("--face-region-code", default=os.getenv("FACE_REGION_CODE", "kerala"))
    parser.add_argument("--face-image-format-code", default=os.getenv("FACE_IMAGE_FORMAT_CODE", "instagram_portrait"))
    parser.add_argument("--face-use-case-code", default=os.getenv("FACE_USE_CASE_CODE", "brand_ambassador"))
    parser.add_argument("--face-style-code", default=os.getenv("FACE_STYLE_CODE", "professional"))
    parser.add_argument("--face-context-code", default=os.getenv("FACE_CONTEXT_CODE", "studio_headshot"))

    parser.add_argument("--audio-text", default=os.getenv("AUDIO_TEXT", "Escape to the Land of the Highlanders. Experience serene blue mountains, lush valleys, and the tranquil charm of Aizawl. Mizoram—where peace pays. Plan your getaway today"))
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

    parser.add_argument("--fusion-duration-sec", type=int, default=int(os.getenv("FUSION_DURATION_SEC", "10")))
    parser.add_argument("--fusion-provider", default=os.getenv("FUSION_PROVIDER", "veed_fabric").strip().lower() or "veed_fabric")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dir(args.out_dir)

    wait_for_service_health("svc-core", args.core_url, args.health_timeout_seconds)
    wait_for_service_health("svc-face", args.face_url, args.health_timeout_seconds)
    wait_for_service_health("svc-audio", args.audio_url, args.health_timeout_seconds)
    wait_for_service_health("svc-fusion", args.fusion_url, args.health_timeout_seconds)
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

    if not args.skip_fusion:
        print_step("Run Fusion")
        face_artifact_id = None
        face_image_url = None
        audio_artifact_id = None
        audio_url = None
        if face_result and face_result.get("artifact_ids"):
            candidate_face_id = face_result["artifact_ids"][0]
            if uuid_or_none(candidate_face_id):
                face_artifact_id = uuid_or_none(candidate_face_id)

        if not face_artifact_id and face_result and uuid_or_none(face_result.get("face_media_asset_id")):
            face_artifact_id = uuid_or_none(face_result.get("face_media_asset_id"))

        if not face_artifact_id and db_face and uuid_or_none(db_face.get("face_artifact_id")):
            face_artifact_id = uuid_or_none(db_face.get("face_artifact_id"))

        if not face_artifact_id and db_face and uuid_or_none(db_face.get("face_media_asset_id")):
            face_artifact_id = uuid_or_none(db_face.get("face_media_asset_id"))

        # Only fall back to URL/image_ref when no UUID exists.
        if not face_artifact_id:
            if face_result and face_result.get("face_image_url"):
                face_image_url = face_result.get("face_image_url")
            elif db_face and db_face.get("face_image_url"):
                face_image_url = db_face.get("face_image_url")

        if audio_result and audio_result.get("artifact_ids"):
            audio_artifact_id = audio_result["artifact_ids"][0]
        if audio_result and audio_result.get("audio_url"):
            audio_url = audio_result.get("audio_url")

        auth, fusion_result = run_fusion(
            args,
            auth,
            args.out_dir,
            container_name,
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
        out_dir = os.getenv("OUT_DIR", "/tmp/df_e2e_pricing_user_flow_failed")
        ensure_dir(out_dir)
        failure = {
            "out_dir": out_dir,
            "error": str(ex),
            "completed_at": dt.datetime.utcnow().isoformat() + "Z",
        }
        write_json(os.path.join(out_dir, "summary.json"), failure)
        print(json.dumps(failure, indent=2, ensure_ascii=False), file=sys.stderr)
        raise
