# services/svc-marketing/app/app/services/orchestration/downstream_clients.py
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional
from uuid import UUID

import httpx

from app.config import settings

logger = logging.getLogger("svc-marketing-downstream")


@dataclass
class DownstreamContext:
    run_id: UUID
    run_as_user_id: UUID
    bearer_token: Optional[str]
    cost_bucket: str
    cost_category: str


# -------------------------
# config helpers
# -------------------------

def _cfg_str(name: str, default: str = "") -> str:
    v = getattr(settings, name, None)
    if v is None or str(v).strip() == "":
        v = os.getenv(name, "")
    v = str(v).strip() if v is not None else ""
    return v if v else default


def _cfg_int(name: str, default: int) -> int:
    v = _cfg_str(name, "")
    if not v:
        return default
    try:
        return int(float(v))
    except Exception:
        return default


def _cfg_float(name: str, default: float) -> float:
    v = _cfg_str(name, "")
    if not v:
        return default
    try:
        return float(v)
    except Exception:
        return default


def _cfg_bool(name: str, default: bool = False) -> bool:
    v = _cfg_str(name, "")
    if not v:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _get_base_url(marketing_attr: str, fallback_attr: str) -> str:
    v = (_cfg_str(marketing_attr, "") or "").strip()
    if not v:
        v = (_cfg_str(fallback_attr, "") or "").strip()
    if not v:
        raise RuntimeError(f"Missing base URL: set {marketing_attr} (or {fallback_attr})")
    return v.rstrip("/")


def _stable_json(obj: Any) -> str:
    """
    Deterministic JSON string for idempotency hashing.
    Uses default=str so we don't fall back to stringifying the entire payload.
    """
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(str(obj), ensure_ascii=False)


def _norm_bearer_token(t: Any) -> str:
    """
    Accept either:
      - raw JWT "eyJ..."
      - header value "Bearer eyJ..."
    Return raw JWT string.
    Prevents "Bearer Bearer <jwt>" going downstream.
    """
    s = str(t or "").strip()
    if not s:
        return ""
    if s.lower().startswith("bearer "):
        s = s.split(" ", 1)[1].strip()
    return s


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _idem_key(ctx: DownstreamContext, kind: str, payload: Dict[str, Any]) -> str:
    """
    Idempotency key stable within a run for the same logical request, but distinct
    across runs and when seed/nonce changes.
    """
    rn = ""
    sd = ""
    try:
        rn = str(payload.get("request_nonce") or "").strip()
    except Exception:
        rn = ""
    try:
        sd = str(payload.get("seed") or "").strip()
    except Exception:
        sd = ""

    base = f"{ctx.run_id}:{kind}:{rn}:{sd}:{_sha256(_stable_json(payload))}"
    return _sha256(base)


