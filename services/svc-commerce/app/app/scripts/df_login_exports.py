#!/usr/bin/env python3
# scripts/df_login_exports.py
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError

TOKEN_KEYS = ("access_token", "token", "bearer_token", "accessToken")
USER_KEYS = ("user_id", "userId", "x_user_id")


def _deep_get_first(d, keys):
    if isinstance(d, dict):
        for k in keys:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in d.values():
            got = _deep_get_first(v, keys)
            if got:
                return got
    elif isinstance(d, list):
        for it in d:
            got = _deep_get_first(it, keys)
            if got:
                return got
    return ""


def _jwt_sub(token: str) -> str:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
        sub = data.get("sub", "")
        return str(sub) if sub else ""
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core-url", default=os.environ.get("CORE_URL", "http://localhost:8000"))
    ap.add_argument("--email", default=os.environ.get("DF_EMAIL", ""))
    ap.add_argument("--password", default=os.environ.get("DF_PASSWORD", ""))
    ap.add_argument("--device-id", default=os.environ.get("DF_DEVICE_ID", "desktop"))
    ap.add_argument("--client-type", default=os.environ.get("DF_CLIENT_TYPE", "web"))
    args = ap.parse_args()

    if not args.email or not args.password:
        print("ERROR: set DF_EMAIL/DF_PASSWORD or pass --email/--password", file=sys.stderr)
        return 2

    url = args.core_url.rstrip("/") + "/api/auth/login"
    payload = {
        "email": args.email,
        "password": args.password,
        "device_id": args.device_id,
        "client_type": args.client_type,
    }
    body = json.dumps(payload).encode("utf-8")

    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        print(f"ERROR: login failed HTTP {e.code}: {raw}", file=sys.stderr)
        return 3

    try:
        data = json.loads(raw)
    except Exception:
        print(f"ERROR: login returned non-JSON: {raw[:2000]}", file=sys.stderr)
        return 4

    token = _deep_get_first(data, TOKEN_KEYS)
    user_id = _deep_get_first(data, USER_KEYS)

    if not user_id and isinstance(data, dict):
        u = data.get("user")
        if isinstance(u, dict):
            user_id = str(u.get("id") or "").strip()

    if not token:
        print(f"ERROR: could not find token in response keys={list(data.keys())}", file=sys.stderr)
        return 5

    if not user_id:
        user_id = _jwt_sub(token)

    if not user_id:
        print("ERROR: could not derive user_id (missing in response and JWT had no sub)", file=sys.stderr)
        return 6

    print(f'export DF_BEARER_TOKEN="{token}"')
    print(f'export DF_X_USER_ID="{user_id}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())