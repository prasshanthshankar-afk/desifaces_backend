
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pricing_response_validator import assert_pricing_contract, summarize_pricing


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    base_url_env: str
    preview_path: str
    generate_path: str
    status_path_template: str
    preview_payload: Dict[str, Any]
    generate_payload: Dict[str, Any]
    timeout_seconds: int = 900
    poll_seconds: int = 3


def _read_json(resp) -> Dict[str, Any]:
    body = resp.read().decode("utf-8")
    if not body.strip():
        return {}
    return json.loads(body)


def _request_json(method: str, url: str, payload: Optional[Dict[str, Any]], headers: Dict[str, str]) -> Dict[str, Any]:
    data = None
    req_headers = {"accept": "application/json", **headers}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers["content-type"] = "application/json"
    req = Request(url, data=data, headers=req_headers, method=method.upper())
    try:
        with urlopen(req) as resp:
            return _read_json(resp)
    except HTTPError as e:
        try:
            detail = _read_json(e)
        except Exception:
            detail = {"raw": e.read().decode("utf-8", errors="replace")}
        raise RuntimeError(f"{method} {url} failed [{e.code}]: {json.dumps(detail)}") from e


def _headers_from_env() -> Dict[str, str]:
    headers: Dict[str, str] = {}
    token = os.getenv("ACCESS_TOKEN", "").strip()
    user_id = os.getenv("X_USER_ID", "").strip()
    if token:
        headers["authorization"] = f"Bearer {token}"
    if user_id:
        headers["x-user-id"] = user_id
    return headers


def _job_id_from_payload(payload: Dict[str, Any]) -> str:
    for key in ("job_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    job = payload.get("job")
    if isinstance(job, dict):
        for key in ("job_id", "id"):
            value = job.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise RuntimeError(f"Could not determine job id from payload: {json.dumps(payload)[:2000]}")


def run_service_pricing_e2e(config: ServiceConfig) -> Dict[str, Any]:
    base_url = os.getenv(config.base_url_env, "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError(f"Missing required base URL env: {config.base_url_env}")

    headers = _headers_from_env()
    out_dir = Path(os.getenv("OUT_DIR", f"/tmp/df_e2e_pricing_{config.name.lower()}"))
    out_dir.mkdir(parents=True, exist_ok=True)

    preview_url = f"{base_url}{config.preview_path}"
    generate_url = f"{base_url}{config.generate_path}"

    preview_payload = json.loads(json.dumps(config.preview_payload))
    generate_payload = json.loads(json.dumps(config.generate_payload))

    preview_resp = _request_json("POST", preview_url, preview_payload, headers)
    (out_dir / f"{config.name.lower()}_preview.json").write_text(json.dumps(preview_resp, indent=2), encoding="utf-8")
    assert_pricing_contract(preview_resp, required=True, require_summary=True)

    generate_resp = _request_json("POST", generate_url, generate_payload, headers)
    (out_dir / f"{config.name.lower()}_generate.json").write_text(json.dumps(generate_resp, indent=2), encoding="utf-8")
    generate_normalized = assert_pricing_contract(generate_resp, required=False, require_summary=False)

    job_id = _job_id_from_payload(generate_resp)
    status_url = f"{base_url}{config.status_path_template.format(job_id=job_id)}"

    deadline = time.time() + int(os.getenv("TIMEOUT_SECONDS", str(config.timeout_seconds)))
    last_status_payload: Dict[str, Any] = {}
    while time.time() < deadline:
        status_resp = _request_json("GET", status_url, None, headers)
        last_status_payload = status_resp
        (out_dir / f"{config.name.lower()}_status_last.json").write_text(json.dumps(status_resp, indent=2), encoding="utf-8")
        normalized = assert_pricing_contract(status_resp, required=True, require_summary=True)
        state = str(normalized["pricing"].get("state") or "")
        job_status = ""
        if isinstance(status_resp.get("status"), str):
            job_status = status_resp["status"]
        elif isinstance(status_resp.get("job"), dict):
            job_status = str(status_resp["job"].get("status") or "")
        if state in {"committed", "released", "reservation_failed", "commit_failed", "release_failed"} or job_status in {"succeeded", "failed", "canceled"}:
            break
        time.sleep(int(os.getenv("POLL_SECONDS", str(config.poll_seconds))))

    final_summary = summarize_pricing(last_status_payload)
    (out_dir / f"{config.name.lower()}_pricing_summary.json").write_text(json.dumps(final_summary, indent=2), encoding="utf-8")

    result = {
        "service": config.name,
        "job_id": job_id,
        "preview": summarize_pricing(preview_resp),
        "generate": {
            "job_id": generate_normalized.get("job_id"),
            "pricing": generate_normalized.get("pricing"),
            "pricing_summary": generate_normalized.get("pricing_summary"),
        },
        "final": final_summary,
        "out_dir": str(out_dir),
    }
    (out_dir / f"{config.name.lower()}_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> int:
    raise SystemExit("Import and call run_service_pricing_e2e(...) from a service-specific runner.")


if __name__ == "__main__":
    sys.exit(main())
