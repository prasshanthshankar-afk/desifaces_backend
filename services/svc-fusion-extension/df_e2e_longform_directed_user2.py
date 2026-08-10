#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib import error, parse, request

EMAIL = os.getenv("DF_EMAIL", "user2@desifaces.ai")
PASSWORD = os.getenv("DF_PASSWORD", "password2")
CORE_URL = os.getenv("CORE_URL", "http://localhost:8000").rstrip("/")
LONGFORM_URL = os.getenv("LONGFORM_URL", "http://localhost:8006").rstrip("/")
DB_SERVICE = os.getenv("DB_SERVICE", "desifaces-db")
DB_USER = os.getenv("POSTGRES_USER", "desifaces_admin")
DB_NAME = os.getenv("POSTGRES_DB", "desifaces")
TIMEOUT_SECONDS = int(os.getenv("TIMEOUT_SECONDS", "1800"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))
ASPECT_RATIO = os.getenv("ASPECT_RATIO", "9:16")
SCENARIO_TYPE = os.getenv("SCENARIO_TYPE", "founder_story")
KEEP_JSON = os.getenv("KEEP_JSON", "1") == "1"
AUTH_RETRY_ON_401 = os.getenv("AUTH_RETRY_ON_401", "1") == "1"


def info(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def repo_root_candidates() -> Iterable[Path]:
    cwd = Path.cwd().resolve()
    yield cwd
    for p in [cwd, *cwd.parents]:
        yield p
    script_dir = Path(__file__).resolve().parent
    yield script_dir
    for p in [script_dir, *script_dir.parents]:
        yield p


def find_repo_root() -> Path:
    for p in repo_root_candidates():
        if (p / "infra" / ".env").exists():
            return p
    fail("Could not find repo root containing infra/.env. Run this from the desifaces-v2 repo or set up the expected layout.")


def run_cmd(cmd: Sequence[str], *, cwd: Optional[Path] = None, check: bool = True) -> str:
    proc = subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and proc.returncode != 0:
        fail(f"Command failed: {' '.join(cmd)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc.stdout


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def psql_query(repo_root: Path, sql: str) -> List[str]:
    cmd = [
        "docker", "compose", "--env-file", "infra/.env",
        "exec", "-T", DB_SERVICE,
        "psql", "-U", DB_USER, "-d", DB_NAME,
        "-At", "-F", "\t", "-c", sql,
    ]
    out = run_cmd(cmd, cwd=repo_root)
    return [line.rstrip("\n") for line in out.splitlines() if line.strip()]


def http_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    form_body: Optional[Dict[str, Any]] = None,
    timeout: int = 60,
) -> Tuple[int, Dict[str, Any]]:
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    data: Optional[bytes] = None
    if json_body is not None:
        req_headers["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode("utf-8")
    elif form_body is not None:
        req_headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = parse.urlencode(form_body).encode("utf-8")

    req = request.Request(url, data=data, method=method.upper(), headers=req_headers)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            try:
                body = json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                body = {"raw": payload}
            return resp.status, body
    except error.HTTPError as e:
        payload = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            body = {"raw": payload}
        return e.code, body
    except error.URLError as e:
        return 0, {"error": str(e)}


def _auth_error_message(body: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("detail", "error", "message", "raw"):
        val = body.get(key)
        if val is None:
            continue
        parts.append(str(val))
    return " | ".join(parts).strip().lower()


def _is_retryable_auth_failure(status: int, body: Dict[str, Any]) -> bool:
    if status != 401:
        return False
    msg = _auth_error_message(body)
    if not msg:
        return True
    needles = [
        "signature has expired",
        "token expired",
        "invalid token",
        "jwt",
        "not authenticated",
        "could not validate credentials",
    ]
    return any(n in msg for n in needles)


@dataclass
class AuthSession:
    token: str = ""

    def login(self, *, force: bool = False) -> str:
        if self.token and not force:
            return self.token

        candidates: List[Tuple[str, str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]] = [
            ("POST", f"{CORE_URL}/api/auth/login", {"email": EMAIL, "password": PASSWORD}, None),
            ("POST", f"{CORE_URL}/api/auth/login", {"username": EMAIL, "password": PASSWORD}, None),
            ("POST", f"{CORE_URL}/api/auth/token", None, {"username": EMAIL, "password": PASSWORD}),
            ("POST", f"{CORE_URL}/api/auth/token", None, {"email": EMAIL, "password": PASSWORD}),
        ]
        last = None
        for method, url, jbody, fbody in candidates:
            status, body = http_json(method, url, json_body=jbody, form_body=fbody)
            if status and 200 <= status < 300 and isinstance(body, dict):
                token = body.get("access_token") or body.get("token")
                if token:
                    self.token = str(token)
                    info(f"Login succeeded via {url}")
                    return self.token
            last = (url, status, body)
        fail(f"Unable to log in as {EMAIL}. Last response: {last}")

    def auth_headers(self) -> Dict[str, str]:
        token = self.login()
        return {"Authorization": f"Bearer {token}"}

    def request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        form_body: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
        retry_auth: bool = True,
    ) -> Tuple[int, Dict[str, Any]]:
        status, body = http_json(
            method,
            url,
            headers=self.auth_headers(),
            json_body=json_body,
            form_body=form_body,
            timeout=timeout,
        )

        if retry_auth and AUTH_RETRY_ON_401 and _is_retryable_auth_failure(status, body):
            info(f"Auth expired/invalid on {method.upper()} {url}; re-authenticating and retrying once.")
            self.login(force=True)
            status, body = http_json(
                method,
                url,
                headers=self.auth_headers(),
                json_body=json_body,
                form_body=form_body,
                timeout=timeout,
            )

        return status, body


def get_user_id(repo_root: Path) -> str:
    rows = psql_query(
        repo_root,
        f"select id from core.users where lower(email)=lower({sql_quote(EMAIL)}) order by created_at desc nulls last, id desc limit 1;",
    )
    if not rows:
        fail(f"No user found in database for email {EMAIL}")
    return rows[0].split("\t")[0]


def get_media_assets_columns(repo_root: Path) -> set[str]:
    rows = psql_query(
        repo_root,
        "select column_name from information_schema.columns where table_schema='public' and table_name='media_assets' order by ordinal_position;",
    )
    return {r.strip() for r in rows}


def get_best_face_artifact(repo_root: Path, user_id: str) -> Tuple[str, Dict[str, Any]]:
    cols = get_media_assets_columns(repo_root)
    if not cols:
        fail("public.media_assets table not found or has no columns")

    select_fields = ["id"]
    if "storage_ref" in cols:
        select_fields.append("storage_ref")
    if "created_at" in cols:
        select_fields.append("created_at")
    if "kind" in cols:
        select_fields.append("kind")
    if "asset_type" in cols:
        select_fields.append("asset_type")
    if "media_type" in cols:
        select_fields.append("media_type")

    where_parts: List[str] = []
    if "user_id" in cols:
        where_parts.append(f"user_id = {sql_quote(user_id)}::uuid")

    image_filters: List[str] = []
    if "content_type" in cols:
        image_filters.append("content_type like 'image/%'")
    if "mime_type" in cols:
        image_filters.append("mime_type like 'image/%'")
    if "storage_ref" in cols:
        image_filters.append("storage_ref ~* '\\.(png|jpg|jpeg|webp)$'")
    if "meta_json" in cols:
        image_filters.append("(meta_json->>'content_type' like 'image/%' or meta_json->>'mime_type' like 'image/%')")
    if image_filters:
        where_parts.append("(" + " or ".join(image_filters) + ")")

    face_pref: List[str] = []
    if "kind" in cols:
        face_pref.append("kind ilike '%face%'")
        face_pref.append("kind ilike '%portrait%'")
    if "asset_type" in cols:
        face_pref.append("asset_type ilike '%face%'")
        face_pref.append("asset_type ilike '%portrait%'")
    if "media_type" in cols:
        face_pref.append("media_type ilike '%face%'")
        face_pref.append("media_type ilike '%portrait%'")
    if "meta_json" in cols:
        face_pref.append("meta_json::text ilike '%face%'")
        face_pref.append("meta_json::text ilike '%portrait%'")

    order_parts: List[str] = []
    if face_pref:
        order_parts.append("case when (" + " or ".join(face_pref) + ") then 0 else 1 end")
    if "created_at" in cols:
        order_parts.append("created_at desc")
    order_parts.append("id desc")

    query = (
        f"select {', '.join(select_fields)} from public.media_assets "
        + ("where " + " and ".join(where_parts) if where_parts else "")
        + f" order by {', '.join(order_parts)} limit 5;"
    )
    rows = psql_query(repo_root, query)

    if not rows:
        fallback = psql_query(
            repo_root,
            f"select face_artifact_id from public.longform_jobs where user_id={sql_quote(user_id)}::uuid order by created_at desc, id desc limit 1;",
        )
        if fallback:
            return fallback[0].split("\t")[0], {"source": "longform_jobs_fallback"}
        fail(f"No usable face artifact found in media_assets for {EMAIL}")

    first = rows[0].split("\t")
    artifact_id = first[0]
    meta: Dict[str, Any] = {"source": "media_assets"}
    if len(first) > 1:
        meta["storage_ref"] = first[1]
    if len(first) > 2:
        meta["created_at"] = first[2]
    return artifact_id, meta


def write_json(out_dir: Path, name: str, payload: Dict[str, Any]) -> None:
    if not KEEP_JSON:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def create_longform_job(auth: AuthSession, face_artifact_id: str, out_dir: Path) -> str:
    body: Dict[str, Any] = {
        "mode": "directed",
        "face_artifact_id": face_artifact_id,
        "aspect_ratio": ASPECT_RATIO,
        "segment_seconds": 30,
        "max_segment_seconds": 60,
        "voice": {
            "locale": "en-US",
            "gender": "male",
            "output_format": "mp3",
        },
        "voice_gender_mode": "auto",
        "intent": {
            "goal": "Create a cinematic founder-style longform video for DesiFaces.ai",
            "audience": "Consumers, creators, and potential investors",
            "tone": ["premium", "cinematic", "confident"],
            "style": ["modern", "emotional", "founder-led"],
            "scenario_type": SCENARIO_TYPE,
            "duration_sec": 75,
        },
        "message": {
            "must_include": [
                "DesiFaces.ai creates premium personalized visual storytelling.",
                "The platform connects face, voice, and talking video into one cinematic workflow.",
                "The experience is built for authentic, high-quality branded content.",
            ],
            "cta": "Visit desifaces.ai to experience the future of AI-powered storytelling.",
        },
        "constraints": {
            "external_provider_ok": True,
            "require_subtitles": True,
            "max_repair_rounds": 1,
            "aspect_ratios": [ASPECT_RATIO],
        },
        "assets": {
            "face_artifact_id": face_artifact_id,
        },
        "tags": {
            "e2e": True,
            "runner": "df_e2e_longform_directed_user2.py",
            "requested_by": EMAIL,
        },
    }

    status, resp = auth.request_json(
        "POST",
        f"{LONGFORM_URL}/api/longform/jobs",
        json_body=body,
        timeout=120,
    )
    write_json(out_dir, "create_job_response.json", {"status": status, "body": resp, "request": body})
    if not (200 <= status < 300):
        fail(f"Create longform job failed [{status}]: {json.dumps(resp, indent=2)}")
    job_id = resp.get("job_id") or resp.get("longform_job_id")
    if not job_id:
        fail(f"Create longform job succeeded but no job_id returned: {resp}")
    return str(job_id)


def poll_job(auth: AuthSession, job_id: str, out_dir: Path) -> Dict[str, Any]:
    deadline = time.time() + TIMEOUT_SECONDS
    attempt = 0
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        attempt += 1
        status, body = auth.request_json(
            "GET",
            f"{LONGFORM_URL}/api/longform/jobs/{job_id}",
            timeout=60,
        )
        if not (200 <= status < 300):
            fail(f"Polling job failed [{status}]: {json.dumps(body, indent=2)}")
        last = body
        write_json(out_dir, "job_status_latest.json", body)
        info(
            f"Poll {attempt}: status={body.get('status')} stage={body.get('stage')} "
            f"completed={body.get('completed_segments')}/{body.get('total_segments')}"
        )
        if body.get("status") in {"succeeded", "failed", "canceled"}:
            return body
        time.sleep(POLL_SECONDS)
    fail(f"Timed out waiting for job {job_id} after {TIMEOUT_SECONDS}s. Last body: {json.dumps(last, indent=2)}")


def fetch_segments(auth: AuthSession, job_id: str, out_dir: Path) -> List[Dict[str, Any]]:
    status, body = auth.request_json(
        "GET",
        f"{LONGFORM_URL}/api/longform/jobs/{job_id}/segments",
        timeout=60,
    )
    if not (200 <= status < 300):
        fail(f"Fetching segments failed [{status}]: {json.dumps(body, indent=2)}")
    if not isinstance(body, list):
        fail(f"Unexpected segments response: {body}")
    write_json(out_dir, "segments.json", {"segments": body})
    return body


def main() -> None:
    repo_root = find_repo_root()
    out_dir = repo_root / "tmp" / f"df_e2e_longform_directed_{int(time.time())}"
    out_dir.mkdir(parents=True, exist_ok=True)

    info(f"REPO_ROOT={repo_root}")
    info(f"CORE_URL={CORE_URL}")
    info(f"LONGFORM_URL={LONGFORM_URL}")
    info(f"OUT_DIR={out_dir}")
    info(f"DF_EMAIL={EMAIL}")

    auth = AuthSession()
    auth.login()
    user_id = get_user_id(repo_root)
    info(f"Resolved user_id={user_id}")

    face_artifact_id, face_meta = get_best_face_artifact(repo_root, user_id)
    info(f"Selected face_artifact_id={face_artifact_id}")
    if face_meta:
        info(f"Artifact meta: {json.dumps(face_meta, indent=2)}")
        write_json(out_dir, "selected_face_artifact.json", {"face_artifact_id": face_artifact_id, **face_meta})

    job_id = create_longform_job(auth, face_artifact_id, out_dir)
    info(f"Created longform job_id={job_id}")

    final_job = poll_job(auth, job_id, out_dir)
    segments = fetch_segments(auth, job_id, out_dir)

    if final_job.get("status") != "succeeded":
        fail(
            "Longform job did not succeed.\n"
            f"Job: {json.dumps(final_job, indent=2)}\n"
            f"Segments: {json.dumps(segments, indent=2)}"
        )

    final_video_url = final_job.get("final_video_url") or final_job.get("final_video_signed_url")
    if not final_video_url:
        fail(f"Job succeeded but no final_video_url returned: {json.dumps(final_job, indent=2)}")

    print("\n=== SUCCESS ===")
    print(f"job_id={job_id}")
    print(f"final_video_url={final_video_url}")
    print(f"segments={len(segments)}")
    print(f"out_dir={out_dir}")


if __name__ == "__main__":
    main()
