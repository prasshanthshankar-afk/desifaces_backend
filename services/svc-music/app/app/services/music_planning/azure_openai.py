from __future__ import annotations

import os
import json
from typing import Any, Dict, List

import httpx


def _env(k: str, default: str | None = None) -> str:
    v = os.getenv(k, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"missing_env:{k}")
    return str(v)


def _endpoint_base() -> str:
    return _env("AZURE_OPENAI_ENDPOINT").rstrip("/")


def _api_version() -> str:
    return os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01")


def _key() -> str:
    return _env("AZURE_OPENAI_KEY")


async def azure_embed_texts(texts: List[str]) -> List[List[float]]:
    deployment = _env("AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT")
    url = f"{_endpoint_base()}/openai/deployments/{deployment}/embeddings"
    params = {"api-version": _api_version()}

    payload = {"input": texts}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            url, params=params, headers={"api-key": _key(), "Content-Type": "application/json"}, json=payload
        )
        r.raise_for_status()
        data = r.json()

    return [item["embedding"] for item in data.get("data", [])]


async def azure_chat_json(system: str, user: str, *, temperature: float = 0.4, max_tokens: int = 1200) -> Dict[str, Any]:
    deployment = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT") or os.getenv("AZURE_OPENAI_DEPLOYMENT")
    if not deployment:
        raise RuntimeError("missing_env:AZURE_OPENAI_CHAT_DEPLOYMENT (or AZURE_OPENAI_DEPLOYMENT)")

    url = f"{_endpoint_base()}/openai/deployments/{deployment}/chat/completions"
    params = {"api-version": _api_version()}

    payload: Dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }

    async with httpx.AsyncClient(timeout=90.0) as client:
        r = await client.post(
            url, params=params, headers={"api-key": _key(), "Content-Type": "application/json"}, json=payload
        )
        r.raise_for_status()
        out = r.json()

    content = (
        (((out.get("choices") or [{}])[0]).get("message") or {}).get("content")
        or ""
    ).strip()

    # best-effort strict JSON parse
    try:
        return json.loads(content)
    except Exception:
        return {"version": 1, "raw_text": content, "parse_error": True}