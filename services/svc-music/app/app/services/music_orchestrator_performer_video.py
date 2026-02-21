from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from uuid import UUID

from app.services.music_orchestrator_common import _as_dict

JsonDict = Dict[str, Any]


# -----------------------------
# env helpers
# -----------------------------
def _truthy(v: Any) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _get_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    s = str(v).strip() if v is not None else ""
    return s or default


def _get_int(name: str, default: int) -> int:
    v = os.getenv(name)
    try:
        return int(float(v)) if v is not None else int(default)
    except Exception:
        return int(default)


def _now() -> float:
    return float(time.time())


def _error_str(e: BaseException) -> str:
    try:
        msg = str(e)
    except Exception:
        msg = "unknown_error"
    return f"{type(e).__name__}:{msg}"


# -----------------------------
# http + errors
# -----------------------------
class _HttpError(RuntimeError):
    def __init__(self, *, code: int, url: str, body: str):
        super().__init__(f"http_error code={code} url={url} body={body[:800]}")
        self.code = int(code)
        self.url = str(url)
        self.body = str(body or "")


async def _http_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[dict] = None,
    timeout_s: int = 60,
) -> JsonDict:
    def _do() -> JsonDict:
        data = None
        h = dict(headers or {})
        if payload is not None:
            b = json.dumps(payload).encode("utf-8")
            data = b
            h["Content-Type"] = "application/json"
        if "Accept" not in h:
            h["Accept"] = "application/json"

        req = urllib.request.Request(url, data=data, method=method.upper(), headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return json.loads(raw) if raw else {}
                except Exception:
                    return {"_raw": raw}
        except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
            raw = ""
            try:
                raw = e.read().decode("utf-8", errors="replace")
            except Exception:
                raw = ""
            raise _HttpError(code=int(getattr(e, "code", 0) or 0), url=url, body=raw) from e

    return await asyncio.to_thread(_do)


# -----------------------------
# token handling (product-grade)
# -----------------------------
_TOKEN_LOCK = asyncio.Lock()
_TOKEN_CACHE: Dict[str, Any] = {"token": "", "exp": 0.0, "source": ""}

# When we observe a 401/403, we temporarily skip env tokens (which might be revoked/invalid).
_SKIP_ENV_TOKEN_UNTIL: float = 0.0


def _strip_quotes(s: str) -> str:
    s = (s or "").strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1].strip()
    return s


def _maybe_extract_token_string(raw: str) -> str:
    """
    Accept:
      - raw JWT
      - "Bearer <jwt>"
      - JSON blob like {"access_token": "..."}
    """
    s = _strip_quotes(raw or "")
    if not s:
        return ""

    if s.startswith("{") and ("access_token" in s or '"token"' in s):
        try:
            j = json.loads(s)
            s = str(j.get("access_token") or j.get("token") or "").strip()
        except Exception:
            pass

    if s.lower().startswith("bearer "):
        s = s.split(" ", 1)[1].strip()

    return s


