# services/svc-music/app/app/scripts/e2e/df_e2e_music_3modes.py
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

JsonDict = Dict[str, Any]


# -----------------------------
# Time helpers (timezone-aware UTC)
# -----------------------------
def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso_utc_z(ts: Optional[dt.datetime] = None) -> str:
    x = ts or _utc_now()
    # 2026-02-18T18:03:14.568460Z
    s = x.isoformat()
    return s.replace("+00:00", "Z")


# -----------------------------
# Small IO helpers
# -----------------------------
def _now_tag() -> str:
    return _utc_now().strftime("%Y%m%d_%H%M%S")


def _snip(s: str, n: int = 220) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def _read_env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


def _read_env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    v = _read_env(name)
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default


def _read_env_float(name: str, default: Optional[float] = None) -> Optional[float]:
    v = _read_env(name)
    if v is None:
        return default
    try:
        return float(v)
    except Exception:
        return default


def _read_env_bool(name: str, default: bool = False) -> bool:
    v = _read_env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _csv_list(s: Optional[str]) -> list[str]:
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def _as_dict(x: Any) -> JsonDict:
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            o = json.loads(s)
            return o if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


def _walk_find_first(d: Any, keys: Tuple[str, ...]) -> Optional[Any]:
    if isinstance(d, dict):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        for v in d.values():
            hit = _walk_find_first(v, keys)
            if hit is not None:
                return hit
    elif isinstance(d, list):
        for v in d:
            hit = _walk_find_first(v, keys)
            if hit is not None:
                return hit
    return None


def _decode_jwt_sub(token: str) -> Optional[str]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        pad = "=" * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode((payload_b64 + pad).encode("utf-8"))
        obj = json.loads(payload.decode("utf-8"))
        sub = obj.get("sub")
        return str(sub) if sub else None
    except Exception:
        return None


# -----------------------------
# HTTP helpers (stdlib)
# -----------------------------
@dataclass
class HttpResp:
    status: int
    headers: Dict[str, str]
    body: bytes


def _http(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    timeout_s: int = 60,
) -> HttpResp:
    hdrs = dict(headers or {})
    req = Request(url, method=method.upper(), headers=hdrs, data=data)
    try:
        with urlopen(req, timeout=timeout_s) as r:
            body = r.read() or b""
            h = {k.lower(): v for k, v in (r.headers.items() if r.headers else [])}
            return HttpResp(status=int(r.status), headers=h, body=body)
    except HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        h = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        return HttpResp(status=int(getattr(e, "code", 0) or 0), headers=h, body=body)
    except URLError as e:
        raise RuntimeError(f"http_url_error url={url} err={e}") from e


def _json_http(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[JsonDict] = None,
    timeout_s: int = 60,
) -> Tuple[int, JsonDict, str]:
    hdrs = dict(headers or {})
    hdrs.setdefault("accept", "application/json")
    body_bytes = b""
    if payload is not None:
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("content-type", "application/json")

    resp = _http(method, url, headers=hdrs, data=(body_bytes if payload is not None else None), timeout_s=timeout_s)
    text = ""
    try:
        text = resp.body.decode("utf-8", errors="replace")
    except Exception:
        text = ""

    obj: JsonDict = {}
    if text.strip():
        try:
            parsed = json.loads(text)
            obj = parsed if isinstance(parsed, dict) else {"_": parsed}
        except Exception:
            obj = {"_raw": text}

    return resp.status, obj, text


