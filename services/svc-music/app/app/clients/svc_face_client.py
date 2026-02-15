from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Dict, Optional, List, Tuple, Set

import httpx

from app.config import settings


@dataclass
class FaceJobResult:
    job_id: str
    status: str
    image_url: Optional[str] = None
    variants: Optional[List[Dict[str, Any]]] = None
    raw: Optional[Dict[str, Any]] = None


class SvcFaceClient:
    """
    Minimal svc-music -> svc-face client.
    We DO NOT require any svc-face changes.

    Endpoints used (svc-face):
      - POST /api/face/creator/generate
      - GET  /api/face/creator/jobs/{job_id}/status

    Auth:
      - Primary: per-request user bearer token (from incoming API request)
      - Fallback: settings.SVC_FACE_BEARER_TOKEN (service-to-service) for worker-mode
        NOTE: we access it via getattr(settings, "SVC_FACE_BEARER_TOKEN", None) so config.py
              does NOT need to define it to avoid AttributeError.
    """

    def __init__(self, base_url: str):
        self.base_url = str(base_url or "").rstrip("/")
        if not self.base_url:
            raise RuntimeError("SvcFaceClient base_url is empty")

    # -----------------------------
    # Public API
    # -----------------------------
    async def create_creator_face_job(
        self,
        *,
        bearer_token: Optional[str],
        payload: Dict[str, Any],
        timeout_s: float = 60.0,
        # NOTE: POST retries can cause duplicates if svc-face doesn't dedupe.
        # Keep default 0; caller can bump if they use request_nonce in payload.
        retries: int = 0,
    ) -> str:
        url = f"{self.base_url}/api/face/creator/generate"
        headers = {
            "Authorization": self._auth_header_value(bearer_token),
            "Content-Type": "application/json",
        }

        async with self._client(timeout_s=timeout_s) as client:
            j = await self._request_json(
                client=client,
                method="POST",
                url=url,
                headers=headers,
                json=payload,
                timeout_s=timeout_s,
                retries=retries,
                retry_on_status={502, 503, 504},  # keep narrow for POST
            )

        job_id = str(j.get("job_id") or "").strip()
        if not job_id:
            # keep error short but useful
            raise RuntimeError(f"svc-face returned no job_id. response_keys={list(j.keys())}")
        return job_id

    async def get_creator_face_status(
        self,
        *,
        bearer_token: Optional[str],
        job_id: str,
        timeout_s: float = 30.0,
        retries: int = 3,
    ) -> Dict[str, Any]:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise RuntimeError("missing job_id for svc-face status request")

        url = f"{self.base_url}/api/face/creator/jobs/{job_id}/status"
        headers = {"Authorization": self._auth_header_value(bearer_token)}

        async with self._client(timeout_s=timeout_s) as client:
            return await self._request_json(
                client=client,
                method="GET",
                url=url,
                headers=headers,
                json=None,
                timeout_s=timeout_s,
                retries=retries,
                retry_on_status={429, 500, 502, 503, 504},
            )

    async def wait_for_creator_face(
        self,
        *,
        bearer_token: Optional[str],
        job_id: str,
        timeout_s: float = 180.0,
        poll_s: float = 2.0,
        status_timeout_s: float = 15.0,
        status_retries: int = 3,
    ) -> FaceJobResult:
        """
        Wait until succeeded/failed, return first variant image_url (if any).

        - Uses a single httpx client for polling.
        - GET status is retried on transient errors.
        - Returns FaceJobResult(status="timeout") on deadline exceed.
        """
        job_id = str(job_id or "").strip()
        if not job_id:
            raise RuntimeError("missing job_id for svc-face polling")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout_s)

        poll_s = max(0.35, float(poll_s or 2.0))
        auth = self._auth_header_value(bearer_token)

        last: Dict[str, Any] = {}

        async with self._client(timeout_s=status_timeout_s) as client:
            while True:
                last = await self._request_json(
                    client=client,
                    method="GET",
                    url=f"{self.base_url}/api/face/creator/jobs/{job_id}/status",
                    headers={"Authorization": auth},
                    json=None,
                    timeout_s=status_timeout_s,
                    retries=status_retries,
                    retry_on_status={429, 500, 502, 503, 504},
                )

                st, variants, img = self._extract_status_variants_image(last)
                st_l = (st or "").strip().lower()

                # tolerate enums printed as "JobStatus.SUCCEEDED"
                if "succeeded" in st_l:
                    return FaceJobResult(
                        job_id=job_id,
                        status=st_l or "succeeded",
                        image_url=img,
                        variants=variants,
                        raw=last,
                    )

                if "failed" in st_l or "cancel" in st_l:
                    return FaceJobResult(
                        job_id=job_id,
                        status=st_l or "failed",
                        image_url=img,
                        variants=variants,
                        raw=last,
                    )

                if loop.time() >= deadline:
                    return FaceJobResult(
                        job_id=job_id,
                        status="timeout",
                        image_url=img,
                        variants=variants,
                        raw=last,
                    )

                await asyncio.sleep(poll_s)

    # -----------------------------
    # Internals
    # -----------------------------
    def _auth_header_value(self, bearer_token: Optional[str]) -> str:
        """
        Worker-safe auth resolution:
          - Use passed bearer_token if present
          - Else fallback to env-configured service token (SVC_FACE_BEARER_TOKEN)
        """
        fallback = getattr(settings, "SVC_FACE_BEARER_TOKEN", None)
        t = str((bearer_token or fallback or "")).strip()

        if not t:
            raise RuntimeError(
                "missing bearer_token for svc-face request. "
                "Provide bearer_token, or set SVC_FACE_BEARER_TOKEN for worker-mode."
            )

        if t.lower().startswith("bearer "):
            return t
        return f"Bearer {t}"

    def _client(self, *, timeout_s: float) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=5.0,
            read=float(timeout_s),
            write=10.0,
            pool=5.0,
        )
        limits = httpx.Limits(max_keepalive_connections=10, max_connections=30)
        return httpx.AsyncClient(timeout=timeout, follow_redirects=True, limits=limits)

    async def _request_json(
        self,
        *,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: Dict[str, str],
        json: Optional[Dict[str, Any]],
        timeout_s: float,
        retries: int,
        retry_on_status: Set[int],
    ) -> Dict[str, Any]:
        method_u = method.upper().strip()

        attempt = 0
        max_attempts = max(1, int(retries) + 1)

        while True:
            attempt += 1
            try:
                resp = await client.request(
                    method_u,
                    url,
                    headers=headers,
                    json=json,
                    timeout=httpx.Timeout(connect=5.0, read=float(timeout_s), write=10.0, pool=5.0),
                )

                # No content
                if resp.status_code == 204:
                    return {}

                # Retryable statuses
                if resp.status_code in retry_on_status and attempt < max_attempts:
                    await self._backoff_sleep(attempt)
                    continue

                resp.raise_for_status()

                try:
                    data = resp.json()
                except Exception:
                    snippet = (resp.text or "").strip().replace("\n", " ")[:300]
                    raise RuntimeError(
                        f"svc-face non-json response status={resp.status_code} body_snippet='{snippet}'"
                    )

                if isinstance(data, dict):
                    return data

                # Sometimes APIs return a list/str etc; wrap for stability
                return {"_raw": data}

            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.ConnectError) as e:
                if attempt < max_attempts:
                    await self._backoff_sleep(attempt)
                    continue
                raise RuntimeError(f"svc-face request failed (network): {method_u} {url}: {e}") from e

            except httpx.HTTPStatusError as e:
                status = getattr(e.response, "status_code", None)
                body = ""
                try:
                    body = (e.response.text or "").strip().replace("\n", " ")[:300]
                except Exception:
                    body = ""

                if status in retry_on_status and attempt < max_attempts:
                    await self._backoff_sleep(attempt)
                    continue

                raise RuntimeError(
                    f"svc-face HTTP error {status} for {method_u} {url}. body_snippet='{body}'"
                ) from e

            except Exception:
                if attempt < max_attempts:
                    await self._backoff_sleep(attempt)
                    continue
                raise

    async def _backoff_sleep(self, attempt: int) -> None:
        # exponential backoff with jitter: 0.2, 0.4, 0.8, 1.6... capped at 2.0
        base = min(2.0, 0.2 * (2 ** max(0, attempt - 1)))
        jitter = random.uniform(0.0, base * 0.25)
        await asyncio.sleep(base + jitter)

    def _extract_status_variants_image(self, payload: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], Optional[str]]:
        """
        Tolerant extraction across slight schema variations.

        Expected (typical):
          { "status": "succeeded", "variants": [ {"image_url": "..."} ] }

        Also tolerates:
          - status nested under "job" or "result"
          - variants under "data"
          - image_url at top-level
          - variant url keys: image_url / url / storage_ref
        """
        st = str(
            payload.get("status")
            or _deep_get(payload, ("job", "status"))
            or _deep_get(payload, ("result", "status"))
            or ""
        ).strip()

        variants_raw = (
            payload.get("variants")
            or _deep_get(payload, ("data", "variants"))
            or _deep_get(payload, ("result", "variants"))
            or []
        )

        variants: List[Dict[str, Any]] = []
        if isinstance(variants_raw, list):
            for v in variants_raw:
                if isinstance(v, dict):
                    variants.append(v)

        img = None
        if variants:
            v0 = variants[0]
            img = (
                _maybe_str(v0.get("image_url"))
                or _maybe_str(v0.get("url"))
                or _maybe_str(v0.get("storage_ref"))
            )

        if not img:
            img = _maybe_str(payload.get("image_url")) or _maybe_str(_deep_get(payload, ("result", "image_url")))

        return st or "", variants, img


def _maybe_str(x: Any) -> Optional[str]:
    if x is None:
        return None
    s = str(x).strip()
    return s if s else None


def _deep_get(d: Dict[str, Any], path: Tuple[str, ...]) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur