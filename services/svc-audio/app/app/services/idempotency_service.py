from __future__ import annotations

import hashlib
import json
from typing import Any, Dict


def _canonicalize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _canonicalize(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [_canonicalize(x) for x in obj]
    return obj


def request_hash(stable_spec: Dict[str, Any]) -> str:
    canon = _canonicalize(stable_spec)
    raw = json.dumps(canon, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def provider_idempotency_key(provider: str, payload_version: str, req_hash: str) -> str:
    return f"{provider}:{payload_version}:{req_hash}"