def _headers(
    ctx: DownstreamContext,
    *,
    idempotency_key: str = "",
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    h: Dict[str, str] = {
        "Accept": "application/json",
        "X-DF-RUN-ID": str(ctx.run_id),
        "X-DF-RUN-AS-USER-ID": str(ctx.run_as_user_id),
        "X-DF-COST-BUCKET": ctx.cost_bucket,
        "X-DF-COST-CATEGORY": ctx.cost_category,
        "X-User-Id": str(ctx.run_as_user_id),
        "X-User-ID": str(ctx.run_as_user_id),
    }

    # Prefer per-run token; fallback allowed for local/dev convenience.
    token_ctx = str(ctx.bearer_token or "").strip()
    token_env = _cfg_str("MARKETING_DOWNSTREAM_BEARER_TOKEN", "").strip()
    token_raw = token_ctx or token_env
    tok = _norm_bearer_token(token_raw)
    if tok:
        h["Authorization"] = f"Bearer {tok}"

    # Optional visibility into "silent auth fallback"
    if (not token_ctx) and token_env and _cfg_bool("DOWNSTREAM_LOG_AUTH_FALLBACK", False):
        logger.warning("downstream auth fallback: using MARKETING_DOWNSTREAM_BEARER_TOKEN for run_id=%s", str(ctx.run_id))

    if idempotency_key:
        h["X-DF-IDEMPOTENCY-KEY"] = idempotency_key

    if isinstance(extra_headers, dict):
        for k, v in extra_headers.items():
            if isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip():
                h[k.strip()] = v.strip()

    return h


def _is_transient_status(code: int) -> bool:
    return code in (408, 409, 425, 429, 500, 502, 503, 504)


def _truncate(s: str, n: int = 2400) -> str:
    if not s:
        return ""
    return s if len(s) <= n else (s[: n - 3] + "...")


def _response_debug(r: httpx.Response) -> str:
    try:
        text = r.text
    except Exception:
        try:
            text = r.content.decode("utf-8", errors="ignore")
        except Exception:
            text = ""
    ctype = (r.headers.get("content-type") or "").lower()
    if "application/json" in ctype and text:
        try:
            obj = json.loads(text)
            text = json.dumps(obj, ensure_ascii=False, default=str)
        except Exception:
            pass
    return _truncate(text, 2400)


def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            j = json.loads(s)
            return j if isinstance(j, dict) else {}
        except Exception:
            return {}
    try:
        return dict(x)
    except Exception:
        return {}


def _extract_job_id(resp: Any) -> Optional[str]:
    def _try(d: Dict[str, Any]) -> Optional[str]:
        for k in ("job_id", "studio_job_id", "run_id", "id", "project_id"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for nk in ("job", "data", "result", "output", "submit", "final"):
            v = d.get(nk)
            if isinstance(v, dict):
                got = _try(v)
                if got:
                    return got
        return None

    if isinstance(resp, dict):
        return _try(resp)
    return None


def _extract_quote_id(resp: Any) -> Optional[str]:
    if not isinstance(resp, dict):
        return None
    for k in ("quote_id", "quoteId"):
        v = resp.get(k)
        if isinstance(v, str) and v:
            return v
    quote = resp.get("quote")
    if isinstance(quote, dict):
        v = quote.get("quote_id") or quote.get("quoteId")
        if isinstance(v, str) and v:
            return v
    return None


def _extract_status(resp: Dict[str, Any]) -> Optional[str]:
    for k in ("status", "state"):
        v = resp.get(k)
        if isinstance(v, str) and v:
            return v.lower()
    return None


def _is_done_status(s: Optional[str]) -> bool:
    return bool(s) and s in ("succeeded", "success", "done", "completed", "complete", "finished")


def _is_failed_status(s: Optional[str]) -> bool:
    return bool(s) and s in ("failed", "error", "canceled", "cancelled")


def _deep_find_url(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj if obj.startswith("http") else None
    if isinstance(obj, dict):
        for k in ("audio_url", "music_url", "reel_url", "video_url", "final_url", "preview_url", "output_url", "url"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for k in ("computed", "output", "result", "urls", "video_outputs"):
            v = obj.get(k)
            u = _deep_find_url(v)
            if u:
                return u
        for v in obj.values():
            u = _deep_find_url(v)
            if u:
                return u
    if isinstance(obj, list):
        for it in obj:
            u = _deep_find_url(it)
            if u:
                return u
    return None


def _format_status_path(spec: str, job_id: str) -> str:
    s = (spec or "").strip()
    if not s:
        return ""
    if "{" in s and "}" in s:
        try:
            return s.format(job_id=job_id, studio_job_id=job_id, project_id=job_id)
        except Exception:
            return s.replace("{job_id}", job_id).replace("{studio_job_id}", job_id).replace("{project_id}", job_id)
    return s


def _parse_422_body_from_runtime_error(msg: str) -> Optional[Dict[str, Any]]:
    """
    _request_json raises: 'POST <url> -> 422 body=<json>'
    We parse that json if possible.
    """
    if " -> 422 " not in msg or "body=" not in msg:
        return None
    try:
        body = msg.split("body=", 1)[1].strip()
        if body.startswith("{"):
            return json.loads(body)
        return None
    except Exception:
        return None


def _voice_audio_expected_shape(detail_obj: Dict[str, Any]) -> Optional[str]:
    """
    Returns: "dict" | "string" | None
    Based on pydantic error types in the 422 detail list.
    """
    detail = detail_obj.get("detail")
    if not isinstance(detail, list):
        return None

    for item in detail:
        if not isinstance(item, dict):
            continue
        loc = item.get("loc")
        if not isinstance(loc, list):
            continue

        # pydantic loc often looks like: ["body", "voice_audio"] or ["voice_audio"]
        if not loc:
            continue
        if str(loc[-1]) != "voice_audio":
            continue

        t = str(item.get("type") or "")
        if t in ("model_attributes_type", "dict_type"):
            return "dict"
        if t in ("string_type",):
            return "string"

        msg = str(item.get("msg") or "").lower()
        if "dictionary" in msg or "object" in msg:
            return "dict"
        if "string" in msg:
            return "string"

    return None


def _coerce_voice_audio(payload: Dict[str, Any], expected: str) -> Dict[str, Any]:
    """
    expected: "dict" or "string"
    Coerces payload["voice_audio"] accordingly (best effort).

    IMPORTANT:
      svc-fusion commonly expects an object with audio_url.
      We use {"type":"audio","audio_url": "<url>"} and also add "url" for compatibility.
    """
    p = dict(payload)
    va = p.get("voice_audio")

    if expected == "dict":
        if isinstance(va, dict):
            # If dict has only "url", also provide "audio_url"
            u = va.get("audio_url") or va.get("url")
            if isinstance(u, str) and u.startswith("http"):
                va2 = dict(va)
                va2.setdefault("type", "audio")
                va2.setdefault("audio_url", u)
                va2.setdefault("url", u)
                p["voice_audio"] = va2
            return p

        if isinstance(va, str) and va.startswith("http"):
            p["voice_audio"] = {"type": "audio", "audio_url": va, "url": va}
            return p

        return p

    if expected == "string":
        if isinstance(va, str):
            return p
        if isinstance(va, dict):
            u = va.get("audio_url") or va.get("url")
            if isinstance(u, str) and u.startswith("http"):
                p["voice_audio"] = u
                return p
        return p

    return p


def _maybe_log_request(kind: str, method: str, url: str, payload: Optional[Dict[str, Any]], idem: str) -> None:
    if not _cfg_bool("DOWNSTREAM_LOG_REQUESTS", False):
        return
    keys = []
    if isinstance(payload, dict):
        try:
            keys = sorted(list(payload.keys()))
        except Exception:
            keys = []
    logger.info(
        "downstream %s %s kind=%s idem=%s payload_keys=%s",
        method,
        url,
        kind,
        (idem[:12] if isinstance(idem, str) else ""),
        keys,
    )


class _BaseClient:
    def __init__(self, timeout_s: int):
        limits = httpx.Limits(
            max_connections=_cfg_int("DOWNSTREAM_MAX_CONNECTIONS", 50),
            max_keepalive_connections=_cfg_int("DOWNSTREAM_MAX_KEEPALIVE", 20),
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            limits=limits,
            headers={"User-Agent": "svc-marketing/1.0"},
        )

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def _request_json(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        json_body: Optional[Dict[str, Any]] = None,
        max_attempts: int = 5,
        base_sleep_s: float = 0.6,
    ) -> Dict[str, Any]:
        last_err: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            try:
                r = await self._client.request(method, url, headers=headers, json=json_body)

                if r.status_code >= 400:
                    body = _response_debug(r)

                    # Treat auth as non-transient and fail fast
                    if r.status_code in (401, 403):
                        raise RuntimeError(f"{method} {url} -> {r.status_code} body={body}")

                    if _is_transient_status(r.status_code):
                        raise httpx.HTTPStatusError(
                            f"transient status {r.status_code} for {method} {url} body={body}",
                            request=r.request,
                            response=r,
                        )

                    raise RuntimeError(f"{method} {url} -> {r.status_code} body={body}")

                if not r.content:
                    return {}

                try:
                    return r.json()
                except Exception:
                    try:
                        return {"_raw": r.text}
                    except Exception:
                        return {}

            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as e:
                last_err = e

                if isinstance(e, httpx.HTTPStatusError):
                    code = e.response.status_code if e.response is not None else 0
                    if code and not _is_transient_status(code):
                        raise

                if attempt == max_attempts:
                    break

                sleep_s = base_sleep_s * (2 ** (attempt - 1))
                if sleep_s > 6.0:
                    sleep_s = 6.0
                await asyncio.sleep(sleep_s)

        assert last_err is not None
        raise last_err

    async def _poll_until_done(
        self,
        ctx: DownstreamContext,
        status_url: str,
        poll_interval_s: float,
        timeout_s: int,
        *,
        transient_404_grace_s: float = 12.0,
    ) -> Dict[str, Any]:
        t0 = asyncio.get_event_loop().time()
        last: Dict[str, Any] = {}
        while True:
            now = asyncio.get_event_loop().time()
            try:
                last = await self._request_json("GET", status_url, headers=_headers(ctx))
            except RuntimeError as e:
                msg = str(e)
                if " -> 404 " in msg and (now - t0) <= transient_404_grace_s:
                    await asyncio.sleep(float(poll_interval_s))
                    continue
                raise

            st = _extract_status(last)
            if _is_done_status(st) or _is_failed_status(st):
                last["_df_status_url"] = status_url
                return last

            if (now - t0) > float(timeout_s):
                return {"status": "timeout", "_df_status_url": status_url, "last": last}

            await asyncio.sleep(float(poll_interval_s))


class SvcFaceClient(_BaseClient):
    def __init__(self) -> None:
        super().__init__(timeout_s=_cfg_int("FACE_TIMEOUT_S", int(getattr(settings, "FACE_TIMEOUT_S", 60))))

    async def create(self, ctx: DownstreamContext, payload: Dict[str, Any]) -> Dict[str, Any]:
        base = _get_base_url("SVC_FACE_URL_MARKETING", "SVC_FACE_URL")
        face_create_path = _cfg_str("FACE_CREATE_PATH", getattr(settings, "FACE_CREATE_PATH", "/api/face/creator/generate"))
        url = base + face_create_path

        idem = _idem_key(ctx, "face.create", payload)

        extra: Dict[str, str] = {}
        rn = str(payload.get("request_nonce") or "").strip()
        sd = str(payload.get("seed") or "").strip()
        if rn:
            extra["X-DF-REQUEST-NONCE"] = rn
        if sd:
            extra["X-DF-SEED"] = sd

        _maybe_log_request("face.create", "POST", url, payload, idem)
        resp = await self._request_json(
            "POST",
            url,
            headers=_headers(ctx, idempotency_key=idem, extra_headers=extra),
            json_body=payload,
        )

        spec = (
            _cfg_str("FACE_STATUS_PATH_PREFIX", (getattr(settings, "FACE_STATUS_PATH_PREFIX", "") or "").strip())
            or "/api/face/creator/jobs/{job_id}/status"
        )
        job_id = _extract_job_id(resp)
        if spec and job_id:
            path = _format_status_path(spec, job_id)
            if "{" not in spec and not path.rstrip("/").endswith(job_id):
                path = path.rstrip("/") + f"/{job_id}"
            status_url = base + path
            final = await self._poll_until_done(
                ctx,
                status_url,
                _cfg_float("FACE_POLL_INTERVAL_S", float(getattr(settings, "FACE_POLL_INTERVAL_S", 2.0))),
                _cfg_int("FACE_POLL_TIMEOUT_S", int(getattr(settings, "FACE_POLL_TIMEOUT_S", 240))),
            )
            return {"submit": resp, "final": final, "status_url": status_url}

        return resp


class SvcFusionClient(_BaseClient):
    def __init__(self) -> None:
        super().__init__(timeout_s=_cfg_int("FUSION_TIMEOUT_S", int(getattr(settings, "FUSION_TIMEOUT_S", 120))))

    async def create(self, ctx: DownstreamContext, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        IMPORTANT FIX:
          - svc-fusion validates voice_audio as an object/dict in some versions.
          - Some callers may send it as a string. If we get 422, detect expected shape and retry once.
        """
        base = _get_base_url("SVC_FUSION_URL_MARKETING", "SVC_FUSION_URL")
        fusion_create_path = _cfg_str("FUSION_CREATE_PATH", getattr(settings, "FUSION_CREATE_PATH", "/jobs"))
        url = base + fusion_create_path

        idem = _idem_key(ctx, "fusion.create", payload)
        h = _headers(ctx, idempotency_key=idem)

        _maybe_log_request("fusion.create", "POST", url, payload, idem)

        try:
            resp = await self._request_json("POST", url, headers=h, json_body=payload)
        except RuntimeError as e:
            msg = str(e)
            body = _parse_422_body_from_runtime_error(msg)
            expected = _voice_audio_expected_shape(body) if body else None
            if expected and "voice_audio" in msg:
                p2 = _coerce_voice_audio(payload, expected)
                if p2 != payload:
                    logger.warning("Fusion 422 voice_audio shape mismatch; retrying with expected=%s", expected)
                    resp = await self._request_json("POST", url, headers=h, json_body=p2)
                else:
                    raise
            else:
                raise

        spec = _cfg_str("FUSION_STATUS_PATH_PREFIX", (getattr(settings, "FUSION_STATUS_PATH_PREFIX", "") or "").strip()) or "/jobs/{job_id}"
        job_id = _extract_job_id(resp)
        if spec and job_id:
            path = _format_status_path(spec, job_id)
            if "{" not in spec and not path.rstrip("/").endswith(job_id):
                path = path.rstrip("/") + f"/{job_id}"
            status_url = base + path
            final = await self._poll_until_done(
                ctx,
                status_url,
                _cfg_float("FUSION_POLL_INTERVAL_S", float(getattr(settings, "FUSION_POLL_INTERVAL_S", 3.0))),
                _cfg_int("FUSION_POLL_TIMEOUT_S", int(getattr(settings, "FUSION_POLL_TIMEOUT_S", 1200))),
            )
            v = _deep_find_url(final) or _deep_find_url(resp)
            out = {
                "submit": resp,
                "final": final,
                "status_url": status_url,
                "status": _extract_status(final) or _extract_status(resp),
            }
            if v:
                out["video_url"] = v
                out["reel_url"] = v
            return out

        return resp


class SvcAudioClient(_BaseClient):
    def __init__(self) -> None:
        super().__init__(timeout_s=_cfg_int("AUDIO_TIMEOUT_S", 120))

    async def create(self, ctx: DownstreamContext, payload: Dict[str, Any]) -> Dict[str, Any]:
        base = _get_base_url("SVC_AUDIO_URL_MARKETING", "SVC_AUDIO_URL")
        audio_create_path = _cfg_str("AUDIO_CREATE_PATH", "/api/audio/tts")
        url = base + audio_create_path

        idem = _idem_key(ctx, "audio.create", payload)
        _maybe_log_request("audio.create", "POST", url, payload, idem)
        resp = await self._request_json("POST", url, headers=_headers(ctx, idempotency_key=idem), json_body=payload)

        spec = _cfg_str("AUDIO_STATUS_PATH_PREFIX", "").strip() or "/api/audio/jobs/{job_id}/status"
        job_id = _extract_job_id(resp)
        if spec and job_id:
            path = _format_status_path(spec, job_id)
            if "{" not in spec and not path.rstrip("/").endswith(job_id):
                path = path.rstrip("/") + f"/{job_id}"
            status_url = base + path
            final = await self._poll_until_done(
                ctx,
                status_url,
                _cfg_float("AUDIO_POLL_INTERVAL_S", 2.0),
                _cfg_int("AUDIO_POLL_TIMEOUT_S", 600),
            )
            a = _deep_find_url(final) or _deep_find_url(resp)
            out = {"submit": resp, "final": final, "status_url": status_url, "status": _extract_status(final) or _extract_status(resp)}
            if a:
                out["audio_url"] = a
            return out

        a = _deep_find_url(resp)
        if a:
            resp = dict(resp)
            resp.setdefault("audio_url", a)
        return resp


class SvcMusicClient(_BaseClient):
    def __init__(self) -> None:
        super().__init__(timeout_s=_cfg_int("MUSIC_TIMEOUT_S", int(getattr(settings, "MUSIC_TIMEOUT_S", 120))))

    async def create(self, ctx: DownstreamContext, payload: Dict[str, Any]) -> Dict[str, Any]:
        base = _get_base_url("SVC_MUSIC_URL_MARKETING", "SVC_MUSIC_URL")

        project_id = payload.get("project_id") or payload.get("id") if isinstance(payload, dict) else None

        create_path = _cfg_str("MUSIC_CREATE_PROJECT_PATH", "/api/music/projects")
        generate_path_tpl = _cfg_str("MUSIC_GENERATE_PATH_TEMPLATE", "/api/music/projects/{project_id}/generate")
        status_spec = _cfg_str("MUSIC_STATUS_PATH_PREFIX", "").strip() or "/api/music/jobs/{job_id}/status"

        create_resp: Optional[Dict[str, Any]] = None
        if not project_id:
            idem = _idem_key(ctx, "music.project.create", payload)
            _maybe_log_request("music.project.create", "POST", base + create_path, payload, idem)
            create_resp = await self._request_json("POST", base + create_path, headers=_headers(ctx, idempotency_key=idem), json_body=payload)
            project_id = (create_resp.get("project_id") if isinstance(create_resp, dict) else None) or _extract_job_id(create_resp)

        if not project_id:
            return {"submit": create_resp or {}, "final": {"status": "failed", "error": "MUSIC_PROJECT_ID_MISSING"}}

        gen_path = generate_path_tpl.format(project_id=str(project_id))
        gen_payload = _as_dict(payload.get("generate_payload")) if isinstance(payload, dict) else {}
        idem2 = _idem_key(ctx, "music.project.generate", {"project_id": str(project_id), **gen_payload})

        _maybe_log_request("music.project.generate", "POST", base + gen_path, gen_payload, idem2)
        gen_resp = await self._request_json("POST", base + gen_path, headers=_headers(ctx, idempotency_key=idem2), json_body=gen_payload or {})

        job_id = _extract_job_id(gen_resp)
        if not job_id:
            u = _deep_find_url(gen_resp)
            out = {"project": create_resp, "submit": gen_resp, "status": _extract_status(gen_resp)}
            if u:
                out["audio_url"] = u
            return out

        path = _format_status_path(status_spec, str(job_id))
        if "{" not in status_spec and not path.rstrip("/").endswith(str(job_id)):
            path = path.rstrip("/") + f"/{job_id}"
        status_url = base + path

        final = await self._poll_until_done(
            ctx,
            status_url,
            _cfg_float("MUSIC_POLL_INTERVAL_S", float(getattr(settings, "MUSIC_POLL_INTERVAL_S", 3.0))),
            _cfg_int("MUSIC_POLL_TIMEOUT_S", int(getattr(settings, "MUSIC_POLL_TIMEOUT_S", 1200))),
        )

        out = {"project": create_resp, "submit": gen_resp, "final": final, "status_url": status_url, "status": _extract_status(final) or _extract_status(gen_resp)}
        u = _deep_find_url(final) or _deep_find_url(gen_resp)
        if u:
            out["audio_url"] = u
        return out


class SvcCommerceClient(_BaseClient):
    def __init__(self) -> None:
        super().__init__(timeout_s=_cfg_int("COMMERCE_TIMEOUT_S", int(getattr(settings, "COMMERCE_TIMEOUT_S", 120))))

    async def create(self, ctx: DownstreamContext, payload: Dict[str, Any]) -> Dict[str, Any]:
        base = _get_base_url("SVC_COMMERCE_URL_MARKETING", "SVC_COMMERCE_URL")

        create_path = _cfg_str("COMMERCE_CREATE_PATH", "").strip() or _cfg_str(
            "COMMERCE_PROMO_PATH",
            getattr(settings, "COMMERCE_PROMO_PATH", "/api/commerce/quote"),
        )
        confirm_path = _cfg_str("COMMERCE_CONFIRM_PATH", "/api/commerce/confirm")

        url = base + create_path
        idem = _idem_key(ctx, "commerce.create", payload)
        _maybe_log_request("commerce.create", "POST", url, payload, idem)
        quote_or_submit = await self._request_json("POST", url, headers=_headers(ctx, idempotency_key=idem), json_body=payload)

        job_id = _extract_job_id(quote_or_submit)
        quote_id = _extract_quote_id(quote_or_submit)
        confirm_resp: Optional[Dict[str, Any]] = None

        if not job_id and quote_id and create_path.rstrip("/").endswith("/quote"):
            confirm_body: Dict[str, Any] = {"quote_id": quote_id}
            overrides = payload.get("confirm_payload") if isinstance(payload, dict) else None
            if isinstance(overrides, dict):
                confirm_body.update(overrides)

            idem2 = _idem_key(ctx, "commerce.confirm", confirm_body)
            _maybe_log_request("commerce.confirm", "POST", base + confirm_path, confirm_body, idem2)
            confirm_resp = await self._request_json("POST", base + confirm_path, headers=_headers(ctx, idempotency_key=idem2), json_body=confirm_body)
            job_id = _extract_job_id(confirm_resp)

        status_spec = _cfg_str("COMMERCE_STATUS_PATH_PREFIX", (getattr(settings, "COMMERCE_STATUS_PATH_PREFIX", "") or "").strip()) or "/api/commerce/jobs/{studio_job_id}/status"

        if job_id:
            path = _format_status_path(status_spec, str(job_id))
            if "{" not in status_spec and not path.rstrip("/").endswith(str(job_id)):
                path = path.rstrip("/") + f"/{job_id}"
            status_url = base + path

            final = await self._poll_until_done(
                ctx,
                status_url,
                _cfg_float("COMMERCE_POLL_INTERVAL_S", float(getattr(settings, "COMMERCE_POLL_INTERVAL_S", 3.0))),
                _cfg_int("COMMERCE_POLL_TIMEOUT_S", int(getattr(settings, "COMMERCE_POLL_TIMEOUT_S", 1200))),
            )
            v = _deep_find_url(final) or _deep_find_url(confirm_resp) or _deep_find_url(quote_or_submit)
            out = {
                "quote": quote_or_submit if confirm_resp is not None else None,
                "confirm": confirm_resp,
                "submit": confirm_resp or quote_or_submit,
                "final": final,
                "status_url": status_url,
                "status": _extract_status(final) or _extract_status(confirm_resp or quote_or_submit),
            }
            if v:
                out["video_url"] = v
                out["reel_url"] = v
            return out

        if confirm_resp is not None:
            return {"quote": quote_or_submit, "confirm": confirm_resp}
        return quote_or_submit