def _multipart_form_data(fields: Dict[str, str], files: Dict[str, Tuple[str, bytes, str]]) -> Tuple[bytes, str]:
    boundary = "----dfBoundary" + uuid.uuid4().hex
    lines: list[bytes] = []

    def add_line(s: str) -> None:
        lines.append(s.encode("utf-8"))

    for name, val in fields.items():
        add_line(f"--{boundary}")
        add_line(f'Content-Disposition: form-data; name="{name}"')
        add_line("")
        add_line(val)

    for name, (filename, content, content_type) in files.items():
        add_line(f"--{boundary}")
        add_line(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"')
        add_line(f"Content-Type: {content_type}")
        add_line("")
        lines.append(content)

    add_line(f"--{boundary}--")
    add_line("")
    body = b"\r\n".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"


def _upload_file_multipart(
    url: str,
    *,
    headers: Dict[str, str],
    fields: Dict[str, str],
    file_field: str,
    file_path: Path,
    file_content_type: str,
    timeout_s: int = 180,
) -> Tuple[int, JsonDict, str]:
    content = file_path.read_bytes()
    body, ctype = _multipart_form_data(fields, {file_field: (file_path.name, content, file_content_type)})
    hdrs = dict(headers)
    hdrs["content-type"] = ctype
    hdrs.setdefault("accept", "application/json")
    resp = _http("POST", url, headers=hdrs, data=body, timeout_s=timeout_s)
    text = resp.body.decode("utf-8", errors="replace")
    obj: JsonDict = {}
    try:
        parsed = json.loads(text) if text.strip() else {}
        obj = parsed if isinstance(parsed, dict) else {"_": parsed}
    except Exception:
        obj = {"_raw": text}
    return resp.status, obj, text


def _is_invalid_token(st: int, obj: JsonDict, raw: str) -> bool:
    if st != 401:
        return False
    detail = obj.get("detail") if isinstance(obj, dict) else None
    if isinstance(detail, str) and "invalid_token" in detail.lower():
        return True
    if isinstance(raw, str) and "invalid_token" in raw.lower():
        return True
    return False


# -----------------------------
# OpenAPI discovery (best-effort)
# -----------------------------
@dataclass
class ApiPaths:
    create_project: str = "/api/music/projects"
    voice_reference_for_project_tmpl: str = "/api/music/projects/{project_id}/voice-reference"
    generate_for_project_tmpl: str = "/api/music/projects/{project_id}/generate"
    status_for_job_tmpl: str = "/api/music/jobs/{job_id}/status"
    publish_for_job_tmpl: str = "/api/music/jobs/{job_id}/publish"
    upload_asset: str = "/api/music/assets/upload"


def _discover_paths(music_url: str) -> ApiPaths:
    paths = ApiPaths()
    try:
        st, spec, _ = _json_http("GET", music_url.rstrip("/") + "/openapi.json", timeout_s=20)
        if st != 200 or not isinstance(spec.get("paths"), dict):
            return paths
        opaths: Dict[str, Any] = spec["paths"]

        def has_method(p: str, m: str) -> bool:
            x = opaths.get(p)
            return isinstance(x, dict) and m.lower() in x

        for p in opaths.keys():
            if "{job_id}" in p and p.endswith("/status") and has_method(p, "get"):
                paths.status_for_job_tmpl = p
            if "{job_id}" in p and p.endswith("/publish") and has_method(p, "post"):
                paths.publish_for_job_tmpl = p
            if "{project_id}" in p and "voice-reference" in p and has_method(p, "post"):
                paths.voice_reference_for_project_tmpl = p
            if "{project_id}" in p and p.endswith("/generate") and has_method(p, "post"):
                paths.generate_for_project_tmpl = p
            if "assets" in p and "upload" in p and has_method(p, "post"):
                paths.upload_asset = p

        candidates = [p for p in opaths.keys() if "projects" in p and "{project_id}" not in p and has_method(p, "post")]
        if "/api/music/projects" in candidates:
            paths.create_project = "/api/music/projects"
        elif candidates:
            candidates.sort(key=len)
            paths.create_project = candidates[0]

    except Exception:
        return paths

    return paths


# -----------------------------
# Auth state + authed wrappers
# -----------------------------
@dataclass
class AuthState:
    core_url: str
    email: str
    password: str
    out_dir: Path
    token: str
    user_id: str
    refresh_count: int = 0

    def headers(self) -> Dict[str, str]:
        return {
            "authorization": f"Bearer {self.token}",
            "x-user-id": self.user_id,
            "accept": "application/json",
        }

    def refresh(self, *, reason: str) -> None:
        self.refresh_count += 1
        token, user_id = _login(self.core_url, self.email, self.password, self.out_dir, log_name=f"auth_refresh_{self.refresh_count}.json")
        self.token = token
        if user_id:
            self.user_id = user_id
        _write_json(
            self.out_dir / f"auth_refresh_meta_{self.refresh_count}.json",
            {"ts_utc": _iso_utc_z(), "reason": reason, "refresh_count": self.refresh_count},
        )


def _json_http_authed(
    auth: AuthState,
    method: str,
    url: str,
    *,
    payload: Optional[JsonDict] = None,
    timeout_s: int = 60,
    retry_on_invalid_token: bool = True,
) -> Tuple[int, JsonDict, str]:
    st, obj, raw = _json_http(method, url, headers=auth.headers(), payload=payload, timeout_s=timeout_s)
    if retry_on_invalid_token and _is_invalid_token(st, obj, raw):
        auth.refresh(reason=f"{method.upper()} {url} invalid_token")
        st, obj, raw = _json_http(method, url, headers=auth.headers(), payload=payload, timeout_s=timeout_s)
    return st, obj, raw


def _upload_file_multipart_authed(
    auth: AuthState,
    url: str,
    *,
    fields: Dict[str, str],
    file_field: str,
    file_path: Path,
    file_content_type: str,
    timeout_s: int = 180,
    retry_on_invalid_token: bool = True,
) -> Tuple[int, JsonDict, str]:
    st, obj, raw = _upload_file_multipart(
        url,
        headers=auth.headers(),
        fields=fields,
        file_field=file_field,
        file_path=file_path,
        file_content_type=file_content_type,
        timeout_s=timeout_s,
    )
    if retry_on_invalid_token and _is_invalid_token(st, obj, raw):
        auth.refresh(reason=f"UPLOAD {url} invalid_token")
        st, obj, raw = _upload_file_multipart(
            url,
            headers=auth.headers(),
            fields=fields,
            file_field=file_field,
            file_path=file_path,
            file_content_type=file_content_type,
            timeout_s=timeout_s,
        )
    return st, obj, raw


# -----------------------------
# Domain actions
# -----------------------------
def _login(core_url: str, email: str, password: str, out_dir: Path, *, log_name: str = "auth.json") -> Tuple[str, str]:
    url = core_url.rstrip("/") + "/api/auth/login"
    payload = {"email": email, "password": password}

    st, obj, raw = _json_http("POST", url, payload=payload, timeout_s=60)
    _write_json(out_dir / log_name, {"http_status": st, "response": obj, "raw_preview": _snip(raw, 400)})

    if st != 200:
        raise RuntimeError(f"login_failed http_status={st} resp={_snip(raw, 300)}")

    token = (
        obj.get("access_token")
        or obj.get("token")
        or obj.get("jwt")
        or _walk_find_first(obj, ("access_token", "token", "jwt"))
    )
    token = str(token or "").strip()
    if not token:
        raise RuntimeError("login_failed_missing_token")

    user_id = obj.get("user_id") or obj.get("id") or _walk_find_first(obj, ("user_id", "userId", "id"))
    if user_id is None:
        user_id = _decode_jwt_sub(token)

    user_id = str(user_id or "").strip()
    if not user_id:
        raise RuntimeError("login_failed_missing_user_id")

    return token, user_id


def _create_project(
    music_url: str,
    api: ApiPaths,
    *,
    auth: AuthState,
    title: str,
    mode: str,
    duet_layout: str,
    language_hint: str,
    out_dir: Path,
    tag: str,
) -> str:
    url = music_url.rstrip("/") + api.create_project
    payload = {"title": title, "mode": mode, "duet_layout": duet_layout, "language_hint": language_hint}

    st, obj, raw = _json_http_authed(auth, "POST", url, payload=payload, timeout_s=60)
    _write_json(out_dir / f"{tag}_project_create.json", {"http_status": st, "request": payload, "response": obj, "raw_preview": _snip(raw, 800)})

    if st not in (200, 201):
        raise RuntimeError(f"create_project_failed mode={mode} http_status={st} resp={_snip(raw, 300)}")

    pid = obj.get("project_id") or obj.get("id") or _walk_find_first(obj, ("project_id", "projectId", "id"))
    pid = str(pid or "").strip()
    if not pid:
        raise RuntimeError(f"create_project_failed_missing_project_id mode={mode}")

    return pid


def _upload_music_asset(
    music_url: str,
    api: ApiPaths,
    *,
    auth: AuthState,
    kind: str,
    file_path: str,
    project_id: Optional[str],
    job_id: Optional[str],
    duration_ms: Optional[int],
    out_dir: Path,
    tag: str,
) -> Tuple[str, Optional[str]]:
    """
    OpenAPI:
      POST /api/music/assets/upload?kind=...&project_id=...&job_id=...&duration_ms=...
      multipart/form-data with field: file
    Returns (artifact_id, sas_url?)
    """
    p = Path(file_path)
    if not p.exists():
        raise RuntimeError(f"asset_upload_missing_file kind={kind} path={file_path}")

    q: Dict[str, Any] = {"kind": kind}
    if project_id:
        q["project_id"] = project_id
    if job_id:
        q["job_id"] = job_id
    if duration_ms is not None:
        q["duration_ms"] = duration_ms

    url = music_url.rstrip("/") + api.upload_asset + "?" + urlencode(q)

    st, obj, raw = _upload_file_multipart_authed(
        auth,
        url,
        fields={},
        file_field="file",
        file_path=p,
        file_content_type="application/octet-stream",
        timeout_s=300,
    )
    _write_json(out_dir / f"{tag}_upload_asset_{kind}.json", {"http_status": st, "url": url, "response": obj, "raw_preview": _snip(raw, 800)})

    if st not in (200, 201):
        raise RuntimeError(f"asset_upload_failed kind={kind} http_status={st} resp={_snip(raw, 300)}")

    artifact_id = obj.get("artifact_id") or _walk_find_first(obj, ("artifact_id", "id"))
    artifact_id = str(artifact_id or "").strip()
    if not artifact_id:
        raise RuntimeError(f"asset_upload_failed_missing_artifact_id kind={kind}")

    sas_url = obj.get("sas_url") or obj.get("url") or obj.get("storage_ref") or _walk_find_first(obj, ("sas_url", "url", "storage_ref"))
    sas_url_s = str(sas_url or "").strip() or None
    return artifact_id, sas_url_s


def _upload_voice_reference_for_project(
    music_url: str,
    api: ApiPaths,
    *,
    auth: AuthState,
    project_id: str,
    voice_ref_path: str,
    out_dir: Path,
    tag: str,
) -> str:
    """
    OpenAPI:
      POST /api/music/projects/{project_id}/voice-reference
      multipart/form-data with field: file
    Returns voice_ref_asset_id
    """
    p = Path(voice_ref_path)
    if not p.exists():
        raise RuntimeError(f"voice_ref_missing_file path={voice_ref_path}")

    path = api.voice_reference_for_project_tmpl.replace("{project_id}", project_id)
    url = music_url.rstrip("/") + path

    st, obj, raw = _upload_file_multipart_authed(
        auth,
        url,
        fields={},
        file_field="file",
        file_path=p,
        file_content_type="application/octet-stream",
        timeout_s=300,
    )
    _write_json(out_dir / f"{tag}_voice_reference_upload.json", {"http_status": st, "response": obj, "raw_preview": _snip(raw, 800)})

    if st not in (200, 201):
        raise RuntimeError(f"voice_ref_upload_failed http_status={st} resp={_snip(raw, 300)}")

    voice_ref_asset_id = obj.get("voice_ref_asset_id") or _walk_find_first(obj, ("voice_ref_asset_id", "id"))
    voice_ref_asset_id = str(voice_ref_asset_id or "").strip()
    if not voice_ref_asset_id:
        raise RuntimeError("voice_ref_upload_failed_missing_voice_ref_asset_id")

    return voice_ref_asset_id


def _generate_job(
    music_url: str,
    api: ApiPaths,
    *,
    auth: AuthState,
    project_id: str,
    mode: str,
    uploaded_audio_asset_id: Optional[str],
    uploaded_audio_url: Optional[str],
    quality: str,
    outputs: list[str],
    seed: Optional[int],
    lyrics_source: Optional[str],
    lyrics_text: Optional[str],
    lyrics_language_hint: Optional[str],
    track_prompt: Optional[str],
    genre_hint: Optional[str],
    vibe_hint: Optional[str],
    provider_hints: Optional[JsonDict],
    out_dir: Path,
    tag: str,
) -> str:
    """
    OpenAPI GenerateMusicIn supports:
      seed, quality, outputs, provider_hints, uploaded_audio_asset_id, uploaded_audio_url,
      track_prompt, genre_hint, vibe_hint, lyrics_source, lyrics_text, lyrics_language_hint
    """
    path = api.generate_for_project_tmpl.replace("{project_id}", project_id)
    url = music_url.rstrip("/") + path

    payload: JsonDict = {"quality": quality, "outputs": outputs, "provider_hints": provider_hints or {}}

    if seed is not None:
        payload["seed"] = seed

    if uploaded_audio_asset_id:
        payload["uploaded_audio_asset_id"] = uploaded_audio_asset_id
    if uploaded_audio_url:
        payload["uploaded_audio_url"] = uploaded_audio_url

    if track_prompt:
        payload["track_prompt"] = track_prompt
    if genre_hint:
        payload["genre_hint"] = genre_hint
    if vibe_hint:
        payload["vibe_hint"] = vibe_hint

    if lyrics_source:
        payload["lyrics_source"] = lyrics_source
    if lyrics_text:
        payload["lyrics_text"] = lyrics_text
    if lyrics_language_hint:
        payload["lyrics_language_hint"] = lyrics_language_hint

    st, obj, raw = _json_http_authed(auth, "POST", url, payload=payload, timeout_s=60)
    _write_json(out_dir / f"{tag}_generate.json", {"http_status": st, "mode": mode, "request": payload, "response": obj, "raw_preview": _snip(raw, 1200)})

    if st not in (200, 201, 202):
        raise RuntimeError(f"generate_failed mode={mode} http_status={st} resp={_snip(raw, 300)}")

    jid = obj.get("job_id") or obj.get("id") or _walk_find_first(obj, ("job_id", "id"))
    jid = str(jid or "").strip()
    if not jid:
        raise RuntimeError(f"generate_failed_missing_job_id mode={mode}")

    return jid


def _poll_status(
    music_url: str,
    api: ApiPaths,
    *,
    auth: AuthState,
    job_id: str,
    out_dir: Path,
    tag: str,
    timeout_s: int,
    poll_s: float,
    write_every_n: int = 5,
) -> JsonDict:
    path = api.status_for_job_tmpl.replace("{job_id}", job_id)
    url = music_url.rstrip("/") + path

    t0 = time.time()
    n = 0
    last_key = ""
    last_written_key = ""
    last_path = out_dir / f"{tag}_status_last.json"

    while True:
        n += 1
        st, obj, raw = _json_http_authed(auth, "GET", url, payload=None, timeout_s=60)

        if st != 200:
            if time.time() - t0 > 30:
                raise RuntimeError(f"status_failed http_status={st} resp={_snip(raw, 250)}")
            time.sleep(poll_s)
            continue

        status = str(obj.get("status") or "").strip().lower()
        stage = str(obj.get("stage") or "").strip().lower()
        prog = obj.get("progress")
        key = f"{status}|{stage}|{prog}"

        # write rolling last status only on change / every N polls
        if obj and (key != last_written_key) and (n % write_every_n == 0 or status in ("succeeded", "failed")):
            _write_json(last_path, obj)
            last_written_key = key

        # print progress on change (or every ~5 polls)
        if key != last_key and (n % 5 == 0 or status in ("succeeded", "failed")):
            elapsed = int(time.time() - t0)
            print(f"[{_iso_utc_z()}] {tag} job={job_id} status={status} stage={stage} elapsed_s={elapsed}")
            last_key = key

        if status in ("succeeded", "failed"):
            _write_json(out_dir / f"{tag}_status.json", obj)
            return obj

        if time.time() - t0 > timeout_s:
            _write_json(out_dir / f"{tag}_status_timeout.json", {"last": obj, "elapsed_s": int(time.time() - t0)})
            raise RuntimeError(f"poll_timeout mode={tag} job_id={job_id}")

        time.sleep(poll_s)


def _qc_or_die(status_obj: JsonDict, *, tag: str) -> Tuple[str, str]:
    status = str(status_obj.get("status") or "").strip().lower()
    if status != "succeeded":
        err = status_obj.get("error")
        raise RuntimeError(f"qc_failed status={status} tag={tag} error={err}")

    computed = _as_dict(status_obj.get("computed"))
    if not computed:
        raise RuntimeError(f"qc_failed_missing_computed tag={tag}")

    mp = computed.get("music_plan")
    if not mp:
        raise RuntimeError(f"qc_failed_missing_music_plan tag={tag}")

    cm_any = status_obj.get("clip_manifest") or computed.get("clip_manifest")
    cm_d = _as_dict(cm_any)
    clips = cm_d.get("clips")
    if not isinstance(clips, list) or not clips:
        raise RuntimeError(f"qc_failed_missing_clips tag={tag}")

    tracks = status_obj.get("tracks")
    has_full = False
    if isinstance(tracks, list):
        for t in tracks:
            td = _as_dict(t)
            if str(td.get("track_type") or "") == "full_mix":
                has_full = True
                break
    if not has_full:
        raise RuntimeError(f"qc_failed_missing_full_mix_track tag={tag}")

    video_outputs = _as_dict(computed.get("video_outputs"))
    final_url = (
        video_outputs.get("final_url")
        or video_outputs.get("viewer_url")
        or computed.get("final_video_url")
        or computed.get("video_final_url")
        or _walk_find_first(computed, ("final_url", "final_video_url", "video_final_url", "viewer_url"))
        or ""
    )
    final_url = str(final_url or "").strip()
    if not final_url:
        raise RuntimeError(f"qc_failed_missing_final_url tag={tag}")

    return final_url, final_url


def _publish(
    music_url: str,
    api: ApiPaths,
    *,
    auth: AuthState,
    job_id: str,
    target: str,
    out_dir: Path,
    tag: str,
) -> JsonDict:
    path = api.publish_for_job_tmpl.replace("{job_id}", job_id)
    url = music_url.rstrip("/") + path

    consent = {
        "accepted": True,
        "user_has_rights": True,
        "allow_synthesis": True,
        "allow_publish": True,
        "source": "df_e2e_music_3modes.py",
        "ts_utc": _iso_utc_z(),
    }
    payload = {"target": target, "consent": consent}

    st, obj, raw = _json_http_authed(auth, "POST", url, payload=payload, timeout_s=60)
    _write_json(out_dir / f"{tag}_publish.json", {"http_status": st, "request": payload, "response": obj, "raw_preview": _snip(raw, 1200)})

    if st != 200:
        raise RuntimeError(f"publish_failed tag={tag} http_status={st} resp={_snip(raw, 300)}")

    return obj


# -----------------------------
# Main
# -----------------------------
def main() -> int:
    core_url = _read_env("CORE_URL", "http://localhost:8000") or "http://localhost:8000"
    music_url = _read_env("MUSIC_URL", "http://localhost:8007") or "http://localhost:8007"
    email = _read_env("DF_EMAIL")
    password = _read_env("DF_PASSWORD")

    if not email or not password:
        print("Missing DF_EMAIL / DF_PASSWORD env vars.", file=sys.stderr)
        return 2

    voice_ref_path = _read_env("VOICE_REF_PATH")  # uploaded per project (autopilot/co_create)
    byo_audio_path = _read_env("BYO_AUDIO_PATH")
    byo_audio_url = _read_env("BYO_AUDIO_URL")

    duet_layout = _read_env("DUET_LAYOUT", "split_screen") or "split_screen"
    language_hint = _read_env("LANGUAGE_HINT", "en-IN") or "en-IN"

    target_publish = (_read_env("PUBLISH_TARGET", "viewer") or "viewer").strip().lower()
    if target_publish not in ("viewer", "fusion"):
        target_publish = "viewer"

    quality = (_read_env("GEN_QUALITY", "standard") or "standard").strip()
    outputs = _csv_list(_read_env("GEN_OUTPUTS", "full_mix,timed_lyrics_json,cover_art")) or ["full_mix"]

    seed0 = _read_env_int("SEED", None)

    track_prompt = _read_env("TRACK_PROMPT")
    genre_hint = _read_env("GENRE_HINT")
    vibe_hint = _read_env("VIBE_HINT")
    lyrics_text = _read_env("LYRICS_TEXT")
    lyrics_lang = _read_env("LYRICS_LANGUAGE_HINT")

    lyrics_source_autopilot = (_read_env("LYRICS_SOURCE_AUTOPILOT", "generate") or "generate").strip()
    lyrics_source_cocreate = (_read_env("LYRICS_SOURCE_COCREATE", "generate") or "generate").strip()
    lyrics_source_byo = (_read_env("LYRICS_SOURCE_BYO", "none") or "none").strip()

    poll_s = _read_env_float("POLL_S", 2.0) or 2.0
    poll_timeout_s = _read_env_int("POLL_TIMEOUT_S", 45 * 60) or (45 * 60)

    fast_preview = _read_env_bool("FAST_PREVIEW", False)
    provider_hints_json = _read_env("PROVIDER_HINTS_JSON")
    provider_hints = _as_dict(provider_hints_json) if provider_hints_json else {}
    if fast_preview:
        # backend can ignore unknown hints; use once svc-music supports it
        provider_hints.setdefault("fast_preview", True)
        provider_hints.setdefault("skip_performer_videos", True)

    run_dir = Path("/tmp") / f"df_e2e_music_3modes_{_now_tag()}"
    _ensure_dir(run_dir)
    print(f"✅ E2E starting. Run dir: {run_dir}")

    api = _discover_paths(music_url)
    _write_json(run_dir / "discovered_paths.json", api.__dict__)

    token, user_id = _login(core_url, email, password, run_dir, log_name="auth.json")
    auth = AuthState(core_url=core_url, email=email, password=password, out_dir=run_dir, token=token, user_id=user_id)

    summary: JsonDict = {"run_dir": str(run_dir), "core_url": core_url, "music_url": music_url, "modes": {}}

    def run_mode(mode: str, tag: str, title: str, byo_url: Optional[str]) -> None:
        pid = _create_project(
            music_url,
            api,
            auth=auth,
            title=title,
            mode=mode,
            duet_layout=duet_layout,
            language_hint=language_hint,
            out_dir=run_dir,
            tag=tag,
        )

        # Voice reference is a dedicated endpoint per project (OpenAPI)
        if voice_ref_path and mode in ("autopilot", "co_create"):
            _upload_voice_reference_for_project(
                music_url,
                api,
                auth=auth,
                project_id=pid,
                voice_ref_path=voice_ref_path,
                out_dir=run_dir,
                tag=tag,
            )

        uploaded_audio_asset_id: Optional[str] = None
        uploaded_audio_url2: Optional[str] = None

        if mode == "byo":
            if byo_url:
                uploaded_audio_url2 = byo_url
            elif byo_audio_path:
                aid, _ = _upload_music_asset(
                    music_url,
                    api,
                    auth=auth,
                    kind="uploaded_audio",
                    file_path=byo_audio_path,
                    project_id=pid,
                    job_id=None,
                    duration_ms=None,
                    out_dir=run_dir,
                    tag=tag,
                )
                uploaded_audio_asset_id = aid
            else:
                raise RuntimeError("byo_mode_missing_audio: set BYO_AUDIO_URL or BYO_AUDIO_PATH")

        mode_seed = None
        if seed0 is not None:
            bump = {"autopilot": 0, "co_create": 7, "byo": 13}.get(mode, 0)
            mode_seed = seed0 + bump

        lyrics_source = {"autopilot": lyrics_source_autopilot, "co_create": lyrics_source_cocreate, "byo": lyrics_source_byo}.get(mode, "generate")

        jid = _generate_job(
            music_url,
            api,
            auth=auth,
            project_id=pid,
            mode=mode,
            uploaded_audio_asset_id=uploaded_audio_asset_id,
            uploaded_audio_url=uploaded_audio_url2,
            quality=quality,
            outputs=outputs,
            seed=mode_seed,
            lyrics_source=lyrics_source,
            lyrics_text=lyrics_text,
            lyrics_language_hint=lyrics_lang,
            track_prompt=track_prompt,
            genre_hint=genre_hint,
            vibe_hint=vibe_hint,
            provider_hints=provider_hints,
            out_dir=run_dir,
            tag=tag,
        )

        st_obj = _poll_status(
            music_url,
            api,
            auth=auth,
            job_id=jid,
            out_dir=run_dir,
            tag=tag,
            timeout_s=poll_timeout_s,
            poll_s=poll_s,
        )
        final_url, _ = _qc_or_die(st_obj, tag=tag)
        pub = _publish(music_url, api, auth=auth, job_id=jid, target=target_publish, out_dir=run_dir, tag=tag)

        summary["modes"][tag] = {"mode": mode, "project_id": pid, "job_id": jid, "final_url": final_url, "publish_status": pub.get("status")}

    # autopilot + co_create always run
    run_mode("autopilot", "autopilot", "E2E Autopilot Music Video", None)
    run_mode("co_create", "co_create", "E2E Co-create Music Video", None)

    # BYO optional (URL or PATH)
    if byo_audio_url:
        run_mode("byo", "byo", "E2E BYO Music Video", byo_audio_url.strip())
    elif byo_audio_path:
        run_mode("byo", "byo", "E2E BYO Music Video", None)
    else:
        summary["modes"]["byo"] = {"mode": "byo", "skipped": True, "reason": "BYO skipped: provide BYO_AUDIO_PATH or BYO_AUDIO_URL."}
        print("ℹ️  BYO skipped: provide BYO_AUDIO_PATH or BYO_AUDIO_URL to run BYO mode.")

    _write_json(run_dir / "summary.json", summary)

    print("\n✅ E2E completed. Run dir:", run_dir)
    for k, v in summary["modes"].items():
        if v.get("skipped"):
            print(f"  - {k:<9} SKIPPED reason={v.get('reason')}")
        else:
            print(f"  - {k:<9} job={v.get('job_id')} final={v.get('final_url')} publish={v.get('publish_status')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())