def _jwt_exp(token: str) -> float:
    """
    Best-effort parse JWT exp. Returns 0 if not parseable.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return 0.0
        payload_b64 = parts[1]
        pad = "=" * ((4 - (len(payload_b64) % 4)) % 4)
        payload_b64 = payload_b64 + pad
        payload = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8", errors="replace")
        j = json.loads(payload)
        exp = float(j.get("exp") or 0)
        return exp
    except Exception:
        return 0.0


def _token_fresh(exp: float, *, skew_s: int = 30) -> bool:
    return exp > (_now() + float(skew_s))


def _invalidate_token_cache(*, skip_env_for_s: int = 180) -> None:
    global _SKIP_ENV_TOKEN_UNTIL
    _TOKEN_CACHE.update({"token": "", "exp": 0.0, "source": ""})
    _SKIP_ENV_TOKEN_UNTIL = max(_SKIP_ENV_TOKEN_UNTIL, _now() + float(skip_env_for_s))


async def _get_access_token_via_service_login() -> str:
    """
    Mint a fresh access_token from svc-core using a service account.
    Store creds (NOT access token) in env:
      DF_CORE_URL=http://svc-core:8000
      DF_SERVICE_EMAIL=user2@desifaces.ai
      DF_SERVICE_PASSWORD=password2
    """
    core_url = _get_str("DF_CORE_URL", "") or _get_str("CORE_URL", "") or "http://svc-core:8000"
    core_url = core_url.rstrip("/")
    email = _get_str("DF_SERVICE_EMAIL", "")
    password = _get_str("DF_SERVICE_PASSWORD", "")

    if not email or not password:
        raise RuntimeError("missing_service_credentials_set_DF_SERVICE_EMAIL_DF_SERVICE_PASSWORD")

    payload = {
        "email": email,
        "password": password,
        "device_id": _get_str("DF_SERVICE_DEVICE_ID", "svc-music-worker"),
        "client_type": _get_str("DF_SERVICE_CLIENT_TYPE", "internal"),
    }

    obj = await _http_json(
        "POST",
        f"{core_url}/api/auth/login",
        payload=payload,
        timeout_s=_get_int("DF_CORE_LOGIN_TIMEOUT_SECS", 30),
    )

    tok = str(obj.get("access_token") or obj.get("token") or "").strip()
    if not tok:
        raise RuntimeError(f"service_login_missing_token resp_keys={list(obj.keys())[:40]}")

    tok = _maybe_extract_token_string(tok)
    if not tok:
        raise RuntimeError("service_login_token_parse_failed")

    return tok


async def _get_fusion_token(*, force_service_login: bool = False) -> Dict[str, Any]:
    """
    Returns dict: {token, exp, source}

    Priority:
      1) cached token (if fresh)
      2) env token(s) (if fresh) unless temporarily skipped
         - if DF_ALLOW_OPAQUE_TOKENS=1, allow env tokens that aren't JWTs (exp=now+10m)
      3) service login (core) with caching
    """
    allow_opaque = _truthy(os.getenv("DF_ALLOW_OPAQUE_TOKENS", "0"))

    async with _TOKEN_LOCK:
        cached_tok = str(_TOKEN_CACHE.get("token") or "")
        cached_exp = float(_TOKEN_CACHE.get("exp") or 0.0)
        if not force_service_login and cached_tok and _token_fresh(cached_exp):
            return {"token": cached_tok, "exp": cached_exp, "source": str(_TOKEN_CACHE.get("source") or "cache")}

        allow_env = (not force_service_login) and (_now() >= float(_SKIP_ENV_TOKEN_UNTIL))

        if allow_env:
            raw = (
                _get_str("DF_FUSION_BEARER_TOKEN", "")
                or _get_str("DF_INTERNAL_BEARER_TOKEN", "")
                or _get_str("DF_AUTH_TOKEN", "")
                or _get_str("BEARER_TOKEN", "")
            ).strip()

            env_tok = _maybe_extract_token_string(raw)
            if env_tok:
                env_exp = _jwt_exp(env_tok) or 0.0
                if env_exp and _token_fresh(env_exp):
                    _TOKEN_CACHE.update({"token": env_tok, "exp": env_exp, "source": "env"})
                    return {"token": env_tok, "exp": env_exp, "source": "env"}

                if allow_opaque and env_exp == 0.0:
                    # treat opaque token as short-lived
                    exp = _now() + float(_get_int("DF_OPAQUE_TOKEN_TTL_SECS", 600))
                    _TOKEN_CACHE.update({"token": env_tok, "exp": exp, "source": "env_opaque"})
                    return {"token": env_tok, "exp": exp, "source": "env_opaque"}

        fresh = await _get_access_token_via_service_login()
        exp = _jwt_exp(fresh) or (_now() + 600.0)
        _TOKEN_CACHE.update({"token": fresh, "exp": exp, "source": "service_login"})
        return {"token": fresh, "exp": exp, "source": "service_login"}


def _build_headers(*, proj: Dict[str, Any], token: str) -> Dict[str, str]:
    headers: Dict[str, str] = {"Accept": "application/json"}
    tok = _maybe_extract_token_string(token)
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    # Use job owner by default; only override with DF_X_USER_ID if explicitly set.
    x_user = (_get_str("DF_X_USER_ID", "") or str(proj.get("user_id") or "")).strip()
    if x_user:
        headers["X-User-Id"] = x_user

    headers["X-Request-Source"] = "svc-music"
    return headers


# -----------------------------
# user-driven performance hints (props are OPTIONAL)
# -----------------------------
def _user_performance_hints(input_json: JsonDict) -> Dict[str, Any]:
    """
    Props must be user/UI/intent provided. We do not invent props here.
    Reads: input_json.provider_hints.performance.*
    """
    hints = _as_dict(input_json.get("provider_hints"))
    perf = _as_dict(hints.get("performance"))
    if not perf:
        return {}

    out: Dict[str, Any] = {}

    for k in ("mode", "energy", "stage", "wardrobe", "camera", "motion_prompt"):
        v = perf.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()

    emo = perf.get("emotion")
    if isinstance(emo, list):
        emo2 = [str(x).strip() for x in emo if str(x).strip()]
        if emo2:
            out["emotion"] = emo2

    props = perf.get("props")
    if isinstance(props, list):
        props2 = [str(p).strip() for p in props if str(p).strip()]
        if props2:
            out["props"] = props2

    bg = _as_dict(perf.get("background"))
    if bg:
        bt = str(bg.get("type") or "").strip().lower()
        url = str(bg.get("url") or "").strip()
        if bt:
            out["background"] = {"type": bt, "url": url or None}

    return out


def _stable_json(obj: Any) -> str:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return "{}"


# -----------------------------
# payload helpers
# -----------------------------
def _find_first_video_url(obj: Any) -> Optional[str]:
    if isinstance(obj, str):
        s = obj.strip()
        if not s.startswith("http"):
            return None
        sl = s.lower()
        if ".mp4" in sl or "blob.core.windows.net" in sl or "video" in sl:
            return s
        return None

    if isinstance(obj, dict):
        # common keys across implementations
        for k in (
            "final_url",
            "video_url",
            "mp4_url",
            "url",
            "sas_url",
            "preview_url",
            "output_url",
            "render_url",
        ):
            v = obj.get(k)
            if isinstance(v, str) and v.strip().startswith("http"):
                return v.strip()
        # nested common patterns
        for k in ("output", "result", "data", "artifact", "artifacts"):
            hit = _find_first_video_url(obj.get(k))
            if hit:
                return hit
        for v in obj.values():
            hit = _find_first_video_url(v)
            if hit:
                return hit
        return None

    if isinstance(obj, list):
        for v in obj:
            hit = _find_first_video_url(v)
            if hit:
                return hit
        return None

    return None


def _pick_face_ref(input_json: JsonDict) -> Dict[str, Optional[str]]:
    computed = _as_dict(input_json.get("computed"))
    hints = _as_dict(input_json.get("provider_hints"))

    # IMPORTANT: include performer_a_image_url (your common svc-face output)
    face_image_url = (
        str(os.getenv("DF_PERFORMER_FACE_IMAGE_URL") or "").strip()
        or str(hints.get("performer_face_image_url") or "").strip()
        or str(computed.get("performer_face_image_url") or "").strip()
        or str(computed.get("performer_a_image_url") or "").strip()
        or str(computed.get("performer_image_url") or "").strip()
        or str(computed.get("performer_face_url") or "").strip()
        or str(computed.get("face_image_url") or "").strip()
        or str(computed.get("face_url") or "").strip()
    )
    face_artifact_id = (
        str(os.getenv("DF_PERFORMER_FACE_ARTIFACT_ID") or "").strip()
        or str(hints.get("performer_face_artifact_id") or "").strip()
        or str(computed.get("performer_face_artifact_id") or "").strip()
        or str(computed.get("performer_artifact_id") or "").strip()
    )

    return {"face_image_url": face_image_url or None, "face_artifact_id": face_artifact_id or None}


def _pick_audio_url(input_json: JsonDict) -> Optional[str]:
    computed = _as_dict(input_json.get("computed"))
    for k in ("audio_master_url", "byo_audio_url", "uploaded_audio_url", "demo_audio_url"):
        v = str(computed.get(k) or "").strip()
        if v.startswith("http"):
            return v
    return None


def _req_key(
    *,
    provider: str,
    audio_url: str,
    face_image_url: Optional[str],
    face_artifact_id: Optional[str],
    performance_hints: Dict[str, Any],
) -> str:
    s = "|".join(
        [
            str(provider or "").strip(),
            str(audio_url or "").strip(),
            str(face_image_url or "").strip(),
            str(face_artifact_id or "").strip(),
            _stable_json(performance_hints),
        ]
    )
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _fusion_paths() -> Dict[str, str]:
    create_path = _get_str("DF_FUSION_CREATE_PATH", "/jobs")
    poll_prefix = _get_str("DF_FUSION_POLL_PATH_PREFIX", "/jobs")
    return {
        "create_path": create_path if create_path.startswith("/") else f"/{create_path}",
        "poll_prefix": poll_prefix if poll_prefix.startswith("/") else f"/{poll_prefix}",
    }


async def _fusion_create(
    *,
    fusion_base: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout_s: int,
) -> JsonDict:
    paths = _fusion_paths()
    urls = [f"{fusion_base}{paths['create_path']}", f"{fusion_base}/api/fusion/jobs"]
    last_err: Optional[Exception] = None
    for u in urls:
        try:
            return await _http_json("POST", u, headers=headers, payload=payload, timeout_s=timeout_s)
        except _HttpError as e:
            last_err = e
            if e.code not in (404, 405):
                raise
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError("fusion_create_failed_unknown")


async def _fusion_poll(
    *,
    fusion_base: str,
    headers: Dict[str, str],
    fusion_job_id: str,
    timeout_s: int,
) -> JsonDict:
    paths = _fusion_paths()
    urls = [f"{fusion_base}{paths['poll_prefix']}/{fusion_job_id}", f"{fusion_base}/api/fusion/jobs/{fusion_job_id}"]
    last_err: Optional[Exception] = None
    for u in urls:
        try:
            return await _http_json("GET", u, headers=headers, timeout_s=timeout_s)
        except _HttpError as e:
            last_err = e
            if e.code not in (404, 405):
                raise
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError("fusion_poll_failed_unknown")


def _normalize_status(obj: JsonDict) -> str:
    s = str(obj.get("status") or "").strip().lower()
    if s:
        return s
    d = _as_dict(obj.get("data"))
    return str(d.get("status") or "").strip().lower()


def _extract_job_id(obj: JsonDict) -> str:
    for k in ("job_id", "id"):
        v = str(obj.get(k) or "").strip()
        if v:
            return v
    d = _as_dict(obj.get("data"))
    return str(d.get("job_id") or d.get("id") or "").strip()


# -----------------------------
# main entry
# -----------------------------
async def ensure_performer_video_for_job(
    *,
    steps,
    jobs,
    job_id: UUID,
    proj: Dict[str, Any],
    input_json: JsonDict,
) -> JsonDict:
    """
    Generates ONE performer video via svc-fusion and persists:
      computed.performer_video_url
      computed.performer_video_job_id
      computed.performer_video_provider
      computed.performer_video_source
      computed.performer_video_fusion_base
      computed.performer_video_auth_source
      computed.performer_video_request_key
      computed.performer_video_audio_url
      computed.performer_video_face_image_url / performer_video_face_artifact_id
      computed.performer_video_performance (user-driven)
      computed.performer_video_props (optional)

    Gating:
      DF_ENABLE_PERFORMER_VIDEOS=1  -> attempt generation
      DF_REQUIRE_PERFORMER_VIDEOS=1 -> fail if not possible
    """
    hints = _as_dict(input_json.get("provider_hints"))
    enable = _truthy(os.getenv("DF_ENABLE_PERFORMER_VIDEOS", "0")) or _truthy(hints.get("enable_performer_videos"))
    require = _truthy(os.getenv("DF_REQUIRE_PERFORMER_VIDEOS", "0")) or _truthy(hints.get("require_performer_videos"))

    computed = _as_dict(input_json.get("computed"))

    computed["performer_video_required"] = bool(require)
    computed["performer_video_enabled"] = bool(enable or require)

    if not enable and not require:
        computed["performer_video_skipped"] = True
        computed["performer_video_skip_reason"] = "disabled"
        input_json["computed"] = computed
        return input_json

    face = _pick_face_ref(input_json)
    audio_url = _pick_audio_url(input_json)

    if not audio_url:
        computed["performer_video_skipped"] = True
        computed["performer_video_skip_reason"] = "missing_audio_url"
        input_json["computed"] = computed
        if require:
            raise RuntimeError("performer_video_missing_audio_url")
        return input_json

    if not (face["face_image_url"] or face["face_artifact_id"]):
        computed["performer_video_skipped"] = True
        computed["performer_video_skip_reason"] = "missing_face_ref"
        input_json["computed"] = computed
        if require:
            raise RuntimeError("performer_video_missing_face_ref")
        return input_json

    fusion_base = _get_str("DF_FUSION_URL", "") or _get_str("FUSION_URL", "") or "http://svc-fusion:8002"
    fusion_base = fusion_base.rstrip("/")

    provider = str(hints.get("fusion_provider") or "").strip() or _get_str("DF_FUSION_PROVIDER", "") or "heygen_av4"

    perf_hints = _user_performance_hints(input_json)

    rk = _req_key(
        provider=provider,
        audio_url=str(audio_url),
        face_image_url=face["face_image_url"],
        face_artifact_id=face["face_artifact_id"],
        performance_hints=perf_hints,
    )

    existing = str(computed.get("performer_video_url") or "").strip()
    existing_rk = str(computed.get("performer_video_request_key") or "").strip()
    if existing.startswith("http") and existing_rk == rk:
        computed["performer_video_skipped"] = False
        computed["performer_video_skip_reason"] = None
        input_json["computed"] = computed
        return input_json

    # If URL exists but request changed, clear it to avoid returning an older run
    if existing.startswith("http") and existing_rk != rk:
        computed["performer_video_url"] = None
        computed["performer_video_job_id"] = None

    # store diagnostics early
    computed["performer_video_fusion_base"] = fusion_base
    computed["performer_video_provider"] = provider
    computed["performer_video_request_key"] = rk
    computed["performer_video_audio_url"] = str(audio_url)
    computed["performer_video_face_image_url"] = face["face_image_url"]
    computed["performer_video_face_artifact_id"] = face["face_artifact_id"]
    computed["performer_video_performance"] = perf_hints
    computed["performer_video_props"] = perf_hints.get("props") if isinstance(perf_hints.get("props"), list) else []
    input_json["computed"] = computed

    # best-effort persist breadcrumbs early
    try:
        await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
    except Exception:
        pass

    payload: Dict[str, Any] = {
        "voice_mode": "audio",
        "voice_audio": {"audio_url": audio_url},
        "provider": provider,
        "tags": {
            "source": "svc-music",
            "music_job_id": str(job_id),
            "music_project_id": str(proj.get("id")),
            "purpose": "performer_video",
            "request_key": rk[:16],
        },
        "consent": {"external_provider_ok": True},
        # only user-driven hints
        "provider_hints": {"performance": perf_hints} if perf_hints else {},
    }
    if face["face_image_url"]:
        payload["face_image_url"] = face["face_image_url"]
    if face["face_artifact_id"]:
        payload["face_artifact_id"] = face["face_artifact_id"]

    tok_info = await _get_fusion_token()
    headers = _build_headers(proj=proj, token=str(tok_info["token"]))
    computed["performer_video_auth_source"] = str(tok_info.get("source") or "")
    input_json["computed"] = computed

    try:
        await steps.upsert_step(
            job_id=job_id,
            step_code="ensure_performer_video",
            status="running",
            meta_json={
                "fusion_base": fusion_base,
                "provider": provider,
                "auth_source": computed.get("performer_video_auth_source"),
                "request_key": rk[:24],
                "has_face_image_url": bool(face["face_image_url"]),
                "has_face_artifact_id": bool(face["face_artifact_id"]),
                "has_auth": bool(headers.get("Authorization")),
                "has_x_user_id": bool(headers.get("X-User-Id")),
                "has_perf_hints": bool(perf_hints),
                "props_count": len(perf_hints.get("props") or []) if isinstance(perf_hints.get("props"), list) else 0,
            },
        )
    except Exception:
        pass

    # ---- Create with one auto-refresh retry on 401/403 ----
    created: JsonDict
    try:
        created = await _fusion_create(
            fusion_base=fusion_base,
            headers=headers,
            payload=payload,
            timeout_s=_get_int("DF_FUSION_CREATE_TIMEOUT_SECS", 60),
        )
    except _HttpError as e:
        if e.code in (401, 403):
            _invalidate_token_cache(skip_env_for_s=_get_int("DF_SKIP_ENV_TOKEN_ON_401_SECS", 300))
            tok_info = await _get_fusion_token(force_service_login=True)
            headers = _build_headers(proj=proj, token=str(tok_info["token"]))
            computed["performer_video_auth_source"] = str(tok_info.get("source") or "service_login")
            input_json["computed"] = computed
            created = await _fusion_create(
                fusion_base=fusion_base,
                headers=headers,
                payload=payload,
                timeout_s=_get_int("DF_FUSION_CREATE_TIMEOUT_SECS", 60),
            )
        else:
            computed["performer_video_skipped"] = True
            computed["performer_video_skip_reason"] = f"fusion_create_failed:{_error_str(e)}"
            input_json["computed"] = computed
            if require:
                raise
            return input_json
    except Exception as e:
        computed["performer_video_skipped"] = True
        computed["performer_video_skip_reason"] = f"fusion_create_failed:{_error_str(e)}"
        input_json["computed"] = computed
        if require:
            raise
        return input_json

    fusion_job_id = _extract_job_id(_as_dict(created))
    if not fusion_job_id:
        computed["performer_video_skipped"] = True
        computed["performer_video_skip_reason"] = "fusion_create_missing_job_id"
        # don't store full response (can be huge); store minimal keys
        computed["performer_video_create_resp"] = {"keys": list(_as_dict(created).keys())[:30]}
        input_json["computed"] = computed
        if require:
            raise RuntimeError("performer_video_fusion_create_missing_job_id")
        return input_json

    computed["performer_video_job_id"] = fusion_job_id
    computed["performer_video_create_resp"] = {"job_id": fusion_job_id}
    input_json["computed"] = computed
    try:
        await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
    except Exception:
        pass

    # ---- Poll (with one 401/403 refresh) ----
    t0 = _now()
    poll_every = max(1, min(30, _get_int("DF_FUSION_POLL_SECS", 5)))
    timeout_s = max(60, min(3600, _get_int("DF_FUSION_TIMEOUT_SECS", 900)))

    last: JsonDict = {}
    refreshed_on_poll = False

    while True:
        if _now() - t0 > float(timeout_s):
            computed["performer_video_skipped"] = True
            computed["performer_video_skip_reason"] = f"fusion_timeout:{timeout_s}s"
            input_json["computed"] = computed
            try:
                await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
            except Exception:
                pass
            if require:
                raise RuntimeError("performer_video_fusion_timeout")
            break

        try:
            last = await _fusion_poll(
                fusion_base=fusion_base,
                headers=headers,
                fusion_job_id=fusion_job_id,
                timeout_s=_get_int("DF_FUSION_POLL_TIMEOUT_SECS", 30),
            )
        except _HttpError as e:
            if (e.code in (401, 403)) and (not refreshed_on_poll):
                refreshed_on_poll = True
                _invalidate_token_cache(skip_env_for_s=_get_int("DF_SKIP_ENV_TOKEN_ON_401_SECS", 300))
                tok_info = await _get_fusion_token(force_service_login=True)
                headers = _build_headers(proj=proj, token=str(tok_info["token"]))
                computed["performer_video_auth_source"] = str(tok_info.get("source") or "service_login")
                input_json["computed"] = computed
                await asyncio.sleep(0.2)
                continue
            await asyncio.sleep(float(poll_every))
            continue
        except Exception:
            await asyncio.sleep(float(poll_every))
            continue

        status = _normalize_status(_as_dict(last))
        if status in ("succeeded", "failed", "canceled", "cancelled"):
            break
        await asyncio.sleep(float(poll_every))

    status = _normalize_status(_as_dict(last))
    if status == "failed":
        err = _as_dict(last.get("data")).get("error_message") or last.get("error_message") or last.get("error") or ""
        computed["performer_video_skipped"] = True
        computed["performer_video_skip_reason"] = f"fusion_failed:{str(err)[:600]}"
        input_json["computed"] = computed
        try:
            await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
        except Exception:
            pass
        if require:
            raise RuntimeError(f"performer_video_fusion_failed:{computed['performer_video_skip_reason']}")
        return input_json

    video_url = _find_first_video_url(last) or _find_first_video_url(_as_dict(last.get("data")))
    if not video_url:
        computed["performer_video_skipped"] = True
        computed["performer_video_skip_reason"] = "fusion_succeeded_but_no_video_url"
        input_json["computed"] = computed
        try:
            await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
        except Exception:
            pass
        if require:
            raise RuntimeError("performer_video_missing_video_url")
        return input_json

    computed["performer_video_url"] = str(video_url)
    computed["performer_video_source"] = "svc-fusion"
    computed["performer_video_skipped"] = False
    computed["performer_video_skip_reason"] = None
    input_json["computed"] = computed

    try:
        await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
    except Exception:
        pass

    try:
        await steps.upsert_step(
            job_id=job_id,
            step_code="ensure_performer_video",
            status="succeeded",
            meta_json={
                "fusion_job_id": fusion_job_id,
                "status": status or "succeeded",
                "has_video_url": True,
                "elapsed_s": round(_now() - t0, 3),
                "auth_source": computed.get("performer_video_auth_source"),
                "request_key": rk[:24],
                "has_perf_hints": bool(perf_hints),
                "props_count": len(perf_hints.get("props") or []) if isinstance(perf_hints.get("props"), list) else 0,
            },
        )
    except Exception:
        pass

    return input_json